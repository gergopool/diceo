"""The public vocabulary: ``Block``, ``Chunk``, ``Limits``, ``Diagnostics``, and the
``Locator`` and ``Kind`` those are described in terms of.

Three of them are types a caller constructs or receives; ``Locator`` and ``Kind``
exist because ``Block`` and ``Chunk`` have to say what their fields are, and a field
whose type has no name cannot be annotated. The public surface is deliberately tiny,
and adding a name to it is a decision that gets recorded rather than a convenience
that gets taken -- so this module is small and boring on purpose.

``Block`` is the narrow waist of the one streaming pipeline: every cracker yields
these lazily and the chunker consumes them lazily, so nothing holds a whole document.
Each format has its own richer internal block type; this is the common shape they all
project onto.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal, NamedTuple

Kind = Literal[
    "heading",
    "paragraph",
    "list_item",
    "table_row",
    "caption",
    "footnote",
    "sheet_row",
    "sheet_summary",
    "slide_title",
    "note",
]


class Locator(NamedTuple):
    """Where a block came from, in terms the source format understands.

    Kept as a NamedTuple because there is one per block and a 2,400-page report
    produces a quarter of a million of them.
    """

    page: int = -1
    """0-based page (pdf) or slide (pptx). ``-1`` when the format has no pages."""

    sheet: str = ""
    """Worksheet name, for the spreadsheet formats. Empty for everything else."""

    row: int = -1
    """Row within the current table or sheet. ``-1`` outside one."""


class Block(NamedTuple):
    """One piece of text plus what is needed to place it and classify it."""

    kind: str
    """What this piece of text is. One of ``heading``, ``paragraph``, ``list_item``,
    ``table_row``, ``caption``, ``footnote``, ``endnote``, ``slide_title`` (pptx) or
    ``note`` (pptx speaker notes). Not every format produces every kind."""

    text: str
    """The text itself, whitespace-normalised by the reader that produced it."""

    level: int = 0
    """Heading level 1..6, or list depth for a ``list_item``. 0 when not applicable."""

    locator: Locator = Locator()
    """Where in the source document this came from."""

    @property
    def is_heading(self) -> bool:
        """True for the kinds that open a section: ``heading`` and ``slide_title``.

        The chunker treats both the same way -- a slide title is a heading that
        happens to be one slide deep.
        """
        return self.kind in ("heading", "slide_title")


@dataclass(slots=True)
class Chunk:
    """A retrieval-sized piece of one document.

    ``text`` is what to index. It already contains any headings that belong to it --
    and that is measured, not a preference: heading *text* is worth 10.6-12.3pp
    because 12.8% of facts live in headings, while prepending a breadcrumb to a chunk
    that already carries them *costs* 3.1pp. So ``title`` and ``breadcrumb`` are
    carried as metadata for the caller's UI, and are deliberately **not** part of
    ``text``.
    """

    text: str
    """What to index, headings included. Hand it to an embedder as
    :attr:`embed_text`; store the rest as :attr:`meta`."""

    index: int
    """0-based position of this chunk in its document, in reading order."""

    doc_id: str = ""
    """Which document this came from: the caller's ``doc_id=`` if they passed one,
    otherwise the file name without its extension."""

    title: str = ""
    """The outermost heading in scope -- usually the document's own title, and the
    worksheet name for a spreadsheet. Metadata for display and filtering, not part
    of ``text``."""

    breadcrumb: str = ""
    """The headings between :attr:`title` and this chunk, joined with ``" > "``.
    Empty when the chunk sits directly under the title."""

    kinds: tuple[str, ...] = ()
    """Which :attr:`Block.kind` values went into this chunk, in first-seen order --
    so ``("heading", "paragraph")`` is prose and ``("table_row",)`` is a row group.
    Spreadsheets use ``sheet_row`` and ``sheet_summary``, which no ``Block`` carries.
    Useful as an index filter: tables and prose answer different questions."""

    locator: Locator = Locator()
    """Where the chunk *starts*: the locator of its first block."""

    char_start: int = 0
    """Where this chunk starts in the running concatenation of the chunk texts of
    this document -- **not** an offset into the source file. A PDF or a spreadsheet
    has no character stream to point into, so the origin is the output: the first
    chunk starts at 0 and every later one starts where its predecessor ended, which
    makes the pair a stable, orderable position rather than a citation into the file.

    Consequences worth knowing. ``chunk.text`` is exactly
    ``char_end - char_start`` characters long, so the spans tile the document with no
    gaps. With ``Limits(overlap_chars=...)`` the repeated text is counted in both
    chunks, so the total runs longer than the document. Spreadsheets do not set
    these at all -- their unit is a row group, and a row's position is
    ``locator.sheet`` and ``locator.row`` -- so both stay 0 and :attr:`meta` omits
    them."""

    char_end: int = 0
    """One past the last character of this chunk, in the same coordinates as
    :attr:`char_start`."""

    extra: Mapping[str, object] | None = None
    """Whatever the caller passed as ``chunk(..., meta={...})``, merged into
    :attr:`meta`. Anything: a tenant id, a source URL, an ACL, a crawl timestamp,
    a nested dict of their own.

    Held **by reference, not copied**, so a one-million-row spreadsheet costs one
    dictionary rather than a million. The consequence, and it is the caller's to
    manage: mutating the mapping after the call changes what every chunk reports.
    Pass a fresh dict per document and treat it as frozen.
    """

    def __len__(self) -> int:
        return len(self.text)

    @property
    def meta(self) -> dict[str, object]:
        """Everything except the text, ready to store beside a vector.

        Flat, JSON-serialisable, and stable: these keys are part of the public API
        because they end up as column names in somebody's index. Empty and ``-1``
        fields are dropped rather than stored, since a locator's ``page=-1`` means
        "not applicable" and writing that into every row of a spreadsheet index is
        noise the caller then has to filter.

        The caller's own :attr:`extra` keys are merged **last and therefore win**.
        That is deliberate: a caller who passes ``doc_id`` means the identifier
        their system uses, and quietly preferring our guess from the file name
        would be the wrong answer every time.
        """
        data: dict[str, object] = {"index": self.index}
        if self.doc_id:
            data["doc_id"] = self.doc_id
        if self.title:
            data["title"] = self.title
        if self.breadcrumb:
            data["breadcrumb"] = self.breadcrumb
        if self.kinds:
            data["kinds"] = list(self.kinds)
        if self.locator.page >= 0:
            data["page"] = self.locator.page
        if self.locator.sheet:
            data["sheet"] = self.locator.sheet
        if self.locator.row >= 0:
            data["row"] = self.locator.row
        if self.char_end:
            data["char_start"] = self.char_start
            data["char_end"] = self.char_end
        if self.extra:
            data.update(self.extra)
        return data

    @property
    def embed_text(self) -> str:
        """What to hand an embedding model. Identical to ``text`` by default.

        A separate name because the two are allowed to diverge later -- a breadcrumb
        does help when upstream structure was *lost*, worth +4.4pp on chunks with no
        headings -- and callers should not have to change which attribute they read
        if that default ever changes.
        """
        return self.text


@dataclass(slots=True)
class Limits:
    """Budgets the caller sets, because a caller who indexes a SharePoint library
    will meet a 1M-row sheet and a 2,400-page PDF and must be able to say "not that
    one".

    Every limit that actually bites is reported in ``diagnostics`` -- silently
    truncating is the failure mode this package exists to avoid (rule 3).
    """

    target_chars: int = 1800
    """Chunk size aim, in characters -- a soft target the chunker packs up to rather
    than a hard cut. 1800 is the conservative default: 600 measured about 8pp better
    on the corpus this was tuned on, and that margin has not been reproduced across
    corpora, so it is not the default. If you are tuning for retrieval, ``600`` is
    the first value to try."""

    table_chars: int | None = None
    """Size aim for table row-groups; defaults to ``target_chars``. Smaller values
    make table cells much easier to find and cost prose recall -- a measured
    trade-off, so it is a dial rather than a constant."""

    list_group_size: int = 0
    """At most this many consecutive list items per chunk; ``0`` disables the rule.

    A chunk holding several bullets is a worse retrieval target for a question about
    *one* of them, because every sibling pulls the chunk vector elsewhere. Experiment
    047 measured the same dose-response in diceo's output and in Unstructured's --
    1.000 at one bullet, 0.857 at three, 0.550 at five or more -- which is what makes
    it a property of the retriever rather than of either tool.

    **Zero by default until 048 settles it**, because the promising value was chosen
    on the corpus that produced the deficit, and capping the group makes more, smaller
    chunks. Experiments 023 and 025 both established that `gold@5` is not comparable
    across chunk sizes at a fixed nominal target, so a win here has to beat a
    size-matched control rather than the default. Set it to run that arm."""

    overlap_chars: int = 0
    """Characters of the previous chunk repeated at the start of the next. The
    standard RAG trick, for facts that straddle a boundary. Off by default until
    measured -- it inflates the index by roughly ``overlap/target``, and adding
    chunks was measured to cost prose recall through distractor pressure."""

    max_pages: int | None = None
    """Stop after this many PDF pages. ``None`` for no bound."""

    max_rows: int | None = None
    """Stop after this many rows, counted **per sheet** for a workbook. ``None`` for
    no bound."""

    max_chars: int | None = None
    """Stop once this many characters have reached the caller. ``None`` for no
    bound."""

    max_seconds: float | None = None
    """Wall-clock budget for this document. Checked at block granularity, so a
    pathological page cannot blow it -- and reported like every other limit rather
    than performed quietly.

    A caller draining a queue needs *this* rather than a page count: they know they
    can spend two seconds on a document, and they do not know in advance which
    document is the 2,400-page one. Without it the only options are a timeout that
    kills the worker (losing the chunks already produced) or no bound at all."""

    reopen_every: int = 100
    """Pages between closing and reopening a PDF. PDFium caches every parsed
    indirect object for the document's lifetime with no public purge, so without this
    peak RSS reaches 1,395 MB on a 1,500-page file against 173 MB with it. Measured
    cost: 2-16%, and negative on some files. 0 disables."""

    detect_tables: bool = True
    """Recover table rows from geometry and numeric density. Measured worth: it takes
    ``wide_table_cell`` retrieval from 0.000 to 0.538."""

    suppress_furniture: bool = False
    """Remove a PDF's running heads and feet -- the repeated document code, title and
    page number that sit in every page's margin.

    **Off by default, and the reason is a measurement that went against it.** Turning it
    on removes real noise: chunks carrying furniture drop from 31.6% to 1.8%, page
    footers stop being mistaken for table rows (866 of 3,294 `table_row` blocks were the
    footer), 5.8% of characters go, and the boilerplate stops appearing spliced into the
    middle of 120 sentences.

    But held-out PDF `gold@5` falls **0.845 -> 0.836**, and the effect is monotone in how
    much is suppressed (1,545 lines -> 0.840; 2,219 lines -> 0.836). That is enough to
    take PDF from -0.9pp against PyMuPDF4LLM, which satisfies the 1pp
    equivalence threshold, to -1.8pp, which does not. Rule 4
    decides: a change that costs retrieval does not become the default.

    The likely mechanism is that the running head *is* a document-level identifier -- code
    plus title -- on chunks that otherwise carry none, so deleting it removes context that
    was accidentally useful. Making this the default needs that context supplied
    deliberately instead."""

    download_timeout: float = 30.0
    """Socket timeout in seconds for explicitly supplied HTTP(S) URLs."""

    max_download_bytes: int = 128 * 1024**2
    """Maximum downloaded/decompressed URL body size; local files are unaffected."""

    #: Below this a chunk cannot hold a word, and the splitter cannot make progress.
    #: Not a taste judgement -- 8 is already useless for retrieval -- but the point at
    #: which the arithmetic stops working.
    MIN_TARGET_CHARS: ClassVar[int] = 8

    def __post_init__(self) -> None:
        """Reject a configuration that cannot terminate, at the point it is written.

        ``Limits(target_chars=0)`` used to make ``chunk()`` **spin forever**: the split
        loop takes a zero-length window, finds no boundary in it, cuts at ``target - 1 ==
        -1`` and leaves ``rest`` exactly as long as it was. No exception, no memory
        growth, no progress -- and the CLI exposed it as ``diceo --target 0``. A hang is
        the worst thing a library can do to an indexing pipeline, because one bad
        configuration stalls the worker instead of failing the document.

        These are the *caller's* values rather than the document's, so they raise
        ``ValueError`` here at construction and not a ``DiceoError`` later: a bad limit
        is a bug in the calling code, and reporting it as "your document is corrupt" would
        send it to entirely the wrong person.
        """
        if self.target_chars < self.MIN_TARGET_CHARS:
            raise ValueError(
                f"target_chars={self.target_chars} is below the minimum of "
                f"{self.MIN_TARGET_CHARS}; a chunk that small cannot hold a word and the "
                f"splitter cannot make progress"
            )
        if self.table_chars is not None and self.table_chars < self.MIN_TARGET_CHARS:
            raise ValueError(
                f"table_chars={self.table_chars} is below the minimum of "
                f"{self.MIN_TARGET_CHARS}"
            )
        for field_name in (
            "list_group_size",
            "overlap_chars",
            "max_pages",
            "max_rows",
            "max_chars",
            "reopen_every",
        ):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name}={value} must not be negative")
        if self.max_seconds is not None and self.max_seconds <= 0:
            raise ValueError(
                f"max_seconds={self.max_seconds} must be positive; use None for no budget"
            )
        if not 0 < self.download_timeout < float("inf"):
            raise ValueError("download_timeout must be finite and positive")
        if self.max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")

    def resolved_table_chars(self) -> int:
        """The size aim to use for table row-groups, with the default filled in.

        ``table_chars`` when the caller set one, ``target_chars`` otherwise -- so a
        reader never has to special-case the ``None``.
        """
        return self.target_chars if self.table_chars is None else self.table_chars


@dataclass
class Diagnostics:
    """What was dropped, skipped or guessed. Never empty for a real document.

    Rule 3: a document that silently is not in the index is the worst failure in this
    domain, so every count here exists to make that impossible to miss.
    """

    format: str = ""
    """What the file turned out to be, decided by content rather than extension.
    Worth logging on its own: a ``.xlsx`` that reports ``docx`` here is a mislabelled
    file, and that is a fact about the caller's corpus they will want."""

    pages: int = 0
    """Pages in the document (pdf) or slides (pptx). 0 for the formats that have
    neither."""

    pages_without_text: int = 0
    """Of those, how many yielded no text at all. Some are genuinely blank; the ones
    that are pictures of text are counted again, more usefully, by
    :attr:`pages_image_only`."""

    pages_image_only: int = 0
    """Pages with no text that *do* draw an image: pictures of text, needing OCR.
    The distinction from `pages_without_text` is the whole point -- a blank
    separator page is fine, a scanned page is a document missing from the index."""

    chars: int = 0
    """Characters that reached the caller. The same thing for every format, which is
    why it is worth comparing against the file's size when a document looks thin."""

    blocks: int = 0
    """How many :class:`Block` objects were read. 0 on the spreadsheet formats,
    whose rows go straight to chunks without ever taking a block form."""

    chunks: int = 0
    """How many :class:`Chunk` objects have been emitted. Final only once the
    iterator is exhausted."""

    sheets: int = 0
    """Worksheets read, for the spreadsheet formats."""

    rows: int = 0
    """Spreadsheet or CSV rows read, across every sheet."""

    truncated: list[str] = field(default_factory=list)
    """**Everything that is missing from your index**, one string per cause: a limit
    that bit (``"max_pages=100"``), a reader that failed part-way
    (``"reader_failed_after_812_blocks: ..."``), pages that are pictures rather than
    text, a sheet that could not be opened.

    Non-empty means content that was in the document is not in the chunks. This is
    the list the no-silent-loss rule exists to fill, and :attr:`lost_data` is the
    one-line way to test it."""

    notes: list[str] = field(default_factory=list)
    """Facts about the document that are **not** losses but change what the chunks
    mean: hidden sheets, a running head that was removed, an extension that
    disagreed with the content, charts whose numbers live in a part diceo does not
    open. Human-readable, one per observation, and worth logging on an unfamiliar
    corpus."""

    pages_image_mixed: int = 0
    """Pages with substantial unread image content alongside their text layer."""

    pages_unreadable_text: int = 0
    """Pages whose text mapping yielded Unicode replacement characters."""

    def truncate(self, what: str) -> None:
        """Record a cause of lost data, ignoring a repeat of one already recorded.

        Called by the readers; a caller normally only reads :attr:`truncated`.
        """
        if what not in self.truncated:
            self.truncated.append(what)

    @property
    def lost_data(self) -> bool:
        """True if anything was dropped. Cheap for a caller to assert on."""
        return bool(self.truncated) or self.pages_without_text > 0

    @property
    def needs_ocr(self) -> bool:
        """At least one page is a picture of text: OCR would recover content.

        The single most valuable boolean in this class. Every extractor "succeeds"
        on a scanned PDF and returns no text, which is how documents go missing
        while the logs stay green (rule 3).

        Deliberately **not** "every page was blank". A one-page PDF holding a
        genuinely empty separator page has nothing to OCR, and answering True for
        it would send a caller's OCR budget at whitespace. Emptiness is reported
        by `pages_without_text` and by a note; this flag means *there is content
        we could not read*.
        """
        return (
            self.pages_image_only > 0
            or self.pages_image_mixed > 0
            or self.pages_unreadable_text > 0
        )

    def as_dict(self) -> dict:
        """Every counter plus the two verdicts, flat and JSON-serialisable.

        What to write to a log line or a per-document row when something was lost:
        it includes :attr:`lost_data` and :attr:`needs_ocr`, which are properties and
        so would be missing from ``dataclasses.asdict``. The lists are copied, so the
        result does not change under a caller who keeps iterating.
        """
        return {
            "format": self.format,
            "pages": self.pages,
            "pages_without_text": self.pages_without_text,
            "pages_image_only": self.pages_image_only,
            "pages_image_mixed": self.pages_image_mixed,
            "pages_unreadable_text": self.pages_unreadable_text,
            "sheets": self.sheets,
            "rows": self.rows,
            "chars": self.chars,
            "blocks": self.blocks,
            "chunks": self.chunks,
            "truncated": list(self.truncated),
            "notes": list(self.notes),
            "lost_data": self.lost_data,
            "needs_ocr": self.needs_ocr,
        }
