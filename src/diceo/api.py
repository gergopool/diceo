"""The public entry points: :func:`chunk`, :func:`extract`, :func:`sniff`.

Two generators over one document, plus the detector they share.

Both are lazy in the way that matters:
``islice(chunk(path), 5)`` reads a handful of pages and stops, and peak memory
does not grow with the document.

They are deliberately **not** lazy about failing. Opening the source and sniffing
the format happen when you call them, so ``diceo.chunk("typo.pdf")`` raises
:class:`~diceo.errors.DocumentNotFound` at the call rather than handing back a
healthy-looking iterator that detonates at the caller's ``for`` loop -- outside
the ``try`` that everyone writes. See :func:`_open`; it costs nothing, because it
is the same work moved earlier.

Everything here is also *defensive at exactly one place*: :func:`_guard` wraps the
reader generator so that a backend raising ``KeyError``, ``struct.error`` or a
ctypes-level ``OSError`` mid-document becomes a
:class:`~diceo.errors.CorruptDocument` **after** the blocks that were already
read have been yielded. Half a document plus a diagnostic beats an exception and
nothing, and it is the difference between a pipeline that indexes 9,998 of 10,000
files and one that stops at the first bad byte.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import IO

from diceo._diagnostic_text import bounded_list, control_chars_note, images_note
from diceo.chunker import chunk_blocks
from diceo.errors import CorruptDocument, DiceoError, EncryptedDocument, UnsupportedFormat
from diceo.source import SUPPORTED, Source, detect, open_source, sniff
from diceo.types import Block, Chunk, Diagnostics, Limits, Locator

Readable = str | Path | bytes | bytearray | memoryview | IO[bytes]

_FAILURES: tuple[type[BaseException], ...] | None = None


def backend_failures() -> tuple[type[BaseException], ...]:
    """Exactly the exceptions that mean "these bytes are not what they claim".

    Enumerated rather than approximated, because approximating it is a trap this
    module fell into on the first attempt: ``zipfile.BadZipFile``, ``struct.error``
    and ``zlib.error`` are all bare ``Exception`` subclasses, so a
    plausible-looking ``except (ValueError, OSError)`` catches **none** of the
    three failures a truncated OOXML file actually produces.

    ``TypeError`` and ``AttributeError`` are deliberately **not** here. They are
    overwhelmingly the shape of *our* bugs, and reporting a bug in diceo as
    "your document is corrupt" sends every one of them to the wrong person.

    Built on first use and cached, so ``import diceo`` does not pay for
    ``zipfile`` (which pulls in ``bz2``, ``lzma`` and ``shutil``) -- the cold
    import budget is 150 ms for a reason: this package gets imported once per
    worker process, thousands of times.
    """
    global _FAILURES
    if _FAILURES is None:
        import struct
        import zipfile
        import zlib
        from xml.etree.ElementTree import ParseError

        extra: list[type[BaseException]] = []
        try:
            from python_calamine import CalamineError

            extra.append(CalamineError)
        except ImportError:  # pragma: no cover - a declared dependency
            pass
        _FAILURES = (
            ValueError,  # includes UnicodeDecodeError and pypdfium2's arg checks
            LookupError,  # superclass of KeyError and IndexError, and the shape that
            # `codecs` raises for an encoding name nobody knows. An OOXML part is free
            # to declare `<?xml version="1.0" encoding="UUF-8"?>`, and three readers
            # passed that name straight to the decoder; KeyError and IndexError were
            # both listed here and their common ancestor was not, so the one case that
            # comes from the *document* rather than from our own dict lookups escaped.
            OSError,  # ctypes, truncated reads, every I/O failure
            RuntimeError,  # pypdfium2.PdfiumError inherits from this; so does
            # RecursionError, which is what a deeply nested XML part produces
            EOFError,
            OverflowError,  # a spreadsheet serial date outside datetime's range
            NotImplementedError,  # a ZIP compression method CPython cannot decode
            MemoryError,  # the guards above bound what a *document* can declare, and
            # they cannot bound the box: a machine already near its limit, a 6 GB
            # spreadsheet whose sharedStrings table is legitimately enormous, or a
            # worker with an RLIMIT_AS raises this instead. It is a failure to read
            # this document, which is what `DiceoError` means -- and leaving it out
            # made the one promise the errors module makes ("chunks, or a
            # DiceoError, never a third thing") false on every out-of-memory path.
            struct.error,
            zlib.error,
            ParseError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            *extra,
        )
    return _FAILURES


_ENCRYPTION_HINTS = ("password", "encrypt", "not decrypted", "security handler")


def _guard(stream: Iterator[Block], source: str, report: Diagnostics) -> Iterator[Block]:
    """Yield from a reader, converting a mid-document backend failure into data.

    The blocks read before the failure are already gone downstream -- that is the
    point of a generator -- so the failure is recorded and the iteration ends
    cleanly. Nothing was lost silently: ``diagnostics.truncated`` names the
    exception and ``lost_data`` is True.

    A document that produced *nothing at all* is different, and raises
    :class:`~diceo.errors.CorruptDocument`, because a caller who gets zero
    chunks and no exception cannot tell an empty document from a failed one.
    """
    produced = 0
    try:
        for block in stream:
            produced += 1
            yield block
    except DiceoError:
        raise
    except backend_failures() as exc:
        detail = f"{type(exc).__name__}: {exc}".strip()
        if any(hint in detail.lower() for hint in _ENCRYPTION_HINTS):
            raise EncryptedDocument(detail, source=source) from exc
        if produced == 0:
            raise CorruptDocument(f"nothing could be read ({detail})", source=source) from exc
        report.truncate(f"reader_failed_after_{produced}_blocks: {detail}")


def _pdf_blocks(src: Source, limits: Limits, report: Diagnostics) -> Iterator[Block]:
    from diceo.pdf import blocks as pdf_blocks

    stats: dict = {}
    pages_seen = 0
    try:
        stream = pdf_blocks(
            src.backend_arg,
            tables=limits.detect_tables,
            furniture=limits.suppress_furniture,
            reopen_every=limits.reopen_every,
            # A cap on the pages *read*: checked only against blocks, pages with no
            # text past the limit were still opened to the end of the file.
            page_range=None if limits.max_pages is None else range(limits.max_pages),
            diagnostics=stats,
        )
        for item in stream:
            pages_seen = max(pages_seen, item.page + 1)
            yield Block(
                kind="paragraph" if item.kind == "para" else item.kind,
                text=item.text,
                level=item.level or 0,
                locator=Locator(page=item.page, row=item.row),
            )
        if limits.max_pages is not None and stats.get("page_count", 0) > limits.max_pages:
            report.truncate(f"max_pages={limits.max_pages}")
    finally:
        # In a `finally` so the counts are right even when the caller stops early
        # with `islice` -- a partial read must still report what it saw.
        report.pages = stats.get("pages", pages_seen)
        report.pages_without_text = stats.get("pages_without_text", 0)
        # Running heads and feet are removed, not lost: they repeat on every page, so a
        # copy in every chunk is pure noise (33: 31.6% of chunks carried them). Reported
        # because it is a deliberate removal of text that was in the document.
        removed = stats.get("furniture_lines", 0)
        if removed:
            report.notes.append(f"furniture_lines={removed} (running heads/feet removed)")
        # A `/Rotate` page comes back bottom-to-top from PDFium and was put back into
        # the author's order. Nothing was lost, but the reading order a caller gets is
        # ours rather than the extractor's, and that is exactly the kind of deliberate
        # disagreement rule 3 says must be visible rather than assumed.
        respun = stats.get("pages_reordered", 0)
        if respun:
            report.notes.append(
                f"pages_reordered={respun} (/Rotate pages restored to top-to-bottom order)"
            )
        # How much structure was recovered, and from which signal. `headings=0` on a
        # long document is the observable that catches a producer whose nominal font
        # sizes are all the same number -- for months that produced a whole report
        # with no headings at all and said nothing (experiment 035).
        levels = stats.get("heading_levels", 0)
        source = stats.get("size_source", "nominal")
        if not levels:
            report.notes.append(
                "headings=0 (no heading styles fitted; chunk boundaries fall back "
                "to paragraph and size limits)"
            )
        elif source != "nominal":
            styles = stats.get("heading_styles", levels)
            report.notes.append(
                f"heading_levels={levels} from {styles} styles, measured from rendered "
                f"glyph boxes (this producer's nominal font sizes are all identical)"
            )
        stripped = stats.get("control_chars_removed", 0)
        if stripped:
            # `chunk.text` is written to databases and handed to embedders; PostgreSQL
            # rejects a NUL in a `text` column outright. Removed, and counted, because
            # they were characters in the document.
            report.notes.append(control_chars_note(stripped))
        kept = stats.get("hyphens_kept", 0)
        rejoined = stats.get("hyphens_rejoined", 0)
        if kept:
            # The residual defect, reported because rule 3 covers text that is present
            # and unfindable as much as text that is absent. A word PDFium broke across
            # a line is rejoined only when the same page also spells it solid; when it
            # does not, `revenue-` + `sharing` stays broken and no query matches it.
            report.notes.append(
                f"hyphens_kept={kept} of {kept + rejoined} line-break hyphens "
                f"(no solid spelling on the same page; those words stay split)"
            )
        scanned = stats.get("pages_image_only") or []
        report.pages_image_only = len(scanned)
        if scanned:
            # A truncation, not a note: these pages have content that is not in the
            # index, which is the definition of lost data (rule 3). The page numbers
            # are listed because "route these to OCR" needs to know which ones.
            pages = bounded_list([page + 1 for page in scanned])
            report.truncate(
                f"image_only_pages={len(scanned)} (pages {pages}): these are "
                f"pictures of a page, not text -- they need OCR or their content is "
                f"absent from your index"
            )
        mixed = stats.get("pages_image_mixed") or []
        report.pages_image_mixed = len(mixed)
        if mixed:
            pages = bounded_list([p + 1 for p in mixed])
            report.truncate(
                f"image_content_pages={len(mixed)} (pages {pages}): "
                "substantial images have unread content; route these pages to OCR"
            )
        replacements = stats.get("pages_with_replacement_chars") or {}
        report.pages_unreadable_text = len(replacements)
        if replacements:
            report.truncate(
                f"unreadable_text_pages={len(replacements)} "
                f"(pages {bounded_list([p + 1 for p in replacements])}): "
                f"replacement_chars={stats.get('replacement_chars', 0)}; route to OCR"
            )
        unresolved = stats.get("tables_without_headers", 0)
        if unresolved:
            report.notes.append(
                f"tables_without_headers={unresolved} "
                "(PDF rows retain data without inferred header labels)"
            )


def _ooxml_blocks(src: Source, kind: str, report: Diagnostics) -> Iterator[Block]:
    from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks, iter_pptx_blocks

    stats = OoxmlDiagnostics()
    reader = iter_docx_blocks if kind == "docx" else iter_pptx_blocks
    try:
        for item in reader(src.backend_arg, diagnostics=stats):
            # Counted here rather than in the reader so that `chars` means the same
            # thing for every format: characters that reached the caller. It read
            # 0 for docx and pptx until `diceo file.docx --count` printed it.
            yield Block(
                kind=item.kind,
                text=item.text,
                level=item.level,
                locator=Locator(page=item.part - 1 if item.part else -1, row=item.row),
            )
    finally:
        if stats.main_part_resolved:
            # Recorded by the resolver since it was written, and surfaced by nobody:
            # fourteen other `stats` fields reach `report` from this block and this one
            # did not, so the caller its own comment describes -- someone debugging a
            # document that read strangely -- could never actually see it.
            report.notes.append(
                f"main_part_resolved={stats.main_part_resolved} (the main part was not "
                f"at the conventional name; resolved through the relationships)"
            )
        if stats.parts_missing:
            # A truncation, not a note. `parts_missing` was counted and forwarded the
            # whole time -- as a note, which the handler `chunk`'s own docstring
            # documents (`if report.lost_data:`) cannot see. A .pptx whose slide parts
            # are named in the relationships and absent from the archive therefore
            # produced zero chunks, no exception and `lost_data` False: rule 3's worst
            # case, shipped. The names are listed because "which slide is not in my
            # index" is the only useful next question.
            report.truncate(
                f"parts_missing={len(stats.parts_missing)} "
                f"({bounded_list(stats.parts_missing)}): named by the "
                f"package but unavailable -- their content is absent from your index"
            )
        if stats.slides_without_text:
            report.notes.append(f"slides_without_text={stats.slides_without_text}")
        if stats.media_parts:
            # `images=`, the spelling the HTML and spreadsheet readers already use for
            # the same fact -- the spreadsheet count comes from the same regex over the
            # same archive. `media_parts` stays the field name: there it is the part
            # being counted, here it is the picture the caller does not have.
            report.notes.append(images_note(stats.media_parts))
        if stats.charts or stats.smartart:
            report.notes.append(
                f"charts={stats.charts} smartart={stats.smartart} "
                f"smartart_texts={stats.smartart_texts} (chart values and SmartArt visual "
                f"relationships/unsupported text order are not indexed)"
            )
        # Content that is in the package and deliberately not in the index. Both were
        # found on real Microsoft samples where all five extractors reported no loss.
        if stats.comments:
            report.notes.append(
                f"comments={stats.comments} (review comments in word/comments.xml, not indexed)"
            )
        if stats.unresolved_list_markers:
            report.truncate(
                f"unresolved_list_markers={stats.unresolved_list_markers} "
                "(authored numbering could not be represented; item text is retained)"
            )
        if stats.moved_runs_skipped:
            report.notes.append(
                f"moved_runs_skipped={stats.moved_runs_skipped} (tracked moves: the "
                f"copy at the old location, which would duplicate the text)"
            )
        if stats.fields_unclosed:
            # A malformed field. Worth naming, because before it was bounded a single
            # unclosed `w:fldChar` suppressed every run for the rest of the document.
            report.notes.append(
                f"fields_unclosed={stats.fields_unclosed} (a field was still open when "
                f"its paragraph ended; suppression was reset there)"
            )
        if stats.slides_hidden:
            # Indexed, not dropped -- but a hidden slide is disproportionately a
            # superseded draft, so the caller gets to decide. Same treatment as a
            # hidden sheet.
            listed = bounded_list(stats.slides_hidden)
            report.notes.append(f"slides_hidden=[{listed}] (indexed anyway)")
        if stats.nested_tables:
            report.notes.append(
                f"nested_tables={stats.nested_tables} (inner rows are their own "
                f"table_row blocks; the outer cell keeps its text)"
            )
        if stats.header_parts or stats.footer_parts:
            # A deliberate exclusion, and one a caller has to be able to see: page
            # headers repeat and are noise, right up until the document number a
            # reader is searching for lives only in one.
            report.notes.append(
                f"header_parts={stats.header_parts} footer_parts={stats.footer_parts} "
                f"({stats.header_bytes + stats.footer_bytes:,} bytes of XML, "
                f"deliberately not indexed: they repeat on every page)"
            )
        if stats.textbox_paragraphs:
            # Emitted, not dropped -- but a pull quote is not in the host paragraph's
            # reading order, so a caller reconstructing prose has to be told.
            report.notes.append(
                f"textbox_paragraphs={stats.textbox_paragraphs} (text boxes: indexed "
                f"as their own blocks, outside the host paragraph)"
            )
        if stats.figure_alt_texts_machine:
            # The figure is counted by `media_parts`; this says the only description
            # it carried was Word's, which describes pixels rather than meaning.
            report.notes.append(
                f"figure_alt_texts_machine={stats.figure_alt_texts_machine} "
                f"(machine-written alt text, not indexed) "
                f"figure_alt_texts={stats.figure_alt_texts} (authored, indexed)"
            )
        report.pages_without_text = len(getattr(stats, "slides_without_text", ()) or ())
        if kind == "pptx":
            report.pages = getattr(stats, "slides", 0) or report.pages


def _text_blocks(src: Source, report: Diagnostics) -> Iterator[Block]:
    from diceo.plaintext import iter_text_blocks

    src.handle.seek(0)
    yield from iter_text_blocks(src.handle, report)


def _html_blocks(src: Source, report: Diagnostics) -> Iterator[Block]:
    from diceo.plaintext import _canonical_encoding, iter_html_blocks

    src.handle.seek(0)
    encoding = _canonical_encoding(src.encoding) if src.encoding else None
    yield from iter_html_blocks(src.handle, report, encoding=encoding)


def _email_blocks(src: Source, report: Diagnostics) -> Iterator[Block]:
    from diceo.plaintext import iter_email_blocks

    src.handle.seek(0)
    yield from iter_email_blocks(src.handle, report)


_BLOCK_READERS = {
    "docx": lambda src, limits, report: _ooxml_blocks(src, "docx", report),
    "pptx": lambda src, limits, report: _ooxml_blocks(src, "pptx", report),
    "text": lambda src, limits, report: _text_blocks(src, report),
    "html": lambda src, limits, report: _html_blocks(src, report),
    "eml": lambda src, limits, report: _email_blocks(src, report),
}

#: Formats whose retrieval unit is a header-carrying row group rather than a
#: block, so they are reachable through `chunk` and not through `extract` (D6).
_TABULAR = frozenset({"xlsx", "xls", "xlsb", "ods", "csv"})


def _no_block_form(kind: str, source: str) -> UnsupportedFormat:
    return UnsupportedFormat(
        f"a {kind} has no block form -- its retrieval unit is a row group with "
        f"its header attached, which is a chunking decision (D6). Use "
        f"diceo.chunk()",
        source=source,
        detected=kind,
    )


def _open(
    source: Readable, name: str, report: Diagnostics, limits: Limits
) -> tuple[ExitStack, Source, str]:
    """Everything decidable before a byte of *content* is read, done eagerly.

    :func:`chunk` and :func:`extract` return generators, so without this the whole
    library is lazy to a fault: ``diceo.chunk("typo.pdf")`` would hand back a
    perfectly healthy iterator and raise :class:`DocumentNotFound` later, at the
    caller's ``for`` loop. That puts the exception outside the ``try`` in the
    obvious way to write the call::

        try:
            pieces = diceo.chunk(path)     # nothing happens here
        except diceo.DiceoError:
            quarantine(path)
        for piece in pieces:                 # ... and it raises *here*
            index(piece)

    Measured before this existed: **every** ordinary failure surfaced late -- missing
    file, a directory, a zero-byte crawl artifact, a truncated download, a .zip
    renamed to .docx. Those are not exotic; they are what a crawl is made of.

    So the open, the stat guards and the format sniff happen at the call, and only
    failures that need document *content* stay lazy -- which is the honest place for
    them, because finding them requires reading the thing. Nothing extra is done:
    this is the same work, moved earlier.

    The open :class:`Source` is handed to the generator inside an :class:`ExitStack`,
    which closes when the generator is exhausted, closed, or garbage-collected.
    """
    stack = ExitStack()
    try:
        src = stack.enter_context(
            open_source(
                source,
                download_timeout=limits.download_timeout,
                max_download_bytes=limits.max_download_bytes,
            )
        )
        if name:
            src.name = name
        kind = detect(src)
        if kind == "html" and src.encoding:
            head = src.handle.read(4)
            src.handle.seek(0)
            if head.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
                # HTML's BOM precedes the transport charset, which precedes meta.
                src.encoding = None
            else:
                from diceo.plaintext import _canonical_encoding

                try:
                    b"\0".decode(_canonical_encoding(src.encoding), errors="replace")
                except LookupError as exc:
                    raise CorruptDocument(
                        "HTTP charset is not a text encoding", source=src.url
                    ) from exc
    except BaseException:
        stack.close()
        raise
    report.format = kind
    # Whatever the sniff had to decide *against* the file's own name -- a .xlsx that
    # holds CSV text, a .html that is a MIME archive. It is invisible in the chunks,
    # which look perfectly healthy, and it is a fact about the caller's corpus.
    report.notes.extend(src.notes)
    return stack, src, kind


def _reader_stream(
    src: Source, kind: str, limits: Limits, report: Diagnostics
) -> Iterator[Block]:
    """Select and guard a reader without owning public block accounting."""
    report.format = kind
    if kind == "pdf":
        stream = _pdf_blocks(src, limits, report)
    elif kind in _BLOCK_READERS:
        stream = _BLOCK_READERS[kind](src, limits, report)
    elif kind in _TABULAR:  # pragma: no cover - extract() refuses these eagerly
        raise _no_block_form(kind, src.name)
    else:  # pragma: no cover - detect() only returns known formats
        raise UnsupportedFormat(f"no reader for {kind}", source=src.name, supported=SUPPORTED)

    return _guard(stream, src.name, report)


def _blocks(src: Source, kind: str, limits: Limits, report: Diagnostics) -> Iterator[Block]:
    """The counted stream exposed by :func:`extract`."""
    for block in _reader_stream(src, kind, limits, report):
        report.blocks += 1
        yield block


def extract(
    source: Readable,
    *,
    limits: Limits | None = None,
    diagnostics: Diagnostics | None = None,
    name: str = "",
) -> Iterator[Block]:
    """Stream a document as :class:`Block` objects, lazily and in bounded memory.

    >>> from diceo import extract
    >>> for block in extract("report.pdf"):        # doctest: +SKIP
    ...     print(block.kind, block.text[:60])

    Args:
        source: a path, ``bytes``, or a seekable binary file object.
        limits: budgets -- see :class:`~diceo.types.Limits`.
        diagnostics: filled in as the iterator is consumed; read it *after* the
            loop. Pass one whenever you care what was skipped, which is always.
        name: a file name for a ``bytes`` or stream source. Used for the document
            id and as a hint for the formats that have no magic bytes (CSV).

    Spreadsheets have no block form -- their retrieval unit is a group of rows with
    the sheet's header line repeated onto it, which is a chunking decision rather
    than an extraction one. Use :func:`chunk` for those.

    Raises:
        DocumentNotFound: nothing readable at that path.
        UnsupportedFormat: a format with no block form, or none diceo reads.
        EncryptedDocument: password-protected.
        CorruptDocument: not the format its bytes claim to be.
        SourceNotSeekable: a file object that cannot seek, which only a stream
            source can be -- a path and ``bytes`` never raise it.

    The source is opened and the format sniffed **by this call**, so a missing path,
    a directory, an unreadable archive and an unsupported format all raise here.
    :class:`CorruptDocument` and :class:`EncryptedDocument` may also arrive from the
    iterator, for the damage only visible once content is read -- so put the handler
    around the loop. Failures found part-way through land in ``diagnostics`` rather
    than raising; see :func:`_guard`.
    """
    limits = limits or Limits()
    report = diagnostics if diagnostics is not None else Diagnostics()
    stack, src, kind = _open(source, name, report, limits)
    if kind in _TABULAR:
        stack.close()
        raise _no_block_form(kind, src.name)
    return _extract_stream(stack, src, kind, limits, report)


def _budgeted(stream: Iterator[Block], limits: Limits, report: Diagnostics) -> Iterator[Block]:
    """Enforce ``max_chars`` and ``max_seconds`` on a raw block stream.

    ``chunk()`` has always honoured both, in :func:`~diceo.chunker.chunk_blocks`.
    ``extract()`` does not go through the chunker, so it honoured **neither**: a caller
    who set ``max_chars=50_000`` and passed a 100 MB document got the whole thing, with
    ``truncated`` empty to say everything was fine. A budget that is silently not applied
    is worse than no budget, because the caller sized their pipeline around it.
    """
    if limits.max_chars is None and limits.max_seconds is None:
        yield from stream
        return
    deadline = None
    if limits.max_seconds is not None:
        from time import monotonic

        deadline = monotonic() + limits.max_seconds
    seen = 0
    for block in stream:
        # Checked when the *next* block arrives, so a document that fits exactly is
        # not reported as truncated -- `lost_data` for nothing lost.
        if limits.max_chars is not None and seen >= limits.max_chars:
            report.truncate(f"max_chars={limits.max_chars}")
            return
        yield block
        seen += len(block.text)
        if deadline is not None and monotonic() > deadline:
            report.truncate(f"max_seconds={limits.max_seconds}")
            return


def _extract_stream(
    stack: ExitStack, src: Source, kind: str, limits: Limits, report: Diagnostics
) -> Iterator[Block]:
    with stack:
        chars = 0
        try:
            for block in _budgeted(_blocks(src, kind, limits, report), limits, report):
                chars += len(block.text)
                report.chars = chars
                yield block
        finally:
            report.chars = chars


def _sheet_chunks(
    src: Source,
    kind: str,
    limits: Limits,
    report: Diagnostics,
    doc_id: str = "",
    extra: Mapping[str, object] | None = None,
) -> Iterator[Chunk]:
    from diceo.sheets import SheetDiagnostics, iter_sheet_chunks

    stats = SheetDiagnostics()
    rows = None
    if kind == "csv":
        from diceo import delimited

        src.handle.seek(0)
        rows = delimited.iter_rows(
            src.handle,
            sheet=src.doc_id,
            diagnostics=stats,
            max_rows=limits.max_rows,
        )
    elif kind != "xlsx":
        from diceo import legacy_sheets

        rows = legacy_sheets.iter_rows(
            src.backend_arg,
            kind=kind,
            diagnostics=stats,
            max_rows=limits.max_rows,
            # A stream has no path of its own, so without this the refusal for a
            # locked or unreadable legacy workbook says `<stream>` -- to the caller
            # who passed `name=` precisely so that it would not.
            name=src.name,
        )

    index = 0
    produced_chars = 0
    deadline = None
    if limits.max_seconds is not None:
        from time import monotonic

        deadline = monotonic() + limits.max_seconds
    try:
        for item in iter_sheet_chunks(
            src.backend_arg if rows is None else src.name,
            target=limits.target_chars,
            max_rows=limits.max_rows,
            diagnostics=stats,
            rows=rows,
        ):
            # Before the chunk, not after: only a chunk that is really withheld
            # makes this a truncation.
            if limits.max_chars is not None and produced_chars >= limits.max_chars:
                report.truncate(f"max_chars={limits.max_chars}")
                break
            index += 1
            produced_chars += len(item.text)
            report.chunks = index
            report.chars = produced_chars
            yield Chunk(
                text=item.text,
                index=index - 1,
                doc_id=doc_id or src.doc_id,
                title=item.sheet,
                kinds=("sheet_summary",) if item.kind == "sheet_summary" else ("sheet_row",),
                locator=Locator(sheet=item.sheet, row=item.first_row),
                extra=extra,
            )
            # The sheet path does not go through `chunk_blocks`, which is where
            # `max_chars` was enforced, so a spreadsheet ignored it entirely: 50,000
            # asked for, 6,199,081 delivered, `truncated` empty.
            if deadline is not None and monotonic() > deadline:
                report.truncate(f"max_seconds={limits.max_seconds}")
                break
    except DiceoError:
        raise
    except backend_failures() as exc:
        detail = f"{type(exc).__name__}: {exc}".strip()
        if index == 0:
            raise CorruptDocument(f"nothing could be read ({detail})", source=src.name) from exc
        report.truncate(f"reader_failed_after_{index}_chunks: {detail}")
    finally:
        report.chunks = index
        report.rows = stats.rows
        report.sheets = stats.sheets
        # Characters, not cells: `chars` means the same thing for every format or
        # it means nothing. `cells` is reported separately by the sheet reader.
        report.chars = produced_chars
        for what, value, _ in stats.truncated:
            report.truncate(f"{what}={value}")
        for note in stats.notes:
            report.notes.append(note)
        for empty in stats.sheets_without_rows:
            report.notes.append(f"empty_sheet={empty}")
        # Read, never dropped (rule 3) -- but a caller filtering by freshness or ACL
        # has to be able to tell. "Very hidden" cannot be un-hidden from Excel's UI at
        # all, so it is disproportionately internal data; the state is reported so a
        # caller can treat the two differently. Real workbooks lean on this: the UK
        # fire statistics file ships five hidden sheets worth 20.8% of its characters.
        for sheet, state in stats.sheets_hidden:
            report.notes.append(f"hidden_sheet={sheet} ({state})")
        # A merged range holds its value in the anchor row only, so the rows under it
        # are read blank in that column. Reported rather than forward-filled: on the
        # five real workbooks in the fixture set only 2 of 63 ranges are both
        # vertical and in the body, and both are wrapped footnotes whose
        # continuation rows are empty anyway.
        for sheet, count in stats.vertical_merges:
            report.notes.append(
                f"vertical_merges={count} in {sheet} (the merged value is read in its "
                f"top row only; rows below it are blank in that column)"
            )
        for broken in stats.sheets_without_part:
            report.truncate(f"unreadable_sheet={broken}")
        # Four counters the reader has always kept and nobody could ever read. Each
        # one is a cell the reader deliberately blanked, which is precisely the case
        # rule 3 exists for: the value is gone from the caller's index and, until
        # this loop, nothing anywhere said so. `error_cells` is a loss (#REF!, #N/A --
        # the cell had a value once); the rest are conditions a caller triaging a
        # corpus needs to be able to sort on.
        if stats.error_cells:
            report.truncate(f"error_cells={stats.error_cells}")
        if stats.invalid_cell_references:
            report.truncate(
                f"invalid_cell_references={stats.invalid_cell_references} (a cell named "
                f"no column, or one past the XFD maximum; its value was kept but its "
                f"position was not)"
            )
        if stats.shared_string_misses:
            report.truncate(
                f"shared_string_misses={stats.shared_string_misses} (a cell's "
                f"shared-string index was out of range, so its text is unreachable)"
            )
        if stats.control_chars_removed:
            report.notes.append(control_chars_note(stats.control_chars_removed))
        if stats.formula_cells_unevaluated:
            # Not a loss: a workbook written by a library caches no results, and no
            # reader short of a spreadsheet engine can do better. A note, so a caller
            # seeing a high count knows the file is a template rather than data.
            report.notes.append(
                f"formula_cells_unevaluated={stats.formula_cells_unevaluated} (no "
                f"cached result in the file; diceo does not evaluate formulas)"
            )
        for sheet, declared, seen in stats.dimension_mismatch:
            report.notes.append(
                f"dimension_mismatch in {sheet}: the file declares {declared} rows "
                f"and the stream delivered {seen}. Neither is corrected -- either "
                f"could be the right one"
            )


def to_text(blocks: Iterable[Block]) -> Iterator[str]:
    """Render blocks as near-markdown text, one piece per block, for a text chunker.

    >>> from diceo import extract, to_text
    >>> text = "".join(to_text(extract("report.pdf")))   # doctest: +SKIP

    Headings get ``#`` by level, because a block's level is a field and a plain join
    loses it. Everything else already carries its own markup -- ``- `` and ``1. `` on
    list items, `` | `` between table cells -- so it passes through as it is. Lazy:
    ``"".join`` is the caller's choice, not ours. Costs ~0.7% on top of `extract`.

    Blocks are separated by a blank line, except consecutive table rows: a table is
    one unit, and a blank line between its rows would split it for any chunker.
    """
    previous = ""
    for block in blocks:
        text = block.text
        if block.is_heading:
            text = f"{'#' * min(max(block.level, 1), 6)} {text}"
        if previous:
            gap = (
                "\n"
                if previous == block.kind == "table_row" and block.locator.row != 0
                else "\n\n"
            )
            text = gap + text
        previous = block.kind
        yield text
    if previous:
        yield "\n"


def chunk(
    source: Readable,
    *,
    limits: Limits | None = None,
    diagnostics: Diagnostics | None = None,
    name: str = "",
    doc_id: str = "",
    meta: Mapping[str, object] | None = None,
) -> Iterator[Chunk]:
    """Stream a document as retrieval-ready :class:`Chunk` objects. The whole package.

    >>> import diceo
    >>> for piece in diceo.chunk("report.pdf"):            # doctest: +SKIP
    ...     index(piece.embed_text, meta=piece.meta)

    Works on every format in :data:`diceo.FORMATS`, from a path,
    ``bytes``, or a seekable file object::

        diceo.chunk(response.content, name="report.pdf")

    Pass ``diagnostics=Diagnostics()`` to find out what was skipped -- pages with
    no text layer, limits that bit, mail attachments not followed, parts that were
    missing. It is filled as the iterator is consumed, so read it *after* the loop::

        report = diceo.Diagnostics()
        pieces = list(diceo.chunk(path, diagnostics=report))
        if report.lost_data:
            log.warning("incomplete: %s", report.as_dict())

    Raises:
        DocumentNotFound: no readable file at that path.
        UnsupportedFormat: a format diceo cannot read; the message names it.
        EncryptedDocument: password-protected.
        CorruptDocument: nothing at all could be read.
        SourceNotSeekable: a file object that cannot seek, which only a stream
            source can be -- a path and ``bytes`` never raise it.

    Those five are the complete set, and they share the base class
    :class:`~diceo.errors.DiceoError`.

    :class:`DocumentNotFound` and :class:`UnsupportedFormat` are raised **by this
    call**, along with the :class:`CorruptDocument` cases visible from the container
    alone -- an empty file, a truncated archive, a ZIP holding no document. So the
    ordinary crawl failures land where the call is written.

    :class:`CorruptDocument` and :class:`EncryptedDocument` can **also** arrive from
    the iterator, because some of what makes a document unreadable is only visible
    once its content is read: an encrypted PDF is a well-formed PDF. Put the handler
    around the loop, not around the call::

        try:
            for piece in diceo.chunk(path):
                index(piece)
        except diceo.DiceoError as exc:
            quarantine(path, str(exc))

    That form catches both. A document that fails only *part-way* through is not an
    exception at all (rule 3): the chunks read before it are yielded, the reason is
    recorded in ``diagnostics.truncated``, and ``lost_data`` becomes True.
    """
    limits = limits or Limits()
    report = diagnostics if diagnostics is not None else Diagnostics()
    stack, src, kind = _open(source, name, report, limits)
    return _chunk_stream(stack, src, kind, limits, report, doc_id, meta)


def _chunk_stream(
    stack: ExitStack,
    src: Source,
    kind: str,
    limits: Limits,
    report: Diagnostics,
    doc_id: str,
    meta: Mapping[str, object] | None,
) -> Iterator[Chunk]:
    with stack:
        if src.url:
            meta = {"source_url": src.url, **(meta or {})}
        if kind in _TABULAR:
            yield from _sheet_chunks(src, kind, limits, report, doc_id, meta)
            return
        chars = 0
        try:
            for item in chunk_blocks(
                _reader_stream(src, kind, limits, report),
                doc_id=doc_id or src.doc_id,
                limits=limits,
                diagnostics=report,
                extra=meta,
            ):
                chars = item.char_end
                report.chars = chars
                yield item
        finally:
            report.chars = chars


__all__ = ["chunk", "extract", "sniff", "to_text"]
