"""Text, Markdown, HTML and email -- the formats with no binary container.

All four are line- or event-streaming readers with no third-party dependency, and
all four yield the same :class:`~diceo.types.Block` stream the PDF and OOXML
crackers do, so the chunker (and every measurement made of it) applies unchanged.

The HTML reader is stdlib ``html.parser`` rather than BeautifulSoup + markdownify,
which is what the permissive competitors use. Not for purity -- both are MIT -- but
because what retrieval wants from HTML is the *block boundaries and the heading
levels* (experiment 020, structure beats the incumbent), and
those are exactly the two things a SAX-style parser gives you for free. Building a
DOM to throw it away is the 90x the markdown converters spend.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import suppress
from html.parser import HTMLParser
from typing import IO, NamedTuple

from diceo._diagnostic_text import control_chars_note, images_note
from diceo.types import Block, Diagnostics, Locator

#: Elements whose character data is not prose. MathML stays on this path, but its
#: authored text alternative is recovered before its presentation tree is skipped.
#:
#: ``head`` is deliberately *absent*: skipping it wholesale also skips ``<title>``,
#: which is a document's most reliable one-line summary. Its other children carry
#: no character data, and ``script``/``style`` are listed here in their own right.
_SKIP = frozenset({"script", "style", "noscript", "svg", "math", "template"})

#: Elements with no end tag. Pushing one onto the ancestor stack would corrupt every
#: depth test after it, which is the classic way a hand-written HTML walker goes wrong.
_VOID = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: HTML5 sectioning content. A `<header>`/`<footer>` inside one of these belongs to *it*,
#: not to the page -- an article's footer holds the author and the DOI.
_SECTIONING = frozenset({"article", "aside", "nav", "section"})
#: HTML5 sectioning roots. Note `<main>` is neither this nor sectioning content, so
#: `body > main > footer` really is the page footer.
_SECTION_ROOTS = frozenset(
    {"body", "blockquote", "details", "dialog", "fieldset", "figure", "td"}
)
#: ARIA landmarks whose author has declared the region to be page furniture. Needed
#: because real pages nest a site footer inside a `<section>`, where the structural rule
#: alone would keep it. `role="navigation"` is deliberately absent: Wikipedia's bottom
#: navboxes carry it and hold real topic terms.
_CHROME_ROLES = frozenset({"banner", "contentinfo"})
#: The only tags `_HtmlBlocks._is_chrome` can answer True to on the strength of the
#: tag alone. Kept beside `_CHROME_ROLES` so the guard in `handle_starttag` and the
#: test it guards cannot drift apart -- a tag added to `_is_chrome` and not to this
#: set would silently stop being detected.
_CHROME_TAGS = frozenset({"nav", "header", "footer"})

#: Elements that end a block of text. Anything not listed is inline, so a
#: ``<span>`` in the middle of a sentence does not split it.
_BLOCK = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "caption",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_HEADINGS = {f"h{level}": level for level in range(1, 7)}
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
#: The same set plus the newline, for HTML content. A source newline is *structure*
#: in a text file, which is why `_WHITESPACE` omits it, but in HTML it is a segment
#: break and a segment break is a space (CSS Text 3 phase 1). Keeping it turned a
#: cell whose source wrapped onto two lines into a `table_row` with a literal newline
#: inside it -- and rows are joined on newlines by the chunker, so that one forged a
#: row boundary the document never had.
_HTML_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")
_META_CHARSET = re.compile(
    rb"""<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9_:.\-]+)""", re.IGNORECASE
)

#: Charset labels that do not mean what they say, from the WHATWG Encoding standard's
#: published label table (https://encoding.spec.whatwg.org/#names-and-labels). Every
#: one of these maps to windows-1252, and the reason is exactly the defect: Python's
#: iso-8859-1 codec cannot fail, so a declared `iso-8859-1` always won the candidate
#: race in `_decode_text` -- while practically every document carrying that label is
#: really cp1252, so its curly quotes, en/em dashes and ellipsis arrived as invisible
#: C1 control characters. Browsers have honoured this mapping since 2012; a table from
#: a spec is a fact about the format, not somebody's code.
_CP1252_LABELS = frozenset(
    {
        "ansi_x3.4-1968",
        "ascii",
        "cp1252",
        "cp819",
        "csisolatin1",
        "ibm819",
        "iso-8859-1",
        "iso-ir-100",
        "iso8859-1",
        "iso88591",
        "iso_8859-1",
        "iso_8859-1:1987",
        "l1",
        "latin1",
        "us-ascii",
        "windows-1252",
        "x-cp1252",
    }
)

#: A `Ã`/`Â`/`â` bigram in the making: a two- or three-byte UTF-8 sequence for a
#: Latin-1-supplement or General-Punctuation character. Read through any single-byte
#: codec it becomes the one shape of mojibake everybody recognises, and a single-byte
#: codec never raises -- so this, plus the whole payload being valid UTF-8, is the only
#: evidence there is that a body labelled latin-1 was written in utf-8.
_MOJIBAKE_BYTES = re.compile(rb"[\xc2\xc3\xe2][\x80-\xbf]")

#: Any byte a single-byte codec and UTF-8 would read differently. Pure ASCII is valid
#: UTF-8 too, and a mail declaring windows-1252 for an ASCII body is the commonest thing
#: in the archive -- so this is what tells "both readings exist" apart from "there is
#: only one reading and two names for it".
_HIGH_BYTE = re.compile(rb"[\x80-\xff]")


#: Minimum share of NUL bytes at one parity before BOM-less UTF-16 is suspected.
#: ASCII text in UTF-16 is ~50% NUL at one parity; 0.30 leaves room for a document
#: that mixes in accented or CJK characters, which carry fewer NULs.
_UTF16_NUL_SHARE = 0.30
#: The other parity must be almost NUL-free, or this is not UTF-16 at all.
_UTF16_WRONG_PARITY_MAX = 0.05
#: Below this many bytes the parity signal is noise. A 2-byte input can look like
#: anything -- chardet answers ISO-8859-1 at 0.73 confidence on one, wrongly.
_UTF16_MIN_SAMPLE = 16
#: Share of decoded characters that must be printable or whitespace before we
#: believe a UTF-16 guess. The NUL ratio alone also matches binary payloads, so the
#: guess is confirmed by the text it produces rather than by the bytes going in.
_UTF16_SANE_SHARE = 0.90

#: C0 controls that carry no text. Tab and newline are kept; carriage return is kept
#: because the readers normalise line endings themselves. Form feed (0x0C) is kept
#: deliberately: it is the de-facto page separator in extracted PDF text, so
#: dropping it here would destroy a boundary signal.
#:
#: C1 (0x80-0x9F) is here for a different reason. Nothing authors those characters on
#: purpose, so one in the output means a byte was read through a codec that mapped it
#: nowhere -- a genuinely Latin-1 document whose curly quotes are now invisible. They
#: cost a caller an embedded character that no query can ever match and that no eye can
#: see in the source; removed and counted, the same accident becomes a
#: `control_chars_removed` number rule 3 says the caller is owed.
#:
#: A pattern rather than the `str.translate` mapping this used to be, because
#: `translate` over a mapping is a dict lookup *per character*: 4.9 ms per 64 KiB
#: piece, measured here. That was affordable while only the first piece of an HTML
#: document was cleaned, and it stopped being affordable the moment every piece was --
#: 60% of the HTML reader's whole runtime on a 13.7 MB page. `re.sub` scans at C speed
#: and returns the *same string object* when it matches nothing, which is the common
#: case, and costs 0.25 ms for the same piece.
_CONTROL_CODES = (*range(0x00, 0x09), 0x0B, *range(0x0E, 0x20), 0x7F, *range(0x80, 0xA0))
_STRIP_CONTROLS = re.compile("[" + "".join(f"\\x{code:02x}" for code in _CONTROL_CODES) + "]")


#: Characters that occupy no space and carry no meaning *on their own*.
#:
#: `str.strip()` removes NBSP, NNBSP and U+3000 -- they are `isspace()` -- but not
#: these, so a line holding nothing but a zero-width space is "non-empty" to Python
#: and becomes its own block: a chunk of pure invisible characters that gets
#: embedded, stored, searched and can be *returned* as a hit. They arrive constantly
#: in text exported from HTML and from Word.
#:
#: Deliberately used only to decide whether a line is blank, never to rewrite text.
#: ZWJ and ZWNJ are load-bearing inside words (Indic conjuncts, emoji sequences) and
#: a soft hyphen is a real hyphenation hint mid-word; it is only their appearance
#: *alone on a line* that means nothing.
_INVISIBLE = (
    "­"  # soft hyphen
    "​‌‍"  # zero-width space / non-joiner / joiner
    "‎‏"  # LTR / RTL marks
    "‪‫‬‭‮"  # bidi embedding and override
    "⁠"  # word joiner
    "⁦⁧⁨⁩"  # bidi isolates
    "﻿"  # zero-width no-break space (a BOM landing mid-file)
)

#: Everything `has_visible_text` strips before asking "is this line blank?" -- the
#: invisibles above plus the whitespace, including the kinds `str.strip()` already
#: knows about.
#:
#: A name rather than the concatenation it replaced, which was built fresh on every
#: call and, more to the point, could not be compared with anything.
#: `diceo.pdf.extract._BLANK_CHARS` is a deliberate second copy of this set -- see
#: the comment there for why that module imports nothing from the rest of the package
#: -- and `tests/test_duplicated_on_purpose.py` asserts the two stay equal, because a
#: duplication argued for on the grounds that it is cheap stops being cheap the moment
#: the copies disagree.
_BLANK_CHARS = _INVISIBLE + " \t\r\n\f\v   　"


#: How deep a chain of forwarded ``message/rfc822`` parts is read before the rest is
#: reported instead of followed. Three covers a forward of a forward of a forward;
#: past that the file is a thread archive rather than one document, and nothing in
#: the format stops a message nesting itself indefinitely.
_RFC822_MAX_DEPTH = 3

#: How deep `_part_size` walks a MIME tree before giving up on a total. Sizing is a
#: diagnostic, not content, so it refuses rather than risking a `RecursionError` on
#: a message nested past anything a mail client would produce.
_PART_SIZE_MAX_DEPTH = 8

#: iCalendar properties a human would read. Everything else in a VEVENT is machine
#: state -- UIDs, sequence numbers, transparency, alarm triggers.
_ICAL_FIELDS = {
    "SUMMARY": "Summary",
    "DTSTART": "Start",
    "DTEND": "End",
    "LOCATION": "Location",
    "ORGANIZER": "Organizer",
    "DESCRIPTION": "Description",
}

#: Characters of two parts' text compared when deciding whether they say the same
#: thing. Long enough that two different mails cannot collide, short enough that a
#: differing footer does not make a duplicate look new.
_DUPLICATE_PREFIX = 200


def has_visible_text(text: str) -> bool:
    """Is there anything here a reader would see?

    The blank-line test throughout this module. `text.strip()` is not enough -- see
    `_INVISIBLE`.
    """
    return bool(text.strip(_BLANK_CHARS))


def sniff_utf16(raw: bytes) -> str | None:
    """``"utf-16-le"``/``"utf-16-be"`` if `raw` looks like UTF-16 with no BOM.

    Windows tools write this whenever a user ticks "Unicode", and without the sniff
    every such file decodes through cp1252 into twice as many characters with a NUL
    between each one -- silently, with no exception and no replacement character to
    count. That doubles every chunk offset, which makes it the worst class of bug
    this library can have.

    Detection is NUL-parity: UTF-16LE encodes ASCII as ``41 00``, so NULs land at odd
    offsets; UTF-16BE as ``00 41``, so they land at even ones. The ratio alone is not
    enough, because a binary payload has NULs too -- so the guess is confirmed by
    decoding the sample and requiring the result to look like text. Known limits: the
    signal weakens as non-ASCII density rises (CJK in UTF-16 carries few NULs) and is
    meaningless on very short inputs, so both are gated rather than guessed at.
    """
    sample = raw[:4096]
    if len(sample) < _UTF16_MIN_SAMPLE:
        return None
    sample = sample[: len(sample) & ~1]
    pairs = len(sample) // 2
    even_nuls = sample[0::2].count(0)
    odd_nuls = sample[1::2].count(0)

    if odd_nuls / pairs >= _UTF16_NUL_SHARE and even_nuls / pairs <= _UTF16_WRONG_PARITY_MAX:
        encoding = "utf-16-le"
    elif even_nuls / pairs >= _UTF16_NUL_SHARE and odd_nuls / pairs <= _UTF16_WRONG_PARITY_MAX:
        encoding = "utf-16-be"
    else:
        return None

    try:
        text = sample.decode(encoding)
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    sane = sum(1 for ch in text if ch.isprintable() or ch.isspace())
    return encoding if sane / len(text) >= _UTF16_SANE_SHARE else None


def strip_controls(text: str, report: Diagnostics) -> str:
    """Remove C0 control characters that no downstream store will accept.

    Not cosmetic. PostgreSQL rejects U+0000 in `text` and in `json`/`jsonb`, XML 1.0
    cannot represent it even escaped, and passing one through turns our success into
    an exception inside the caller's indexer -- which is exactly the failure this
    library exists to prevent. Counted in diagnostics rather than dropped quietly,
    because a document full of controls is usually a decoding problem upstream.
    """
    cleaned, removed = count_controls(text)
    if removed:
        report.notes.append(control_chars_note(removed))
    return cleaned


def count_controls(text: str) -> tuple[str, int]:
    """The same removal, returning the count instead of recording it.

    The sheet readers need this shape: they clean one *cell* at a time, and a note
    per cell would put thousands of identical lines in the diagnostics. They keep a
    running count on `SheetDiagnostics` and emit one line at the end.
    """
    if not text:
        return text, 0
    cleaned = _STRIP_CONTROLS.sub("", text)
    return cleaned, len(text) - len(cleaned)


def decode(raw: bytes, report: Diagnostics) -> str:
    """Bytes to text, with control characters removed and everything counted."""
    return strip_controls(_decode_text(raw, report), report)


def _report_loss(report: Diagnostics | None, what: str, count: int, detail: str) -> None:
    """Record something the index will not have, in whichever report shape arrived.

    Two shapes reach the decoders: :class:`Diagnostics` from the text, HTML and mail
    readers, and ``SheetDiagnostics`` from the CSV one, which carries tuples that
    ``api.py`` forwards. Both end up in ``Diagnostics.truncated``, so both set
    ``lost_data`` -- the boolean the docstring and the README tell callers to branch
    on. One function for it because four sites reported the same loss four ways and
    three of them chose ``notes``, where nothing downstream can see it: identical
    bytes named ``x.txt`` and ``x.html`` disagreed about whether 25,600 characters
    had gone missing.
    """
    if not count or report is None:
        return
    if hasattr(report, "truncate"):
        report.truncate(f"{what}={count} ({detail})")
    else:
        report.truncated.append((what, count, -1))


#: Why a replacement character counts as a truncation and not as a note: it is a byte
#: the reader could not decode, so it is a character the caller's index will not have.
_UNDECODABLE = (
    "bytes that matched no encoding we tried; each one is a character your index will not have"
)


def _count_undecodable(text: str, report: Diagnostics | None) -> str:
    """Report the replacement characters in ``text``, then hand it back unchanged.

    Every lenient decode in this module goes through here, including the BOM and
    BOM-less-UTF-16 paths, which used ``errors="replace"`` and counted nothing at
    all: an odd-length UTF-16 file lost its last character in silence.
    """
    _report_loss(report, "undecodable_bytes", text.count("�"), _UNDECODABLE)
    return text


def _canonical_encoding(label: str) -> str:
    """A declared charset label, mapped to the codec a browser would really use."""
    name = label.strip().lower()
    return "cp1252" if name in _CP1252_LABELS else name


def _declared_encoding(declared: re.Match[bytes] | None) -> str:
    """The codec a ``<meta charset>`` names, or ``""`` if it names nothing usable.

    A declared ``utf-16`` (or ``utf-32``) is discarded rather than obeyed, which is
    what the WHATWG Encoding standard requires and for a reason that is self-evident
    once stated: the declaration was found by an *ASCII* regex, so the bytes carrying
    it are not UTF-16 and neither is the document. Obeying it reads an ordinary page
    as CJK -- and a two-byte codec cannot report being wrong, so nothing would say so.
    """
    if declared is None:
        return ""
    name = _canonical_encoding(declared.group(1).decode("ascii", "ignore"))
    wide = name.replace("_", "-").startswith(("utf-16", "utf16", "utf-32", "utf32"))
    return "" if wide else name


def _decodes_cleanly(raw: bytes, encoding: str, *, partial_tail: bool = False) -> bool:
    """Does `encoding` read every byte of `raw` without replacing anything?

    ``partial_tail`` says `raw` is the opening of something longer, so a multi-byte
    character cut in half at its end is not evidence against a codec: an incremental
    decoder with ``final=False`` buffers the fragment instead of calling it an error.
    Both callers that pass it hold a fixed-size slice of a larger stream -- a 4 KB
    sniff sample and a 64 KiB read -- where the last character is cut in half roughly
    as often as the encoding's average character is long.

    ``b"".decode`` first, as the gate on the label: it is the one call that refuses
    both an unknown charset and a *bytes-to-bytes* codec (a page declaring
    ``charset="zlib"`` names a real entry in the codec registry whose incremental
    decoder happily returns `bytes`, which would then be fed to an HTML parser).
    """
    import codecs

    try:
        b"".decode(encoding)
        if partial_tail:
            codecs.getincrementaldecoder(encoding)().decode(raw, False)
        else:
            raw.decode(encoding)
    except (UnicodeError, LookupError):
        # `UnicodeError` rather than `UnicodeDecodeError`: a declared charset reaches
        # this as a label, and `idna` -- a real codec -- raises the bare parent class.
        return False
    return True


def _pick_encoding(
    raw: bytes, report: Diagnostics, *, partial_tail: bool = False
) -> tuple[str, bool]:
    """Which codec these bytes are, and whether it reads them losslessly.

    Split out of `_decode_text` because a *stream* needs the answer rather than the
    text: `iter_html_blocks` settles the encoding on the first 64 KiB and has to read
    the other 200 MB with it, and for as long as this decision lived inside the
    decode, every later piece was read as UTF-8 instead.

    The bool says a strict decode succeeded, which is what tells a U+FFFD the document
    genuinely contains from one we invented. It is `False` on the BOM and UTF-16 paths
    because those decode leniently by construction.
    """
    for bom, encoding in (
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe\x00\x00", "utf-32-le"),
        (b"\x00\x00\xfe\xff", "utf-32-be"),
        (b"\xff\xfe", "utf-16-le"),
        (b"\xfe\xff", "utf-16-be"),
    ):
        if raw.startswith(bom):
            return encoding, False

    sniffed = sniff_utf16(raw)
    if sniffed:
        report.notes.append(f"utf16_without_bom={sniffed}")
        return sniffed, False

    declared = _META_CHARSET.search(raw[:4096])
    candidates = ["utf-8"]
    name = _declared_encoding(declared)
    if name and name not in ("utf-8", "utf8"):
        # Second normally: cp1252 punctuation is invalid utf-8, so letting utf-8 answer
        # first costs a mislabelled page nothing and protects a real utf-8 one from a
        # label that lies. **First when the sample is pure ASCII**, because then utf-8
        # succeeding is not evidence of anything -- ASCII decodes identically through
        # utf-8, cp1252, shift_jis and iso-8859-2 alike, so the declaration is the only
        # thing here that knows what the *rest* of the document is. That distinction
        # only became load-bearing when this function started answering for a stream:
        # on a whole buffer an ASCII document reads the same either way, but on the
        # first 64 KiB of a windows-1252 page whose accents all come later, guessing
        # utf-8 loses every one of them.
        candidates.insert(0 if raw.isascii() else 1, name)
    # cp1252 last: it decodes almost any byte sequence, so trying it early would
    # mask a real utf-8 document with mojibake.
    candidates.append("cp1252")

    for encoding in candidates:
        if _decodes_cleanly(raw, encoding, partial_tail=partial_tail):
            return encoding, True
    return "utf-8", False


def _decode_text(raw: bytes, report: Diagnostics) -> str:
    """Bytes to text, counting anything that had to be replaced.

    Encoding failures are counted rather than swallowed. A page that decodes with
    4,000 replacement characters is technically a success and practically a loss,
    and rule 3 says the caller gets told which one they have.
    """
    encoding, clean = _pick_encoding(raw, report)
    if clean:
        # A strict decode replaced nothing, so any U+FFFD in here is a character
        # the document genuinely contains and not a byte we lost.
        return raw.decode(encoding)
    return _count_undecodable(raw.decode(encoding, "replace"), report)


def text_lines(handle: IO[bytes], report: Diagnostics | None = None) -> Iterator[str]:
    """Decoded lines from a binary handle, streaming, encoding decided by the BOM.

    ``TextIOWrapper`` rather than decoding each line ourselves, because a UTF-8
    character split across a read boundary and a UTF-16 file both break per-line
    decoding, and both appear in real corpora (UTF-16 is what Windows tools write
    when you tick "Unicode").

    Control characters are stripped here rather than in each caller, because a NUL
    that reaches a caller's index is an exception in *their* process -- PostgreSQL
    rejects U+0000 in `text` and `jsonb`, and XML 1.0 cannot represent it even
    escaped. `report` is optional only so the delimiter sniff can call this without
    one; pass it whenever the count should reach the caller.
    """
    import io

    # Read a real sample, not four bytes: the BOM needs 4, but the BOM-less UTF-16
    # sniff needs enough bytes for the NUL-parity signal to mean anything. Always
    # seek back to 0 -- never assume the handle is at the start, because a caller
    # (or an earlier sniff) may have consumed some of it.
    handle.seek(0)
    head = handle.read(4096)
    handle.seek(0)
    encoding = "utf-8"
    for bom, name in (
        (b"\xff\xfe\x00\x00", "utf-32"),
        (b"\x00\x00\xfe\xff", "utf-32"),
        (b"\xef\xbb\xbf", "utf-8-sig"),
        (b"\xff\xfe", "utf-16"),
        (b"\xfe\xff", "utf-16"),
    ):
        if head.startswith(bom):
            encoding = name
            break
    else:
        # No BOM. A UTF-16 file written by a Windows "save as Unicode" would decode
        # through utf-8 into twice as many characters with a NUL between each one,
        # silently doubling every offset -- see `sniff_utf16`.
        encoding = sniff_utf16(head) or encoding
        if encoding == "utf-8" and not _decodes_cleanly(head, "utf-8", partial_tail=True):
            # The single highest-frequency real-world defect in this whole area, and
            # this path had no fallback at all: `errors="replace"` turned every
            # non-ASCII byte of a cp1252 or latin-1 file into U+FFFD and reported
            # success. `decode()` -- the whole-buffer path used for HTML and mail --
            # has tried cp1252 for a long time; the *streaming* path used for CSV and
            # plain text never did, so the formats most likely to come out of an ERP,
            # a bank or a government export were the ones that lost their accented
            # characters. Across eight rival trackers, encoding/charset/garbled is the
            # largest single issue class there is (657 issues).
            encoding = "cp1252"
            if report is not None:
                report.notes.append("encoding=cp1252 (not valid UTF-8; decoded as cp1252)")
    wrapper = io.TextIOWrapper(handle, encoding=encoding, errors="replace", newline=None)
    removed = 0
    replaced = 0
    kept = 0
    try:
        for line in wrapper:
            clean = _STRIP_CONTROLS.sub("", line)
            removed += len(line) - len(clean)
            replaced += clean.count("�")
            kept += len(clean.strip())
            yield clean
    finally:
        if removed and report is not None:
            if kept:
                report.notes.append(control_chars_note(removed))
            else:
                # Nothing but control characters was in the file, so the reader yields
                # no blocks, the chunker yields no chunks, and the document is simply
                # not in the caller's index. 4,096 NUL bytes named `.txt` used to come
                # back as zero chunks, a `control_chars_removed` *note*, and
                # `lost_data` False -- a clean bill of health for a document that
                # vanished. Only this all-or-nothing case is a truncation: a stray form
                # feed in a real file is not a loss worth raising the flag for, and 93
                # files in the corpus carry one.
                _report_loss(
                    report,
                    "control_chars_removed",
                    removed,
                    "the document held nothing else, so no text reached the index",
                )
        # A replacement character is a byte that is gone -- a truncation rather than a
        # note, so `lost_data` becomes True. That boolean is what the API tells callers
        # to branch on, and a document full of U+FFFD is exactly the case they want to
        # catch. The fuzz suite found this the moment it was written the other way,
        # which is the argument for the fuzz suite.
        _report_loss(report, "undecodable_bytes", replaced, _UNDECODABLE)
        # Detach rather than close: the caller owns the byte handle and may still
        # need it (a mail body reader, for instance, reuses the buffer).
        # A ValueError here means the byte handle is already closed, which happens
        # when the caller abandons the iterator: `open_source`'s context manager
        # exits before this generator is finalised. There is nothing left to detach
        # from, and raising inside a `finally` during GeneratorExit surfaces as an
        # unraisable exception in the caller's log -- a scary traceback for a
        # non-event, printed by a library that promised not to do that.
        with suppress(ValueError):
            wrapper.detach()


#: A paragraph with no blank line in it is flushed at a line boundary past this many
#: characters. Unbounded, a 20 MB log with no blank lines was one string (81 MB peak)
#: and produced its first chunk only at end of file. The chunker splits it anyway.
_MAX_PENDING_TEXT = 1 << 16
_LIST_MARKER = re.compile(r"\d+[.)] ")


def iter_text_blocks(handle: IO[bytes], report: Diagnostics) -> Iterator[Block]:
    """Plain text and Markdown. A blank line ends a block; ``#`` sets a level.

    Read line by line rather than whole -- a 2 GB log file is a legitimate input
    and rule 2 applies to text as much as to PDF.
    """
    pending: list[str] = []
    pending_chars = 0
    fence = False
    for raw in text_lines(handle, report):
        line = raw.rstrip("\n")
        report.chars += len(line) + 1
        stripped = line.strip()
        if pending_chars > _MAX_PENDING_TEXT:
            yield Block("paragraph", "\n".join(pending), 0, Locator())
            pending = []
        if not pending:
            pending_chars = 0
        pending_chars += len(line) + 1

        if stripped.startswith("```") or stripped.startswith("~~~"):
            # Inside a fence, blank lines and '#' are content, not structure.
            fence = not fence
            pending.append(line)
            continue
        if fence:
            pending.append(line)
            continue

        # `has_visible_text`, not `stripped`: a line holding only a zero-width space
        # is truthy to Python and would become a block of pure invisible characters.
        if not has_visible_text(stripped):
            if pending:
                yield Block("paragraph", "\n".join(pending), 0, Locator())
                pending = []
            continue

        if stripped.startswith("#"):
            marker = len(stripped) - len(stripped.lstrip("#"))
            if 1 <= marker <= 6 and stripped[marker : marker + 1] in (" ", ""):
                if pending:
                    yield Block("paragraph", "\n".join(pending), 0, Locator())
                    pending = []
                yield Block("heading", stripped[marker:].strip(" #"), marker, Locator())
                continue

        if stripped.startswith(("- ", "* ", "+ ")) or (
            stripped[:1].isdigit() and _LIST_MARKER.match(stripped)
        ):
            if pending:
                yield Block("paragraph", "\n".join(pending), 0, Locator())
                pending = []
            yield Block("list_item", stripped, 0, Locator())
            continue

        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") > 2:
            if pending:
                yield Block("paragraph", "\n".join(pending), 0, Locator())
                pending = []
            if set(stripped) <= set("|-: \t"):  # the ---|--- rule under a header
                continue
            # Cell by cell, so an empty first cell survives: stripping `| ` off the
            # whole row moved every value of `|  | 5 |` one column left.
            cells = [cell.strip() for cell in stripped[1:-1].split("|")]
            yield Block("table_row", " | ".join(cells), 0, Locator())
            continue

        pending.append(line)

    if pending:
        yield Block("paragraph", "\n".join(pending), 0, Locator())
    # No second count of the replacement characters here. `text_lines` already reports
    # every one of them, as a truncation, over these same lines -- so a `.txt` file
    # arrived with its bytes named once in `truncated` and again in `notes`, and a
    # caller adding the two up double-counted its own loss.


#: Upper bound on elements held open at once, and the only thing between a crawled
#: page and several GB of resident memory.
#:
#: Every non-void start tag pushes a frame on `_stack` and a count on `_open`, and
#: nothing pops until a matching end tag arrives -- so a page that never *closes*
#: anything holds one frame per tag whatever its tags are, and the tags do not have to
#: be ones this reader knows. `<table>` is the expensive one, because it also saves a
#: table frame holding three freshly allocated containers: measured through
#: `iter_html_blocks` in a clean process, 6.7 MB of `<table>` grew the process by
#: **498 MB** and 2.9 MB of `<b>` by 126 MB, both with `truncated` empty. A 100 MB
#: crawled page of that shape is several GB. `Limits` cannot see it -- it is checked
#: between blocks, and this shape produces its first one at end of file.
#:
#: 65,536 is chosen against *unclosed* elements rather than against nesting depth,
#: because this reader does not implement HTML5's optional end tags: 200,000 `<li>`
#: written without `</li>` is legal markup that a browser closes for itself and that
#: holds 200,000 frames here. Nesting alone would justify far less -- the deepest of
#: 197 pages measured (14 crawled, among them nytimes, bbc, npr, github, mdn, eurostat,
#: gov.uk and wikipedia, plus the 182 in the corpora) holds **36** elements open, and
#: the corpus median is 6. What this bounds is ~8 MB of ancestor stack, or ~33 MB if
#: every open element is a table.
#:
#: Past it the element is simply not tracked -- no frame, no census entry, no table
#: state saved -- rather than the document being refused. Text keeps flowing, which is
#: this module's standing answer to malformed markup (`unclosed_skip`, the span cap,
#: the tolerant unwind all count rather than raise), and the count reaches the caller
#: as a truncation because the *structure* around that text really is gone.
_MAX_OPEN_ELEMENTS = 1 << 16


class _HtmlBlocks(HTMLParser):
    """Turn HTML events into blocks, holding at most one block of text.

    A table row is accumulated across its cells so that a row arrives as one
    ``table_row`` block -- the unit that
    experiment 016 (recovering boundaries blind) showed
    matters -- rather than one block per cell, which is unreadable.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[Block] = []
        self._text: list[str] = []
        self._cells: list[str] = []
        self._in_row = False
        #: Table-cell geometry. A `table_row` is tab-separated and the chunker
        #: prefixes the header row, so the *n*th field has to belong to the *n*th
        #: column -- and `colspan`, `rowspan` and empty cells all break that.
        #: `_col` is the next column to fill, `_rowspans` maps a column to
        #: ``(rows still to fill, text)``, and `_in_cell` tells "close the cell that
        #: is open" apart from "a new cell is starting" so the start-tag call cannot
        #: insert a phantom blank.
        self._col = 0
        self._span = 1
        self._rowspan = 1
        self._in_cell = False
        self._rowspans: dict[int, tuple[int, str]] = {}
        #: How many `<td>`/`<th>` elements are open, and the blocks already closed
        #: inside the innermost one. Real HTML wraps cell content in `<p>` or `<div>`
        #: constantly, and a block-level flush emptied `self._text` before
        #: `_flush_cell` could see it: every cell became a loose paragraph, the row
        #: was never emitted at all, and the table lost the header prefixing that
        #: experiment 016 (recovering boundaries blind) showed is
        #: the whole reason a `table_row` is worth recovering.
        self._cell_depth = 0
        self._fragments: list[str] = []
        #: The next row emitted is a table's first, so it carries `row=0`: that is
        #: how the chunker tells two adjacent tables from one.
        self._first_row = False
        #: One entry per open `<ul>`/`<ol>`: ``None`` for bullets, else the last
        #: number used. `_marker` is the prefix the open `<li>`'s first text takes.
        #: Without them a list read as bare lines, where the markdown path kept `- `.
        self._lists: list[int | None] = []
        self._marker = ""
        #: A `<dt>` waiting for its `<dd>`, emitted as `term: definition` -- two
        #: bare alternating lines read as unrelated paragraphs.
        self._term: str | None = None
        #: One saved frame per enclosing `<table>`. Layout-nested tables are
        #: everywhere on the web, and with a single set of these fields the inner
        #: table's first `<tr>` discarded the outer row's cells entirely.
        self._frames: list[tuple] = []
        self._skip = 0
        self._hidden_tag = ""
        self.hidden_chars = 0
        self._math = False
        self._math_text = ""
        self._annotation: list[str] | None = None
        self.math_without_text = 0
        #: Characters inside ``script``/``style``/``svg``/``math``/``template``. Normally
        #: this is CSS and JavaScript and dropping it is the whole point; it matters only
        #: when the element never closes, and then it is the rest of the document.
        self.skipped_chars = 0
        self.unclosed_skip = False
        self._heading = 0
        self._title = ""
        #: Raster figures seen. Not read -- counted, so a page whose answer is
        #: only in a chart does not look like a clean success (rule 3).
        self._images = 0
        #: (tag, role) for every open element, so a block can be judged by its ancestry.
        self._stack: list[tuple[str, str]] = []
        #: How many of each tag name are open, so the tolerant unwind in
        #: `handle_endtag` can answer "is there anything to unwind to?" without
        #: walking the stack. A stray `</span>` in a document nested *n* deep used to
        #: scan all *n* frames and find nothing, once per end tag: 4,000 stray end
        #: tags on a 28 KB page cost 16,012,002 comparisons, and the count squares
        #: with the document (n -> 2n quadrupled it). Scraped HTML is full of both
        #: halves -- unclosed `<b>`/`<font>` and end tags with no start tag.
        self._open: dict[str, int] = {}
        #: Elements that arrived with `_MAX_OPEN_ELEMENTS` already open and were
        #: therefore never tracked. Reported, because their text is still emitted
        #: while the structure around it is not.
        self.untracked_elements = 0
        #: Depth of the outermost chrome region we are inside, or 0.
        self._chrome = 0
        #: Characters removed as site chrome. Reported, never silently dropped.
        self.boilerplate_chars = 0
        #: Characters this document has gained from `colspan`/`rowspan` replication,
        #: and what was refused once the budget ran out. See `_MAX_SPAN_CHARS`.
        self._span_chars = 0
        self.span_cells_capped = 0
        self.span_chars_refused = 0

    def updatepos(self, i: int, j: int) -> int:
        """Skip ``ParserBase``'s line/column bookkeeping. See the warning below.

        The base class counts newlines and recomputes a column offset on every
        segment it consumes -- ~247,000 calls over the 57 HTML pages in the corpora
        -- and the only thing that ever reads the result is ``getpos()``. This reader
        does not call it, does not report a position in any diagnostic, and is
        private, so the bookkeeping is measurably not free and entirely unread:
        **554 ms to 522 ms** over those pages, byte-identical blocks.

        **The trap this leaves, stated so it is not discovered by surprise**:
        ``self.getpos()`` now always answers ``(1, 0)``. If a diagnostic ever wants
        to say *where* in the page something went wrong -- an unclosed
        ``<script>``, a table that never ended -- delete this method first. A wrong
        line number is worse than none, and it would arrive silently.
        """
        return j

    def _flush(self, kind: str = "paragraph", level: int = 0, collapse: bool = True) -> None:
        if not (self._text or self._fragments):
            # Every block-level start tag flushes, and most of them have nothing to
            # flush: with both buffers empty the join is `""`, the regex matches
            # nothing and the `if not text` below returns. Same answer, no regex.
            return
        if self._fragments:
            # Only reachable with a cell still open: `</table>` or end of file inside
            # a `<td>` nobody closed, which is legal HTML5. The cell can never be
            # placed now, so what it collected is emitted rather than dropped -- it
            # is exactly what the reader printed before the fragment path existed.
            self._text[:0] = [" ".join(self._fragments), " "]
            self._fragments.clear()
        # `collapse` is False for exactly one caller, `</pre>`: it is the one element
        # whose segment breaks CSS does *not* turn into spaces, and its line breaks
        # are the only structure a pasted code block has.
        raw = "".join(self._text)
        text = _HTML_WHITESPACE.sub(" ", raw).strip() if collapse else raw.strip("\r\n")
        self._text.clear()
        if not text or (not collapse and not has_visible_text(text)):
            return
        if self._chrome:
            # Site chrome: counted, not emitted. Measured on 8 real pages, this is 2.3%
            # of a long Eurostat page and 54.8% of a short GOV.UK guide -- the defect
            # scales inversely with document length, and crawled pages are short.
            self.boilerplate_chars += len(text)
            return
        if kind == "term":
            if self._term is not None:
                self.out.append(Block("paragraph", self._term, 0, Locator()))
            self._term = text
            return
        if self._marker and kind in ("paragraph", "list_item"):
            text, kind, level = self._marker + text, "list_item", len(self._lists) or 1
            self._marker = ""
        elif kind == "list_item":
            kind = "paragraph"  # an item's text after a nested list: a continuation
        if self._term is not None:
            if kind == "paragraph":
                text = f"{self._term}: {text}"
            else:
                self.out.append(Block("paragraph", self._term, 0, Locator()))
            self._term = None
        self.out.append(Block(kind, text, level, Locator()))

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._skip:
            if tag in _SKIP or tag == self._hidden_tag:
                self._skip += 1
            elif self._math and not self._math_text and tag == "annotation":
                encoding = next((v for k, v in attrs if k == "encoding"), "") or ""
                if encoding.lower() in {
                    "application/x-tex",
                    "application/x-latex",
                    "text/x-tex",
                    "text/latex",
                    "text/plain",
                }:
                    self._annotation = []
            return
        if tag in _SKIP:
            if tag == "math":
                self._math = True
                for key, value in attrs:
                    if key == "hidden" and (value or "").lower() != "until-found":
                        self._hidden_tag = tag
                        self._math = False
                        self._math_text = ""
                        break
                    if key in ("alttext", "aria-label") and value and not self._math_text:
                        self._math_text = value.strip()
            self._skip = 1
            return
        role = ""
        tracked = True
        if tag not in _VOID:
            if len(self._stack) >= _MAX_OPEN_ELEMENTS:
                # Not pushed, not counted, and the `<table>` branch below saves no
                # frame for it either -- see `_MAX_OPEN_ELEMENTS`. Its end tag then
                # unwinds whatever the census can still find under that name, or
                # nothing at all, which is the mis-nesting `handle_endtag`'s tolerant
                # unwind was written for and has always survived.
                tracked = False
                self.untracked_elements += 1
            else:
                for key, value in attrs:
                    if key == "role" and value:
                        role = value.strip().lower()
                    elif key == "hidden" and (value or "").lower() != "until-found":
                        # aria-hidden does not imply invisible: decorative spans can
                        # still hold visible punctuation, units or mathematical text.
                        # until-found is explicitly searchable content in HTML.
                        self._hidden_tag = tag
                        self._skip = 1
                        return
                self._stack.append((tag, role))
                self._open[tag] = self._open.get(tag, 0) + 1
                if tag in ("td", "th"):
                    # Kept in step with the stack in both directions, so a `<td>`
                    # nobody closed cannot leave the reader believing it is inside a
                    # cell for the rest of the document -- the tolerant unwind in
                    # `handle_endtag` takes the depth with it.
                    self._cell_depth += 1
                # The `tag in _CHROME_TAGS or role` guard is a superset of everything
                # `_is_chrome` can answer True to, so it decides the same question --
                # without a method call on every one of a page's tens of thousands of
                # start tags, ~99.9% of which are neither a landmark nor carry a role.
                if (
                    not self._chrome
                    and (tag in _CHROME_TAGS or role)
                    and self._is_chrome(tag, role)
                ):
                    self._flush()
                    self._chrome = len(self._stack)
        if tag == "img":
            # `alt` is authored, human-written content about the figure, and often the
            # only machine-readable form of a chart's message. MarkItDown keeps it; we
            # dropped it, and the corpus writes alt="" so nothing noticed
            # (experiment 033, adjudication).
            alt = ""
            for key, value in attrs:
                if key == "hidden" and (value or "").lower() != "until-found":
                    return
                if key == "alt" and value:
                    alt = value
            self._images += 1
            if alt:
                self._flush()
                self.out.append(
                    Block("caption", _HTML_WHITESPACE.sub(" ", alt).strip(), 0, Locator())
                )
            return
        if tag == "title":
            return
        if tag == "table":
            if not tracked:
                # The frame this would push is the most expensive thing the reader
                # allocates per tag, and it is the whole reason `_MAX_OPEN_ELEMENTS`
                # exists. Resetting the cell state below without saving it would drop
                # the enclosing row as well, so past the ceiling a `<table>` does
                # nothing at all rather than half of this.
                return
            # A nested table opens *inside* an open `<td>`, so the outer cell's
            # accumulated text has to be saved too -- text after `</table>` is still
            # that cell's content. Spans never cross a table boundary, so the map is
            # reset rather than inherited.
            self._frames.append(
                (
                    self._cells,
                    self._col,
                    self._rowspans,
                    self._in_row,
                    self._text,
                    self._in_cell,
                    self._span,
                    self._rowspan,
                    self._fragments,
                    self._first_row,
                )
            )
            self._cells = []
            self._col = 0
            self._rowspans = {}
            self._in_row = False
            self._text = []
            self._in_cell = False
            self._span = 1
            self._rowspan = 1
            self._fragments = []
            self._first_row = True
            return
        if tag == "tr":
            if self._in_row:
                # `</tr>` is optional in HTML5, so the next `<tr>` is what closes the
                # row -- and this reset it instead. Every row but the last vanished:
                # a three-row table came back as `r3c1\tr3c2` and nothing else, with
                # `truncated` empty and `lost_data` False. A `<thead>` written without
                # `</tr>` lost its header row too, which is the one row the chunker
                # prefixes into all the others.
                if not self._in_cell:
                    # Text sitting in the `<tr>` itself with no cell open belongs to no
                    # column, and `_flush_cell` drops exactly that. It is invalid markup
                    # -- a browser lifts it out of the table -- but dropping it to buy
                    # the row back would be trading one rule 3 loss for another.
                    self._flush()
                self._close_row()
            else:
                self._flush()
            self._in_row = True
            self._cells = []
            self._col = 0
            self._in_cell = False
            return
        if tag in ("td", "th") and self._in_row:
            self._flush_cell()
            self._span = self._span_of(attrs, "colspan")
            self._rowspan = self._span_of(attrs, "rowspan")
            self._in_cell = True
            return
        if self._cell_depth and tag in _BLOCK and tag not in ("td", "th"):
            # `table` and `tr` returned above, and a `<td>` is the cell rather than a
            # block inside it -- a stray one outside any `<tr>` has to keep falling
            # through to the flush below, or the text before it is never emitted by
            # anybody. What is left here is a `<p>`, `<div>`, `<ul>`, `<li>`, `<br>`
            # or heading *inside* a cell: a line break in that cell, never a block.
            self._flush_cell_fragment()
            return
        if (self._heading and tag in _BLOCK and tag not in _HEADINGS) or (
            tag == "br" and self._marker
        ):
            # `<h2>Annual<br>Report</h2>` is one heading, not a paragraph and a
            # heading: inside one, a block tag is a space. So is a `<br>` in a list
            # item that has not been emitted yet -- the item is one line.
            self._text.append(" ")
            return
        if tag in _HEADINGS:
            self._flush()
            self._heading = _HEADINGS[tag]
            return
        if tag in ("ul", "ol"):
            self._flush()
            if tracked:
                start = 1
                if tag == "ol":
                    for key, value in attrs:
                        if key == "start" and value and value.strip().isdigit():
                            start = int(value)
                self._lists.append(None if tag == "ul" else start - 1)
            return
        if tag == "li":
            self._flush()
            number = self._lists[-1] if self._lists else None
            if number is None:
                self._marker = "- "
            else:
                self._lists[-1] = number + 1
                self._marker = f"{number + 1}. "
            return
        if tag in _BLOCK:
            if tag == "br" and self._open.get("pre"):
                self._text.append("\n")
                return
            self._flush()

    def _drop_chrome(self) -> None:
        """Leave the chrome region, flushing whatever it accumulated into the counter."""
        self._flush()
        self._chrome = 0

    def _is_chrome(self, tag: str, role: str) -> bool:
        """Is the element just opened site chrome rather than document content?

        Three clauses, each earned by a real page:

        * ``<nav>`` always.
        * an ARIA ``banner``/``contentinfo`` landmark -- NPR nests its site footer inside a
          ``<section>``, so the structural clause below keeps it, but the author declared
          it furniture. ``role="navigation"`` is **not** included: Wikipedia's bottom
          navboxes carry it and hold 4,338 characters of genuine topic terms.
        * ``<header>``/``<footer>`` whose nearest sectioning ancestor is the document
          itself. Inside an ``<article>``, ``<section>`` or ``<figure>`` they are that
          thing's header and footer -- the real ONS bulletin keeps all ten of its section
          headings in ``article > div > section > header``, and its table sources in
          ``figure > footer``. A header inside ``main`` is also document content:
          GOV.UK puts its page title and publication metadata there.

        ``<aside>`` is deliberately kept: it holds pull-quotes and key-facts boxes at
        least as often as sponsor messages.
        """
        if tag == "nav":
            return True
        if role in _CHROME_ROLES:
            return True
        if tag not in ("header", "footer"):
            return False
        # Walked by index rather than over `self._stack[:-1]`: that slice copies the
        # whole ancestor stack on every `<header>`/`<footer>`, before the loop it feeds
        # has looked at one frame.
        for index in range(len(self._stack) - 2, -1, -1):
            ancestor = self._stack[index][0]
            if tag == "header" and ancestor == "main":
                return False
            if ancestor in _SECTIONING or ancestor in _SECTION_ROOTS:
                return ancestor == "body"
        return True

    #: Upper bound on how far one cell may be expanded. `colspan="0"` is legal HTML
    #: meaning "to the end of the column group" and `colspan="99999"` is a hostile
    #: file; neither may be taken literally.
    _MAX_SPAN = 64

    @staticmethod
    def _span_of(attrs: list, name: str) -> int:
        for key, value in attrs:
            if key == name:
                try:
                    span = int((value or "1").strip())
                except (TypeError, ValueError):
                    return 1
                if span < 1:
                    return 1
                return min(span, _HtmlBlocks._MAX_SPAN)
        return 1

    #: Characters one document may gain from span replication before spans stop being
    #: honoured. Replication is the only thing in this reader that makes the output
    #: grow faster than the input, and it multiplies: `colspan="64" rowspan="64"`
    #: copies one cell 4,096 times, and a row of `<tr>` costs four bytes to collect
    #: another 64 of them. Measured: **952 bytes reached 1,232,832 characters and
    #: 1,852 bytes reached 4,919,232**, with `truncated` empty and `lost_data` False.
    #:
    #: Deliberately far above any real need rather than tight -- all 56 HTML files in
    #: the held-out corpora replicate **0** characters, and a guard that fires on
    #: ordinary documents is noise a caller learns to ignore. What is bought here is
    #: not a small number, it is a *constant* one: output is now at most the input
    #: plus this.
    _MAX_SPAN_CHARS = 1 << 18

    def _place(self, text: str) -> None:
        """Put one cell at the next free column, honouring both spans."""
        self._absorb_spans()
        span, rowspan = self._span, self._rowspan
        self._span = 1
        self._rowspan = 1
        replicated = len(text) * (span * rowspan - 1)
        if replicated and self._span_chars + replicated > self._MAX_SPAN_CHARS:
            # Past the budget the cell takes one column and carries into no further
            # row. The text itself is still placed, so what is lost is the *coverage*
            # -- the columns this cell was labelling now have no label, which is the
            # thing the repetition below exists to provide. Counted, and reported as
            # a truncation, because that is the half a caller cannot see.
            self.span_cells_capped += 1
            self.span_chars_refused += replicated
            span = rowspan = 1
        else:
            self._span_chars += replicated
        start = self._col
        for offset in range(span):
            while len(self._cells) <= start + offset:
                self._cells.append("")
            # Repeating the text into every column the cell covers is what the span
            # *means*: `<th colspan="3">2023</th>` labels three columns, and a blank
            # in two of them would lose the label there.
            self._cells[start + offset] = text
            if rowspan > 1:
                self._rowspans[start + offset] = (rowspan - 1, text)
        self._col = start + span

    def _absorb_spans(self) -> None:
        """Fill any columns owned by a cell merged down from an earlier row."""
        while self._col in self._rowspans:
            remaining, text = self._rowspans[self._col]
            while len(self._cells) <= self._col:
                self._cells.append("")
            self._cells[self._col] = text
            if remaining > 1:
                self._rowspans[self._col] = (remaining - 1, text)
            else:
                del self._rowspans[self._col]
            self._col += 1

    def _flush_cell_fragment(self) -> None:
        """End a block inside a cell without ending the cell.

        The one thing this must never do is append to `self.out`: a `<td>` holding
        `<p>EMEA</p>` used to emit that paragraph and then place an *empty* cell, so a
        table of wrapped cells produced no data rows at all -- `any(self._cells)` was
        False for every one of them -- and its numbers reached the index as loose
        paragraphs with no column names anywhere near them.

        Nothing pending is the common case -- every block-level tag inside a cell
        calls this, and real markup opens far more elements than it puts text in --
        and with nothing pending the join, the whitespace regex, the strip and the
        truthiness test all agree on ``""``. Returning first is the same answer
        without the four calls.
        """
        if not self._text:
            return
        fragment = _HTML_WHITESPACE.sub(" ", "".join(self._text)).strip()
        self._text.clear()
        if fragment:
            self._fragments.append(fragment)

    def _flush_cell(self) -> None:
        if not (self._text or self._fragments or self._in_cell):
            # Nothing collected and no cell open: the join is over an empty list, the
            # clear is a no-op and `_place` is not reached. Same state, no work --
            # and this is called from every `<td>`, `</td>`, `<tr>` and `</tr>`.
            return
        self._flush_cell_fragment()
        # One space between blocks, because that is what they were: two lines of the
        # same cell, not two cells.
        cell = " ".join(self._fragments)
        self._fragments.clear()
        if self._in_cell:
            self._place(cell)
            self._in_cell = False

    def _close_row(self) -> None:
        """Finish the open row and emit it."""
        self._flush_cell()
        # A cell merged down from an earlier row can sit past this row's last
        # `<td>`, so absorb once more before deciding the row is finished.
        self._absorb_spans()
        self._in_row = False
        # Trailing blanks carry nothing and cost a tab each; interior ones are
        # load-bearing, because they are what keeps later fields under their own
        # column names.
        while self._cells and not self._cells[-1]:
            self._cells.pop()
        if any(self._cells):
            # ` | `-separated like docx, pptx and sheet rows. A tab was the old
            # separator, and most tokenizers read a tab as a plain space, so cell
            # boundaries -- and every empty cell -- vanished from the embedding.
            row = 0 if self._first_row else -1
            self._first_row = False
            self.out.append(Block("table_row", " | ".join(self._cells), 0, Locator(row=row)))
        self._cells = []
        self._col = 0

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag in _SKIP or tag == self._hidden_tag:
                self._skip -= 1
                if not self._skip:
                    self._hidden_tag = ""
                    if self._math:
                        self._finish_math()
            elif self._annotation is not None and tag == "annotation":
                self._math_text = "".join(self._annotation).strip()
                self._annotation = None
            return
        if tag not in _VOID and self._open.get(tag):
            # Tolerate mis-nesting: unwind to the most recent matching open tag rather
            # than assuming the document is well formed. A stray `</div>` must not
            # desynchronise every depth test after it.
            #
            # `self._open` is what keeps that linear. The scan below is paid for by the
            # frames it deletes -- each start tag pushes one frame and each frame is
            # deleted at most once, so the scanning is O(1) per tag amortised -- but an
            # end tag matching *nothing* deleted nothing and still walked the whole
            # stack, and that is the shape scraped HTML produces by the thousand. The
            # census answers it without touching the stack at all.
            for index in range(len(self._stack) - 1, -1, -1):
                if self._stack[index][0] == tag:
                    if self._chrome and self._chrome > index:
                        self._drop_chrome()
                    for name, _ in self._stack[index:]:
                        if name in ("td", "th"):
                            self._cell_depth -= 1
                        count = self._open[name] - 1
                        if count:
                            self._open[name] = count
                        else:
                            del self._open[name]
                    del self._stack[index:]
                    break
        if tag == "title":
            self._title = _WHITESPACE.sub(" ", "".join(self._text)).strip()
            self._text.clear()
            return
        if tag in _HEADINGS:
            if self._cell_depth:
                self._flush_cell_fragment()
                return
            self._flush("heading", self._heading)
            self._heading = 0
            return
        if tag in ("td", "th"):
            self._flush_cell()
            return
        if tag == "tr":
            self._close_row()
            return
        if tag == "table":
            if self._in_row:
                # `</td>` and `</tr>` are both optional in HTML5, so a table may
                # legally end with a row still open -- and the frame pop below
                # replaces `_cells`, `_text` and `_fragments` in one go, so an open
                # row went with it. Loose cell text used to survive as a paragraph
                # because every block-level tag flushed one; now that a cell holds
                # its text until it is placed, dropping the row here would be
                # silent loss (rule 3).
                self._close_row()
            if self._frames:
                (
                    self._cells,
                    self._col,
                    self._rowspans,
                    self._in_row,
                    self._text,
                    self._in_cell,
                    self._span,
                    self._rowspan,
                    self._fragments,
                    self._first_row,
                ) = self._frames.pop()
                if self._in_cell and self._text:
                    # A table is block-level, so the text either side of it is not one
                    # word: without this, `OUTER 2<table>..</table>AFTER` welds to
                    # `OUTER 2AFTER`.
                    self._text.append(" ")
            else:
                self._flush()
                self._rowspans = {}
            return
        if self._cell_depth and tag in _BLOCK:
            # The closing half of the start-tag guard above: `</p>`, `</div>`,
            # `</li>` inside a cell end a line of that cell, nothing more.
            self._flush_cell_fragment()
            return
        if self._heading and tag in _BLOCK:
            return  # see the start-tag half: a block tag inside a heading is a space
        if tag == "pre":
            self._flush(collapse=False)
            return
        if tag == "li":
            self._flush("list_item")
            self._marker = ""
            return
        if tag in ("ul", "ol"):
            self._flush()
            if self._lists:
                self._lists.pop()
            return
        if tag == "dt":
            self._flush("term")
            return
        if tag in _BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip:
            if self._hidden_tag:
                self.hidden_chars += len(data)
            elif self._annotation is not None:
                self._annotation.append(data)
            # Counted, because an unclosed skip element swallows everything after it and
            # a browser does the same -- but a browser is not building somebody's index.
            self.skipped_chars += len(data)
            return
        self._text.append(data)
        if (
            len(self._text) > _MAX_TEXT_PIECES
            and not (self._heading or self._cell_depth or self._in_row)
            and not self._open.get("pre")
        ):
            # A page of inline tags and no block ones was one paragraph held whole:
            # 20 MB of `<span>`s peaked at 213 MB with its first chunk at end of file.
            self._flush("list_item" if self._open.get("li") else "paragraph")

    def _finish_math(self) -> None:
        if self._annotation is not None:
            self._math_text = "".join(self._annotation).strip()
            self._annotation = None
        if self._math_text:
            self._text.append(self._math_text)
        else:
            self.math_without_text += 1
        self._math = False
        self._math_text = ""

    def close(self) -> None:  # type: ignore[override]
        super().close()
        if self._math:
            self._finish_math()
        if self._in_row:
            # A file that simply stops inside a table -- truncated downloads do this
            # constantly -- otherwise loses the last row, which is now held in the
            # cell rather than already loose in `self.out`.
            self._close_row()
        self._flush(collapse=not self._open.get("pre"))
        if self._term is not None:
            self.out.append(Block("paragraph", self._term, 0, Locator()))
            self._term = None
        # `<style>` with no `</style>` leaves `_skip` above zero for the rest of the
        # document, so every remaining byte goes to `handle_data` and is dropped. On a
        # mid-body occurrence the caller gets *one* chunk -- enough that no error fires
        # and the document looks merely short -- and on an early one, zero chunks with
        # `lost_data` False. That is rule 3's stated worst case: the document is not in
        # the index and nothing anywhere says so.
        #
        # Recovering the remainder is not on offer (nobody, including a browser, can
        # tell where the element was meant to end), so the honest move is to say how
        # much went and which element ate it.
        self.unclosed_skip = self._skip > 0

    @property
    def title(self) -> str:
        return self._title

    @property
    def pending_chars(self) -> int:
        """Characters ``html.parser`` is still holding because a construct is open.

        ``rawdata`` is the base class's own buffer: `feed` appends to it, and whatever
        `goahead` could not finish -- a start tag with no ``>``, an unclosed comment, a
        ``<script>`` body with no ``</script>`` yet -- stays there and is rescanned from
        the beginning on the next call. That rescan is the whole cost `_MAX_PENDING_CHARS`
        bounds, and this is the one place that reaches into the base class for it.

        ``getattr`` rather than the attribute, so that a CPython which renames the
        buffer reports 0 and leaves this reader exactly as slow as it was before the
        ceiling existed, instead of raising on every document.
        """
        return len(getattr(self, "rawdata", ""))


#: How much unterminated markup one document may pile up before the rest of it is
#: refused. The other half of the answer is the growing read below; neither works
#: alone, and the reason they are one fix is that this number has to be *high*.
#:
#: ``html.parser`` cannot emit a construct it has not seen the end of, so it keeps the
#: whole of it -- a start tag with no ``>``, an unclosed comment, a ``<script>`` body
#: with no ``</script>`` -- in its buffer and rescans it from the beginning on every
#: `feed`. Feeding a document in fixed pieces therefore makes one unterminated tag cost
#: O(n^2 / piece). Measured through this function, at almost exactly 4x per doubling:
#: **0.16s at 2 MB, 0.61s at 4 MB, 2.45s at 8 MB, 9.48s at 16 MB**, which is ~25
#: minutes at the 200 MB export this module's docstring advertises and ~10 hours at
#: 1 GB, with no error and no diagnostic. Rule 2's bounded memory does not hold either:
#: the buffer is the document.
#:
#: 8 MiB is 12.6x the largest buffer any of the 197 pages measured ever holds. That
#: peak is 651 KB, on a nytimes.com front page, and it is a **legitimate** 695 KB
#: inline ``<script>`` rather than damage -- the parser holds a CDATA body for the same
#: reason it holds a half-written tag. The next highest is 44 KB and 190 pages hold
#: under 1 KB. Refusing the tail of a real page is a far worse failure than the seconds
#: a tighter ceiling would save, which is what buys the slack; embedded JSON of a few
#: MB is a thing pages do.
_MAX_PENDING_CHARS = 1 << 23


#: Text pieces one block may gather before it is flushed mid-paragraph. One piece is
#: the text between two tags, so this is tens of KB of prose, not a limit any real
#: paragraph meets.
_MAX_TEXT_PIECES = 4096


def iter_html_blocks(
    handle: IO[bytes],
    report: Diagnostics,
    chunk_bytes: int = 1 << 16,
    encoding: str | None = None,
) -> Iterator[Block]:
    """Stream HTML as blocks, feeding the parser a piece at a time.

    The parser holds one block of text at a time, so a 200 MB single-page export
    costs the same memory as a small one.

    ``chunk_bytes`` is a floor rather than the size of every piece. A document that
    opens a construct and never finishes it makes the parser hold and rescan it, and
    reading such a document in fixed pieces is quadratic; `_MAX_PENDING_CHARS` has the
    measurements and both halves of the answer.

    Every piece goes through **one** incremental decoder, seeded with the encoding the
    first piece settled on. Both halves of that sentence were defects, and they had to
    be fixed together because either one alone still corrupts the document:

    * *The encoding was worked out and thrown away.* Only the first piece was decoded
      by `decode()`; every later one was ``raw.decode("utf-8", "replace")``. So a
      windows-1252, shift_jis or iso-8859-2 page was read correctly up to byte 65,536
      and as UTF-8 after it -- the bigger the page, the smaller the share of it that
      arrived. Nothing counted the replacements either, so ``lost_data`` stayed
      ``False`` while most of the document turned into U+FFFD.
    * *Fixed-size pieces were decoded independently.* That splits any multi-byte
      character straddling a boundary, in a perfectly valid UTF-8 file, and
      ``errors="replace"`` hid the result. An incremental decoder holds the fragment
      until the next piece completes it, which removes the fault by construction
      rather than by arithmetic.
    """
    import codecs

    parser = _HtmlBlocks()
    first = handle.read(chunk_bytes)
    if encoding is None:
        # A caller that already decoded the markup passes its encoding: sniffing
        # re-encoded UTF-8 let a stale `<meta charset=windows-1252>` win, and an
        # email body past 64 KiB came back as `CafÃ©`.
        encoding, _ = _pick_encoding(first, report, partial_tail=True)
    decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
    # `ignore` drops exactly the byte sequences `replace` turns into U+FFFD and
    # differs from it in nothing else, so the gap between their outputs is the number
    # of characters we invented. Counting U+FFFD in the text instead would call a page
    # that genuinely contains one a loss, which is the noise `_decode_text` has always
    # been careful not to make.
    kept = codecs.getincrementaldecoder(encoding)(errors="ignore")
    removed = 0
    replaced = 0
    unread = 0
    held = 0
    piece = first
    while True:
        final = not piece
        text = decoder.decode(piece, final)
        replaced += len(text) - len(kept.decode(piece, final))
        clean = _STRIP_CONTROLS.sub("", text)
        removed += len(text) - len(clean)
        report.chars += len(clean)
        parser.feed(clean)
        yield from _drain(parser)
        if final:
            break
        pending = parser.pending_chars
        if pending > _MAX_PENDING_CHARS:
            held = pending
            unread = _unread_bytes(handle)
            break
        # Never read a piece smaller than what the parser is still holding. That buffer
        # is rescanned in full on every feed, so a fixed piece rescans it once per
        # 64 KiB of document, which is the quadratic `_MAX_PENDING_CHARS` describes; a
        # piece that grows with the buffer makes the total rescanning proportional to
        # the document instead. Measured on 16 MB of unterminated tag with the ceiling
        # lifted, so that this line is the only difference: **9.48s to 0.33s**, and 2x
        # rather than 4x for every doubling after it. It is also what lets the ceiling
        # sit an order of magnitude above the biggest buffer a real page produces,
        # which is the half that matters more. Healthy markup holds under 1 KB back
        # between pieces on 190 of the 197 pages measured, so `max` returns
        # `chunk_bytes` and the piece size is exactly what it has always been.
        piece = handle.read(max(chunk_bytes, pending))
    parser.close()
    yield from _drain(parser)
    if removed:
        # One note for the document rather than one per 64 KiB piece -- and the pieces
        # after the first were never stripped at all, so a NUL past byte 65,536 went
        # to the caller's index to be rejected there.
        report.notes.append(control_chars_note(removed))
    _report_loss(report, "undecodable_bytes", replaced, _UNDECODABLE)
    if parser.unclosed_skip:
        report.truncate(
            f"unclosed_element: a script/style/svg/math/template or hidden tag "
            f"was never closed, "
            f"so the {parser.skipped_chars:,} characters after it were read as its "
            f"content and are not in the output. Where the element was meant to end "
            f"cannot be recovered -- a browser drops the same text"
        )
    if parser.math_without_text:
        report.truncate(
            f"math_without_text={parser.math_without_text}: MathML expressions had no "
            f"authored alttext, aria-label or TeX/plain-text annotation; their "
            f"presentation trees were not converted"
        )
    if parser.hidden_chars:
        report.notes.append(f"hidden_chars={parser.hidden_chars} (hidden UI content removed)")
    if parser.span_cells_capped:
        report.truncate(
            f"table_span_expansion_capped={parser.span_cells_capped} cell(s): this "
            f"document asked for more than {_HtmlBlocks._MAX_SPAN_CHARS:,} characters "
            f"of colspan/rowspan replication, so {parser.span_chars_refused:,} further "
            f"characters of it were refused. Every cell's text is still in the output "
            f"once; what those cells no longer do is label the other columns and rows "
            f"they span, so fields there may sit under the wrong column name"
        )
    if unread:
        # `held` without `unread` means the ceiling was passed on the piece that
        # finished the file: every byte was still fed, so nothing is missing and there
        # is nothing to report. A diagnostic that fires when the output is complete is
        # the noise a caller learns to ignore, and then the real one goes unread too.
        report.truncate(
            f"unterminated_markup: a tag, comment or script body ran for over "
            f"{held:,} characters without ending, so the {unread:,} bytes after it were "
            f"not read. Everything before it is in the output, and so is whatever the "
            f"parser could still make of the buffer at end of file. Reading on is what "
            f"is refused: an unfinished construct is rescanned on every piece, which "
            f"costs 4x for every doubling of it"
        )
    if parser.untracked_elements:
        report.truncate(
            f"nesting_untracked={parser.untracked_elements} element(s): this document "
            f"held more than {_MAX_OPEN_ELEMENTS:,} elements open at once, so the ones "
            f"past that are not on the ancestor stack. Their text is still in the "
            f"output; what is no longer reliable is the structure around it -- a cell "
            f"may leave its row, and a nav/header/footer among them is not recognised "
            f"as site chrome"
        )
    if parser._images:
        report.notes.append(images_note(parser._images))
    if parser.boilerplate_chars:
        report.notes.append(
            f"boilerplate_chars={parser.boilerplate_chars} (nav/header/footer removed)"
        )
    if parser.title:
        report.notes.append(f"title={parser.title[:120]}")


def _drain(parser: _HtmlBlocks) -> Iterator[Block]:
    if parser.out:
        yield from parser.out
        parser.out = []


def _unread_bytes(handle: IO[bytes]) -> int:
    """How much is left, counted without decoding or parsing any of it.

    Only so the truncation entry can name a number, which is the difference between a
    caller knowing they lost a footer and knowing they lost 900 MB. Seeking to the end
    would be cheaper, but the handle can be a stream that cannot seek; nothing here is
    decoded, kept or joined, so the cost is one pass of I/O in constant memory.
    """
    total = 0
    while True:
        piece = handle.read(1 << 20)
        if not piece:
            return total
        total += len(piece)


def _header_values(message: object, field: str) -> list[str]:
    """Every occurrence of a header, de-duplicated, in order.

    ``message[field]`` returns only the *first* occurrence and reports nothing, so a
    mail carrying two ``Subject:`` lines silently loses one -- and if the real subject
    is the second, the index gets the wrong one. RFC 5322 permits at most one, but
    duplicates occur in the wild, which is why ``get_all`` exists.
    """
    try:
        raw = message.get_all(field) or []  # type: ignore[attr-defined]
    except Exception:  # a malformed header can raise on access
        return []
    seen: dict[str, None] = {}
    for item in raw:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


class _Body(NamedTuple):
    """Which part a reader would have read, and the evidence for choosing it."""

    part: object | None
    subtype: str
    text: str
    #: Pre-rendered blocks, when the winner is html that had to be rendered to be
    #: compared at all. Empty when `text` is what gets emitted.
    blocks: tuple[Block, ...]
    #: ``id()`` of every candidate considered, chosen or not. The reconciliation pass
    #: must not emit the alternative that lost -- that would double the body.
    seen: frozenset[int]
    #: Whitespace-collapsed opening of the winning text, for the duplicate test.
    prefix: str


def _merge(target: Diagnostics, scratch: Diagnostics) -> None:
    """Fold a throwaway report into the caller's, once its part is kept."""
    target.chars += scratch.chars
    target.notes.extend(scratch.notes)
    for item in scratch.truncated:
        target.truncate(item)


def _part_text(part: object, report: Diagnostics) -> str:
    """The decoded text of one part, counting a decode failure rather than raising.

    Deliberately not ``get_content()``. It returns `str`, so the bytes never reached
    `decode()` and the declared charset was obeyed as an instruction rather than
    weighed as evidence. Two real shapes came out of that: a body labelled with a
    charset Python has no codec for raised `LookupError` and lost the whole body while
    its text sat right there in the file -- rule 3's worst case -- and a utf-8 body
    mislabelled `iso-8859-1`, which is the commonest mail defect there is, decoded
    without error into mojibake nobody counted.
    """
    try:
        raw = part.get_payload(decode=True)  # type: ignore[attr-defined]
        if raw is None:
            # A payload that is a list of parts, not bytes. Those have no text of
            # their own; leave them to the caller that knows what they are.
            content = part.get_content()  # type: ignore[attr-defined]
            return content if isinstance(content, str) else ""
    except Exception as exc:
        report.truncate(f"body_undecodable={type(exc).__name__}")
        return ""
    return _decode_part(raw, part, report)


def _decode_part(raw: bytes, part: object, report: Diagnostics) -> str:
    """One part's bytes to text, with the declared charset weighed against them.

    The declared label still leads -- it is the only thing that can name a Shift-JIS or
    a KOI8-R body -- but it does not get the last word, because a single-byte codec
    cannot report being wrong. It loses when it produced U+FFFD where utf-8 is clean,
    and when it produced no error at all but the payload is valid utf-8 carrying the
    sequences that make `Ã©`. An unknown label is not fatal any more: `decode()`'s
    ladder reads the bytes without it, which is what the html reader has always done.
    """
    try:
        label = part.get_content_charset() or ""  # type: ignore[attr-defined]
    except Exception:  # a malformed Content-Type can raise on access
        label = ""
    declared = _canonical_encoding(label)
    if declared in ("", "utf-8", "utf8"):
        return decode(raw, report)
    try:
        text = raw.decode(declared, "replace")
    except Exception:
        # Not just `LookupError`. A `Content-Type` can name any codec at all, and two
        # kinds fail differently: `x-unknown-8` has no codec, while `idna` has one that
        # refuses an error handler and raises a bare `UnicodeError`. Neither is a reason
        # to lose the body -- the bytes are still there and `decode()` can still read
        # them, which is what the html reader does with an unrecognised meta charset.
        report.notes.append(f"charset_unusable={label} (decoded from the bytes instead)")
        return decode(raw, report)

    valid_utf8 = True
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        valid_utf8 = False
    if valid_utf8 and ("�" in text or _MOJIBAKE_BYTES.search(raw)):
        report.notes.append(
            f"charset_override={label}->utf-8 (declared label lost to the bytes)"
        )
        return decode(raw, report)

    if valid_utf8 and _HIGH_BYTE.search(raw):
        # The declared reading won, and the bytes are *also* a clean UTF-8 document with
        # non-ASCII in it. Both readings exist and only the producer knows which was
        # meant. Keeping the declared one is the conservative call -- it is the only
        # thing that can name a KOI8-R or a Shift-JIS body, and the signature above
        # already catches the case where it is visibly wrong. But a caller reading
        # Cyrillic or CJK out of a body labelled latin-1 is looking at the one shape
        # this rule does not catch, and rule 3 says they get to know that rather than
        # to wonder.
        report.notes.append(
            f"charset_ambiguous={label} (the bytes are also valid UTF-8; "
            f"kept the declared reading)"
        )

    # `get_content()` replaced these too and said nothing, so a body could arrive as a
    # page of U+FFFD and still look like a clean success -- and reporting it as a note
    # was only half a step further: `lost_data` stayed False for a body that is gone.
    return strip_controls(_count_undecodable(text, report), report)


def _html_blocks_of(content: str, report: Diagnostics) -> list[Block]:
    import io

    return list(iter_html_blocks(io.BytesIO(content.encode("utf-8")), report, encoding="utf-8"))


def _visible_chars(blocks: list[Block]) -> int:
    return sum(len(block.text.strip()) for block in blocks)


def _opening(text: str) -> str:
    return " ".join(text.split())[:_DUPLICATE_PREFIX]


def _headers_size(part: object) -> int:
    """Roughly what a part's headers occupy on the wire, without serialising them."""
    try:
        return sum(len(name) + len(str(value)) + 4 for name, value in part.items())  # type: ignore[attr-defined]
    except Exception:
        return 0


def _part_size(part: object, depth: int = 0) -> int:
    """Estimated bytes of a part's payload, without decoding it.

    ``get_payload(decode=True)`` returns ``None`` rather than raising for a
    ``message/rfc822`` part, so ``len(... or b"")`` reported every forwarded mail as
    0 bytes -- a number a caller cannot act on and cannot tell from an empty file.

    This number exists only to print an estimated payload size in a
    diagnostic about a part diceo deliberately **does not** extract, and it was
    base64-decoding that part in full to get it -- and, for multipart, re-serialising
    every child with ``as_bytes()``. Measured on a 160 MB mail carrying a 120 MB
    attachment, in a clean process: parsing alone peaks at 984 MB, and the old sizing
    took that to **1,144 MB** -- a whole extra copy of the file to print a number.
    Computing it from the *encoded* payload instead costs nothing measurable: 984 MB,
    unchanged from the parse.

    Base64 carries at most three bytes per four characters, excluding line breaks;
    padding can over-count by two bytes. Quoted-printable is also over-counted.
    Diagnostics label the estimate instead of claiming an exact decoded count.

    None of this makes mail *streamed*. ``message_from_binary_file`` materialises the
    whole message -- the 984 MB above is 6.1x the file, and it is the stdlib parser,
    not us. `iter_email_blocks` says so in the diagnostics rather than leaving the
    README's "bounded memory" to cover a format where it does not hold.
    """
    try:
        if part.is_multipart():  # type: ignore[attr-defined]
            if depth >= _PART_SIZE_MAX_DEPTH:
                return -1
            return sum(
                _headers_size(child) + _part_size(child, depth + 1)
                for child in part.get_payload()  # type: ignore[attr-defined]
            )
        raw = part.get_payload()  # type: ignore[attr-defined]
        if not isinstance(raw, str):
            return -1
        if str(part.get("Content-Transfer-Encoding", "")).strip().lower() == "base64":  # type: ignore[attr-defined]
            return (len(raw) - raw.count("\n") - raw.count("\r")) * 3 // 4
        return len(raw)
    except Exception:
        return -1


def _choose_body(message: object, report: Diagnostics) -> _Body:
    """The body a reader would have seen, chosen by how much text it actually yields.

    ``get_body(preferencelist=("plain", "html"))`` returns the first *available*
    preference rather than the fullest one, so a one-line "view this in your browser"
    stub beats the entire html body -- which then vanishes completely, because
    ``iter_attachments()`` yields nothing at all for a ``multipart/alternative``.

    Both candidates are fetched and the html one is rendered through
    :func:`iter_html_blocks` so the two can be compared on *stripped visible* text:
    experiment 029 (bug-tracker audit) measured that raw byte
    length cannot discriminate (the stub was 0.67 of the html part's bytes, and markup
    inflates the loser). Rendering a part that then loses is the price of the
    comparison; it is bounded by the body, which the ``email`` package has already
    materialised. Ties go to plain text: it is the cheaper read and says the same
    thing.
    """
    plain = html = None
    try:
        plain = message.get_body(preferencelist=("plain",))  # type: ignore[attr-defined]
        html = message.get_body(preferencelist=("html",))  # type: ignore[attr-defined]
    except Exception:  # malformed multipart
        report.notes.append("body_unreadable")
    seen = frozenset(id(part) for part in (plain, html) if part is not None)

    if plain is not None and html is not None:
        plain_scratch = Diagnostics()
        plain_text = _part_text(plain, plain_scratch)
        html_scratch = Diagnostics()
        html_blocks = _html_blocks_of(_part_text(html, html_scratch), html_scratch)
        plain_visible = len(plain_text.strip())
        html_visible = _visible_chars(html_blocks)
        if html_visible > plain_visible:
            _merge(report, html_scratch)
            report.notes.append(
                f"alternative_discarded=text/plain ({plain_visible} visible chars; "
                f"kept text/html with {html_visible})"
            )
            joined = "\n".join(block.text for block in html_blocks)
            return _Body(html, "html", "", tuple(html_blocks), seen, _opening(joined))
        _merge(report, plain_scratch)
        report.notes.append(
            f"alternative_discarded=text/html ({html_visible} visible chars; "
            f"kept text/plain with {plain_visible})"
        )
        return _Body(plain, "plain", plain_text, (), seen, _opening(plain_text))

    part = plain if plain is not None else html
    if part is None:
        return _Body(None, "", "", (), seen, "")
    text = _part_text(part, report)
    subtype = "html" if part.get_content_subtype() == "html" else "plain"
    return _Body(part, subtype, text, (), seen, _opening(text))


def _probably_never_mail(message: object) -> bool:
    """Is there no MIME structure at all -- was this only ever a text file?

    The fallback in :func:`iter_email_blocks` re-reads the bytes as plain text, which
    is right for a log of ``Date:`` lines and catastrophic for real mail: boundaries,
    header blocks and base64 payloads all land in the index while the body does not.
    Structure is the discriminator that a header-name list can never be -- a
    ``MIME-Version`` header, a ``boundary`` parameter or more than one part means a
    mail agent wrote this, whatever diceo then failed to find in it.
    """
    try:
        if message.get("mime-version") is not None:  # type: ignore[attr-defined]
            return False
        if message.get_param("boundary") is not None:  # type: ignore[attr-defined]
            return False
        return len(list(message.walk())) == 1  # type: ignore[attr-defined]
    except Exception:
        return False


def _unescape_ical(value: str) -> str:
    """RFC 5545 text escaping: ``\\n`` is a line break, ``\\,`` a literal comma."""
    out: list[str] = []
    escaped = False
    for char in value:
        if escaped:
            out.append("\n" if char in "nN" else char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    return "".join(out)


def _calendar_text(content: str) -> str:
    """The human-readable half of an iCalendar object.

    A meeting invite carries its *when* and *where* only here, and a
    ``multipart/alternative`` invite hides this part from ``get_body()`` and
    ``iter_attachments()`` alike. Everything outside `_ICAL_FIELDS` is machine state
    -- UIDs, sequence numbers, transparency -- so indexing the raw object would spend
    tokens on strings no query will ever match.
    """
    unfolded: list[str] = []
    for raw in content.splitlines():
        if raw[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += raw[1:]
        else:
            unfolded.append(raw)
    lines: list[str] = []
    for line in unfolded:
        name, _, value = line.partition(":")
        label = _ICAL_FIELDS.get(name.partition(";")[0].strip().upper())
        if label and value.strip():
            lines.append(f"{label}: {_unescape_ical(value.strip())}")
    return "\n".join(lines)


def _unreferenced_leaves(message: object, used: set[int]) -> list:
    """Every non-multipart leaf that neither accounting source reached.

    ``get_body()`` and ``iter_attachments()`` are not a partition of the tree:
    ``iter_attachments()`` yields *nothing* for a ``multipart/alternative`` and
    everything-but-the-root for a ``multipart/related``, so an html sibling, a nested
    related branch and the ``text/calendar`` part of a meeting invite were all
    invisible to both -- and left no trace in diagnostics, which is the exact shape
    of loss rule 3 exists for.

    Deliberately **not** ``message.walk()``, which descends into the message a
    ``message/rfc822`` part carries. That subtree belongs to the recursion in
    `_iter_forwarded`, which does its own accounting for it -- collecting it here as
    well emitted the forwarded body twice, measured on a top-level ``message/rfc822``
    wrapper. So a ``message/*`` part is a leaf here whatever it contains.

    Marks what it returns, so the same part cannot be accounted for twice.
    """
    leaves: list = []
    stack = [message]
    while stack:
        part = stack.pop()
        try:
            payload = part.get_payload()  # type: ignore[attr-defined]
            maintype = part.get_content_maintype()  # type: ignore[attr-defined]
        except Exception:
            continue
        if maintype != "message" and isinstance(payload, list):
            stack.extend(reversed(payload))
            continue
        if maintype == "multipart" or id(part) in used:
            continue
        used.add(id(part))
        leaves.append(part)
    return leaves


def _attachment_line(part: object) -> str:
    """The one thing a caller needs to fetch a part diceo deliberately left out."""
    return (
        f"attachment={part.get_filename() or '(unnamed)'} "  # type: ignore[attr-defined]
        f"({part.get_content_type()}, {_part_size(part)} bytes "  # type: ignore[attr-defined]
        f"estimated from encoded payload) -- "
        f"not extracted; diceo's unit of work is one document"
    )


def _is_attachment_leaf(part: object) -> bool:
    """A part the sender attached that ``iter_attachments()`` still does not yield.

    A mail whose *whole body* is a PDF is not multipart, and ``iter_attachments()``
    returns nothing at all for a message that is not multipart -- so the one thing
    the mail is about was reported by nobody.
    """
    if part.get_content_maintype() == "text":  # type: ignore[attr-defined]
        return False
    if part.get_content_type() == "message/rfc822":  # type: ignore[attr-defined]
        return False
    return bool(part.is_attachment() or part.get_filename())  # type: ignore[attr-defined]


def _iter_leaf(
    part: object, report: Diagnostics, used: set[int], depth: int, body_prefix: str
) -> Iterator[Block]:
    """Account for one unreferenced leaf: index it if it is text, count it if not."""
    import io

    kind = part.get_content_type()  # type: ignore[attr-defined]
    if kind == "message/rfc822":
        yield from _iter_forwarded(part, report, used, depth)
        return
    if kind == "text/calendar":
        text = _calendar_text(_part_text(part, report))
        if has_visible_text(text):
            report.chars += len(text)
            report.notes.append("calendar_part=1 (event details indexed)")
            yield Block("paragraph", text, 0, Locator())
        return
    if kind in ("text/plain", "text/html"):
        content = _part_text(part, report)
        if not has_visible_text(content):
            return
        scratch = Diagnostics()
        blocks = (
            _html_blocks_of(content, scratch)
            if kind == "text/html"
            else list(iter_text_blocks(io.BytesIO(content.encode("utf-8")), scratch))
        )
        opening = _opening("\n".join(block.text for block in blocks))
        if opening and opening == body_prefix:
            report.notes.append(f"duplicate_part={kind} (same text as the body)")
            return
        _merge(report, scratch)
        report.notes.append(f"unreferenced_part={kind} (indexed)")
        yield from blocks
        return
    report.truncate(
        f"unreferenced_part=(inline) ({kind}, {_part_size(part)} bytes "
        f"estimated from encoded payload) -- not "
        f"extracted; it is neither the body nor an attachment"
    )


def _iter_forwarded(
    part: object, report: Diagnostics, used: set[int], depth: int
) -> Iterator[Block]:
    """Read a forwarded ``message/rfc822`` part in place, to a bounded depth.

    It used to be reported as an attachment, and reported wrongly: the payload is a
    list, so ``get_payload(decode=True)`` is ``None`` and every forwarded mail read
    ``(unnamed), 0 bytes`` -- a false size, and a name the recovery loop this
    module's docstring documents cannot ask for. A forwarded mail is the same
    document's own quoted history rather than a second document, so it is read.
    Bounded, because nothing stops a message nesting itself all afternoon.
    """
    inner = None
    try:
        payload = part.get_payload()  # type: ignore[attr-defined]
        if isinstance(payload, list) and payload:
            inner = payload[0]
    except Exception:
        inner = None
    size = _part_size(part)
    if inner is None:
        report.truncate(
            f"forwarded_message_unreadable ({size} bytes estimated from encoded payload) -- "
            f"a message/rfc822 part "
            f"with no message in it"
        )
        return
    subject = (_header_values(inner, "subject") or ["(no subject)"])[0][:60]
    if depth >= _RFC822_MAX_DEPTH:
        report.truncate(
            f"rfc822_depth_exceeded={depth + 1} (forwarded message '{subject}', "
            f"{size:,} bytes estimated from encoded payload, not read)"
        )
        return
    report.notes.append(
        f"forwarded_message={subject} ({size:,} bytes estimated from encoded payload, "
        f"read inline)"
    )
    yield from _iter_message(inner, report, used, depth + 1)


def _iter_message(
    message: object,
    report: Diagnostics,
    used: set[int],
    depth: int,
    body: _Body | None = None,
    parts: list | None = None,
) -> Iterator[Block]:
    """Subject, envelope, body, forwarded mail and every remaining leaf, in order."""
    import io

    if body is None:
        body = _choose_body(message, report)
    if parts is None:
        try:
            parts = list(message.iter_attachments())  # type: ignore[attr-defined]
        except Exception:
            parts = []
    used.update(body.seen)
    used.update(id(part) for part in parts)

    for value in _header_values(message, "subject"):
        # A forwarded subject sits under the one that forwarded it, which is what the
        # chunker's breadcrumb wants.
        yield Block("heading", value, min(depth + 1, 6), Locator())
    envelope = [
        f"{label}: {value}"
        for label, field in (
            ("From", "from"),
            ("To", "to"),
            ("Cc", "cc"),
            ("Date", "date"),
        )
        for value in _header_values(message, field)
    ]
    if envelope:
        # One block, because these four lines are a unit and splitting them puts
        # the sender in a different chunk from the date.
        yield Block("paragraph", "\n".join(envelope), 0, Locator())
        report.chars += sum(len(line) for line in envelope)

    # Everything that accounted for content, whether it was indexed or only counted.
    # Zero of them means the message reached the caller as headers and nothing else.
    accounted = 0
    if body.blocks:
        accounted += len(body.blocks)
        yield from body.blocks
    elif body.part is not None:
        # Not counted into `report.chars` here: the reader below counts what it
        # decodes, and counting both doubled every body.
        stream = io.BytesIO(body.text.encode("utf-8"))
        blocks = (
            iter_html_blocks(stream, report, encoding="utf-8")
            if body.subtype == "html"
            else iter_text_blocks(stream, report)
        )
        for block in blocks:
            accounted += 1
            yield block

    attachments = 0
    for part in parts:
        if part.get_content_type() == "message/rfc822":
            for block in _iter_forwarded(part, report, used, depth):
                accounted += 1
                yield block
            continue
        attachments += 1
        report.truncate(_attachment_line(part))

    # Collected before it is drained, so the count is known now: the forwarded parts
    # above have already marked their own subtrees.
    leaves = _unreferenced_leaves(message, used)
    accounted += len(leaves)
    for leaf in leaves:
        if _is_attachment_leaf(leaf):
            # Counted with the others: from the caller's side this is an attachment,
            # whatever the reason `iter_attachments()` had for not yielding it.
            attachments += 1
            report.truncate(_attachment_line(leaf))
            continue
        yield from _iter_leaf(leaf, report, used, depth, body.prefix)

    if attachments:
        report.notes.append(f"attachments={attachments}")

    if not accounted and not attachments:
        # A note rather than `truncate`, and the distinction is exact: every leaf is
        # either a body candidate, an attachment or a reconciled leaf, so reaching
        # here means each one was a body candidate that decoded to nothing. The mail
        # is empty; a mail whose content could not be *read* was already counted
        # above, by whichever branch met it.
        report.notes.append("email_without_body=no part of this message yielded text")


def iter_email_blocks(handle: IO[bytes], report: Diagnostics) -> Iterator[Block]:
    """An RFC 5322 message: the headers that matter, the body a reader would have
    seen, and an honest account of every part that is not in the index.

    **Attachments are reported, never silently dropped.** diceo's unit of work is
    one document, so a PDF attached to a mail is a *second* document
    and extracting it here would make one call do unbounded work. But a mail whose
    only content is its attachment must not look like an empty success -- that is
    the exact failure rule 3 exists for. So every attachment lands in
    ``diagnostics.truncated`` with its name, type and estimated payload size, and
    ``diagnostics.lost_data`` is True. The caller can then feed the bytes back in::

        import email, diceo
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        for part in msg.iter_attachments():
            diceo.chunk(part.get_content(), name=part.get_filename())

    That loop finds every reported attachment but one: a mail that is *not* multipart
    because its whole body is the attachment, where ``iter_attachments()`` yields
    nothing and ``msg.get_content()`` is the file.

    A forwarded ``message/rfc822`` part is the exception: it is one document's own
    quoted history rather than a second document, so it is read in place to
    `_RFC822_MAX_DEPTH` levels.

    Three accounting sources are needed because CPython's two do not cover the tree.
    See `_choose_body` (both alternatives, not the first available) and
    `_unreferenced_leaves` (what neither of them reaches).
    """
    import email
    import email.policy

    try:
        message = email.message_from_binary_file(handle, policy=email.policy.default)
    except Exception as exc:  # the email package raises a wide variety
        from diceo.errors import CorruptDocument

        raise CorruptDocument(f"not a parseable RFC 5322 message ({exc})") from exc

    # Emitted unconditionally, for the same reason the .xlsb/.ods note is: mail is
    # the one remaining format that is *not* streamed, and a caller sizing a worker
    # off the README's "bounded memory" would size it wrong. There is no incremental
    # RFC 5322 parser in the standard library -- `message_from_binary_file` reads the
    # whole message before the first header is available -- and measured on a 160 MB
    # mail that peaks at 984 MB, 6.1x the file. No `Limits` value bounds it, because
    # it is spent before the first block exists.
    report.notes.append(
        "eml is parsed whole (no incremental RFC 5322 parser exists), so peak memory "
        "is a multiple of the file rather than bounded by Limits; attachments are "
        "sized without decoding them"
    )

    body = _choose_body(message, report)
    try:
        parts = list(message.iter_attachments())
    except Exception:
        parts = []

    # An .eml has no magic number, so detection is a heuristic over the first few
    # lines -- and Python's parser never fails: it treats any leading `Word:` lines
    # as headers and whatever follows as the body. When *every* leading line looks
    # like a header, the body comes out empty and the "message" is really a text
    # file. A log of repeated `Date: ...` lines was reaching this path and losing
    # everything but its first line, because duplicate headers collapse to the first
    # and we only emit four of them.
    #
    # The fix is not a longer list of header names -- Tika has been tuning that list
    # for a decade and two of its fix titles read "again" and "yet again". It is to
    # use the outcome: **no body text and no attachments means this was not mail.**
    # Re-read the bytes as plain text, where every line survives.
    #
    # Gated on `_probably_never_mail`, because the outcome test alone is not enough:
    # real mail can also produce no body -- a statement whose only part is a PDF, an
    # html newsletter behind an empty plain stub -- and re-reading *that* as text
    # indexes the MIME boundaries and the base64 payload while the real content still
    # never lands. Mail that yields nothing stays mail, and says so in diagnostics.
    if (
        not has_visible_text(body.text)
        and not body.blocks
        and not parts
        and _probably_never_mail(message)
    ):
        try:
            handle.seek(0)
        except (OSError, ValueError):  # unseekable stream: nothing to fall back to
            report.notes.append("email_without_body")
        else:
            report.format = "text"
            report.notes.append("email_sniff_rejected=no body and no attachments")
            yield from iter_text_blocks(handle, report)
            return

    yield from _iter_message(message, report, set(), 0, body, parts)
