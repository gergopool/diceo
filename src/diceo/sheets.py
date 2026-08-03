"""Spreadsheets: a streaming row reader, and the chunking that makes a sheet findable.

Two halves, deliberately separable.

**The reader** (:func:`iter_rows`) is stdlib only -- ``zipfile`` plus
``ElementTree.iterparse`` over the worksheet part -- and it is a generator, so a
1M-row sheet never exists as Python objects. It is here rather than in a faster
form because the fast form is a byte scanner and the two must be measured against
each other before one ships -- experiment 006 (office fast paths), run in the
research repository. The reader is written so the backend can be swapped without
the chunker noticing: everything below consumes ``Iterable[Row]``.

**The chunker** (:func:`iter_sheet_chunks`) implements decision D6. Chunking a
sheet is not chunking prose, and the usual two answers are both wrong: one chunk
per row destroys the context that makes a row mean anything and explodes the
index, while whole-sheet markdown is unbounded and useless past a few hundred
rows. Instead:

1. a **sheet summary chunk** -- name, flattened column headers, inferred dtypes,
   row count, sample values. On a 1M-row export this is the chunk that answers
   "what is in this file", which is the question users actually ask, and no
   competitor emits it;
2. **row-group chunks with the header re-prefixed into every group**, so a group
   is readable on its own by an embedder and by an LLM;
3. **honest uncertainty** on the layouts that really occur -- merged multi-row
   headers, a title block above the table, two tables side by side. The header
   detector reports what it believes and how sure it is, rather than pretending.

The summary chunk is emitted **first**, which the format makes possible only
because two things sit near the top of the worksheet part: ``<dimension>`` gives
the row count before any row is read, and a bounded look-ahead of
:data:`SAMPLE_ROWS` rows gives the dtypes and samples. Both are checked against
what the stream actually delivered, and a disagreement is a diagnostic rather
than a correction -- ``dimension`` is producer-written and allowed to lie.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from xml.etree.ElementTree import iterparse

from diceo._diagnostic_text import bounded_list, images_note
from diceo._zip_parts import MAX_DEPTH as _MAX_DEPTH
from diceo._zip_parts import MAX_OPEN_ELEMENTS as _MAX_OPEN_ELEMENTS
from diceo._zip_parts import MEDIA_DIR as _MEDIA_DIR
from diceo._zip_parts import OFF_REL as _OFF_REL
from diceo._zip_parts import dtd_free_stream as _dtd_free_stream
from diceo._zip_parts import refuse_dtd as _refuse_dtd
from diceo._zip_parts import refuse_shape as _refuse_shape
from diceo.plaintext import count_controls

__all__ = [
    "Row",
    "SheetChunk",
    "SheetDiagnostics",
    "SheetProfile",
    "iter_rows",
    "iter_sheet_chunks",
    "sheet_names",
]

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
#: ISO 29500 **Strict** -- Excel's "Strict Open XML Spreadsheet" save option, and what
#: some public-sector archives require. Same elements, different namespace. Every tag
#: here was built from the Transitional namespace above, so a Strict workbook matched
#: nothing and produced zero chunks with `lost_data=False`: a whole valid file silently
#: absent from the index.
#:
#: Rather than match two namespaces per element in the hot loop, each part's namespace
#: is read off its **root element**. That costs one string operation per part, handles
#: any spelling including a future one, and keeps the per-row path untouched.
_STRICT_MAIN = "{http://purl.oclc.org/ooxml/spreadsheetml/main}"


def _namespace_of(tag: str) -> str:
    """``"{urn:x}worksheet"`` -> ``"{urn:x}"``; a bare tag -> ``""``."""
    if tag.startswith("{"):
        return tag[: tag.index("}") + 1]
    return ""


# How many leading rows the header detector and the dtype sampler may look at.
# It is a constant rather than a fraction of the sheet because it has to stay O(1):
# the whole point is that a 1M-row sheet costs the same as a 100-row one.
SAMPLE_ROWS = 12

# Excel's calendar contains a 1900-02-29 that never existed, so counting days from
# 1899-12-31 reproduces the off-by-one below serial 60 that the format requires.
_SERIAL_EPOCH = date(1899, 12, 31)
# Excel for Mac: serial 0 is 1904-01-01 and there is no phantom leap day.
_EPOCH_1904 = date(1904, 1, 1)
_MILLIS_PER_DAY = 86_400_000
_MAX_SERIAL = 2958466.0  # past 9999-12-31 there is no date to render

# Builtin number-format ids that mean "this number is a date/time" (ECMA-376
# §18.8.30). Two blocks, and the second one is easy to miss: 14-22 and 45-47 are the
# western date and time formats, while **27-36 and 50-58** are the Japanese, Chinese,
# Korean and Thai date and era formats. Excel references those by id and writes no
# `<numFmt>` element for them, so a workbook authored in any of those locales had
# every date reach the index as a bare serial -- `45292` matches no query a human
# writes, which makes it a rule-3 loss rather than a formatting nicety.
#
# A deliberate divergence from the experimental byte scanner, which excludes the locale
# block to stay cell-for-cell with calamine (calamine does not implement it either).
# That scanner is held to cell-for-cell agreement with calamine by a gate in the
# research repository; this reader is not part of that gate and already diverges on
# builtin 46.
#
# Ids that look adjacent but are not dates stay out: 23-26 are reserved, 37-44 are
# currency and accounting, 48 is scientific and 49 is text.
_BUILTIN_DATE_FORMATS = frozenset(
    {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47} | set(range(27, 37)) | set(range(50, 59))
)


class Row(NamedTuple):
    """One sheet row. ``cells`` starts at column A; interior gaps are ``""``.

    A NamedTuple because there is one per row and a 1M-row sheet must not pay for
    a dataclass's ``__dict__``. ``number`` is the sheet's own 1-based ``r``
    attribute, so a gap in the numbering is a real gap in the sheet rather than a
    reader artefact.
    """

    sheet: str
    number: int
    cells: list[str]


@dataclass
class SheetDiagnostics:
    """What the reader saw. Rule 3: every loss is counted, never just logged."""

    sheets: int = 0
    rows: int = 0
    cells: int = 0
    shared_strings: int = 0
    date_cells: int = 0
    error_cells: int = 0
    #: Formula cells carrying no cached result. Unavoidable without evaluating the
    #: formula -- any workbook last written by a library has none -- so it is counted
    #: rather than silently blank: a caller seeing a high count knows the file is a
    #: template rather than data.
    formula_cells_unevaluated: int = 0
    #: Cells whose shared-string index was out of range or empty, so their text is
    #: unreachable. calamine resolved the empty case to index 0 and stamped the first
    #: string of the table into every such cell -- confidently wrong text.
    shared_string_misses: int = 0
    #: Control characters removed from cell text. A sheet is the one format that can
    #: carry them *legally*: ``_xHHHH_`` (ECMA-376 18.4.12) exists to smuggle exactly
    #: the characters XML forbids, so a NUL reached `Chunk.embed_text` here while
    #: every other reader stripped it -- and PostgreSQL rejects U+0000 in ``text``
    #: and ``jsonb``, which turned our success into the caller's exception.
    control_chars_removed: int = 0
    #: Attributes holding something the format does not allow there -- a row's ``r``
    #: that is not a row number, a cell's ``s`` that is not a style index, a
    #: ``mergeCell`` whose reference is not a range. Each of these used to raise a
    #: ``ValueError`` *out of the row stream* and truncate the document at that row:
    #: ``r="abc"`` cost every row after it, and a 5,000-digit row number reached the
    #: caller as `use sys.set_int_max_str_digits()`, which is advice about CPython
    #: rather than about their file. Now the attribute is ignored and counted -- the
    #: row keeps its values, and what is lost is its *number* (rows after it are
    #: numbered by position) or the knowledge that a cell was formatted as a date.
    malformed_attributes: int = 0
    #: Cells whose ``r`` attribute names no column (``r="7"``) or a column past the
    #: format's XFD maximum. Their value is kept -- appended at the end of the row --
    #: because it is real content, but its *position* is lost and the caller has to be
    #: able to see that a column ordering they are relying on may be wrong.
    invalid_cell_references: int = 0
    # `dimension` said one thing and the stream delivered another. Not corrected,
    # because either could be right and the caller deserves to know.
    dimension_mismatch: list[tuple[str, int, int]] = field(default_factory=list)
    sheets_without_part: list[str] = field(default_factory=list)
    #: ``(sheet name, state)`` for every sheet whose ``state`` is ``hidden`` or
    #: ``veryHidden``. Read anyway -- rule 3 forbids dropping real content -- but
    #: surfaced, because "very hidden" cannot be un-hidden from Excel's UI and is
    #: therefore disproportionately stale internal data the caller may want to skip.
    sheets_hidden: list[tuple[str, str]] = field(default_factory=list)
    #: A sheet that exists and holds nothing. Distinct from `sheets_without_part`:
    #: that one is a broken workbook, this one is an empty tab, and a caller
    #: triaging failures needs to tell them apart.
    sheets_without_rows: list[str] = field(default_factory=list)
    #: ``(sheet name, count)`` for merge ranges spanning more than one row. A merged
    #: range stores its value in the top-left cell only, so the rows below the anchor
    #: are read blank in that column and a category key can end up attributed to one
    #: row out of five. Counted rather than forward-filled -- see
    #: `tests/test_xlsx_merged_cells.py` for the measurement behind that choice --
    #: because rule 3 is about the loss being *visible*, not about guessing.
    #: Horizontal-only ranges are excluded: their blanks sit to the right of the
    #: value and `_flatten` already resolves them in a header.
    vertical_merges: list[tuple[str, int]] = field(default_factory=list)
    truncated: list[tuple[str, int, int]] = field(default_factory=list)
    #: Free-text observations that are not losses -- a non-comma delimiter, a
    #: backend that could not stream. Surfaced through `Diagnostics.notes`.
    notes: list[str] = field(default_factory=list)


@dataclass
class SheetProfile:
    """What the chunker believes about a sheet's shape, and how sure it is.

    ``confidence`` is the field that keeps this honest. ``"clean"`` means one
    header row over a rectangle. ``"merged-header"`` means several header rows
    were flattened down the column. ``"uncertain"`` means the detector found no
    convincing header and is treating every row as data -- which is the correct
    answer for a sheet of prose, and the answer competitors paper over.
    """

    sheet: str
    title: str | None
    header: list[str]
    header_rows: int
    first_data_row: int
    confidence: str
    #: One entry per column, and -- whenever ``header`` is non-empty -- **exactly as
    #: many entries as ``header`` has labels**. The two used to be sized
    #: independently (the header from the header row, the dtypes from the modal body
    #: width), so a header overhanging its table paired the two through a
    #: ``zip(strict=False)`` and dropped the trailing column names from the summary
    #: chunk. Columns past the body type as ``"empty"``.
    dtypes: list[str] = field(default_factory=list)
    samples: list[list[str]] = field(default_factory=list)
    declared_rows: int | None = None
    declared_columns: int | None = None
    notes: list[str] = field(default_factory=list)
    # Rows above the header that are not part of the table -- a report's
    # "Prepared by / Period / Status" block. Real content, so it is carried into
    # the summary chunk rather than dropped (rule 3).
    preamble: list[str] = field(default_factory=list)
    preamble_rows: int = 0


@dataclass
class SheetChunk:
    """A retrieval-ready piece of a sheet.

    ``kind`` is ``"sheet_summary"`` or ``"row_group"``. ``text`` is what gets
    embedded; ``first_row``/``last_row`` are the sheet's own row numbers, so a
    chunk can be pointed back at the cells it came from (D8: identity includes
    position, never content alone -- two row groups of a sparse sheet are
    routinely byte-identical).
    """

    kind: str
    sheet: str
    text: str
    first_row: int = 0
    last_row: int = 0
    rows: int = 0


# --------------------------------------------------------------------------- #
# Package layout
# --------------------------------------------------------------------------- #


@dataclass
class _Layout:
    sheets: list[tuple[str, str | None]]
    #: ``{sheet name: state}`` for the non-visible sheets only.
    hidden: dict[str, str]
    shared_strings: str | None
    styles: str | None
    #: ``<workbookPr date1904="1"/>`` -- Excel for Mac's calendar. Ignoring it shifts
    #: every date in the workbook by 1462 days and still yields a valid-looking date.
    epoch_1904: bool = False


def _resolve(target: str) -> str:
    """A workbook relationship target, resolved against ``xl/``."""
    if target.startswith("/"):
        return target[1:]
    if target.startswith("../"):
        return target[3:]
    return f"xl/{target}"


def _layout(archive: zipfile.ZipFile) -> _Layout:
    """Sheet order from ``workbook.xml``, part paths from its relationships.

    Sorting ``xl/worksheets/sheet*.xml`` is the tempting shortcut and it is wrong
    twice over: the map from sheet position to filename is arbitrary, and
    ``sheet10`` sorts before ``sheet2``.
    """
    names = set(archive.namelist())
    targets: dict[str, str] = {}
    shared = styles = None
    try:
        rels = archive.read("xl/_rels/workbook.xml.rels")
    except KeyError:
        rels = b""
    if rels:
        from xml.etree.ElementTree import fromstring

        _refuse_dtd(rels, name=archive.filename or "", part="xl/_rels/workbook.xml.rels")
        for element in fromstring(rels):
            kind = element.get("Type", "").rsplit("/", 1)[-1]
            target = _resolve(element.get("Target", ""))
            if kind in {"worksheet", "chartsheet", "dialogsheet"}:
                targets[element.get("Id", "")] = target
            elif kind == "sharedStrings":
                shared = target
            elif kind == "styles":
                styles = target

    sheets: list[tuple[str, str | None]] = []
    hidden: dict[str, str] = {}
    epoch_1904 = False
    try:
        book = archive.read("xl/workbook.xml")
    except KeyError:
        book = b""
    if book:
        from xml.etree.ElementTree import fromstring

        _refuse_dtd(book, name=archive.filename or "", part="xl/workbook.xml")
        root = fromstring(book)
        main = _namespace_of(root.tag) or _MAIN
        for element in root.iter(f"{main}workbookPr"):
            epoch_1904 = element.get("date1904", "").strip().lower() in {"1", "true"}
            break
        for element in root.iter(f"{main}sheet"):
            name = element.get("name", "")
            state = element.get("state", "visible")
            if state != "visible":
                hidden[name] = state
            # `r:id` is itself namespaced, and Strict spells that namespace
            # differently too, so the attribute is found by its local name.
            reference = element.get(f"{{{_OFF_REL}}}id") or element.get("id") or ""
            if not reference:
                reference = next(
                    (v for k, v in element.attrib.items() if k.rpartition("}")[2] == "id"),
                    "",
                )
            part = targets.get(reference)
            sheets.append((name, part if part in names else None))

    if not sheets:
        numbered = sorted(
            (n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[-1]).group(1)),
        )
        sheets = [(Path(n).stem, n) for n in numbered]
    if shared not in names:
        shared = "xl/sharedStrings.xml" if "xl/sharedStrings.xml" in names else None
    if styles not in names:
        styles = "xl/styles.xml" if "xl/styles.xml" in names else None
    return _Layout(sheets, hidden, shared, styles, epoch_1904)


def sheet_names(path: str | Path) -> list[str]:
    """Sheet names in workbook order. Reads two small parts, not the sheets."""
    with zipfile.ZipFile(path) as archive:
        return [name for name, _ in _layout(archive).sheets]


# --------------------------------------------------------------------------- #
# Shared strings and number formats
# --------------------------------------------------------------------------- #


#: ``_xHHHH_`` -- how OOXML carries a character XML itself forbids (ECMA-376 18.4.12).
_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def unescape_cell(text: str, report: SheetDiagnostics | None = None) -> str:
    """Decode ``_xHHHH_`` escapes, including the doubly-escaped literal.

    Decoding is also the one route by which a control character enters a chunk, so
    the result is cleaned here rather than at the call sites: ``_x0000_`` is a legal
    way to write a NUL, and the escape exists precisely because XML cannot hold one
    literally. Tab, newline and the carriage return that separates a cell's two
    lines all survive -- they are not in the stripped set.

    A cell holding two lines is stored as ``line1_x000D_line2``. Undecoded, that is
    one token with a hex marker in the middle: neither line can be found, and the
    marker pollutes the vocabulary.

    The subtlety is that a cell whose *real* text is the seven characters
    ``_x000D_`` is stored as ``_x005F_x000D_`` -- the underscore is itself escaped.
    A single left-to-right pass gets both cases right for free: the leading
    ``_x005F_`` matches first and yields ``_``, and scanning resumes *after* it, so
    the remaining ``x000D_`` stays literal. Decoding repeatedly, or right-to-left,
    would collapse the two cases into one and make them indistinguishable -- which
    is the openpyxl bug (issue 2099, still open).
    """
    if "_x" not in text:
        return text
    decoded = _ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    cleaned, removed = count_controls(decoded)
    if removed and report is not None:
        report.control_chars_removed += removed
    return cleaned


def _si_text(element: object, text_tag: str, run_tag: str) -> str:
    """The text of one ``<si>``, without its pronunciation guide.

    ``CT_Rst`` holds ``t?``, ``r*`` (formatting runs), ``rPh*`` (phonetic hints) and
    ``phoneticPr?``. Collecting every descendant ``<t>`` -- the obvious one-liner --
    picks up ``rPh`` too, so a Japanese cell reading 東京 with the ruby gloss
    トウキョウ came out as **東京トウキョウ**: a token that matches neither the name nor
    its reading. calamine shipped exactly this and fixed it in 0.16.2.

    Walking only ``r/t`` instead is the opposite error: it drops a leading bare
    ``<t>`` when a user bolds the *second* half of a string (calamine 636). Both
    children are handled here, in document order, which is the same fork the OOXML
    survey found in ``w:p`` -- walk everything and duplicate, walk too narrowly and
    lose.
    """
    parts: list[str] = []
    for child in element:  # type: ignore[attr-defined]
        if child.tag == text_tag:
            parts.append(child.text or "")
        elif child.tag == run_tag:
            for node in child:
                if node.tag == text_tag:
                    parts.append(node.text or "")
    return "".join(parts)


def _shared_strings(
    archive: zipfile.ZipFile, part: str | None, report: SheetDiagnostics
) -> list[str]:
    """The interning table, as a list indexed by a cell's ``<v>``.

    This is the one thing that must be materialised, and it is bounded by the
    number of *distinct* strings rather than by the row count -- a 1M-row export
    of nine metric names costs nine objects. Rich-text runs concatenate.
    """
    if part is None:
        return []
    table: list[str] = []
    string_tag = f"{_MAIN}si"
    root_tag = f"{_MAIN}sst"
    text_tag = f"{_MAIN}t"
    run_tag = f"{_MAIN}r"
    with archive.open(part) as stream:
        guarded = _dtd_free_stream(stream, name=archive.filename or "", part=part)
        parent = None
        first = True
        #: See `_MAX_DEPTH` and `_MAX_OPEN_ELEMENTS`.
        depth = 0
        open_elements = 0
        #: Hoisted into locals: both are read once per element, and a module global
        #: costs a dict lookup where a local costs an array index. Measured at ~4%
        #: of the whole docx and xlsx read before this line existed.
        max_depth = _MAX_DEPTH
        max_open = _MAX_OPEN_ELEMENTS
        for event, element in iterparse(guarded, ("start", "end")):
            if event == "start":
                depth += 1
                open_elements += 1
                if depth > max_depth or open_elements > max_open:
                    _refuse_shape(depth, open_elements, name=archive.filename or "", part=part)
                if first:
                    first = False
                    found = _namespace_of(element.tag)
                    if found != _MAIN:
                        # ISO 29500 Strict, or any other spelling. See _STRICT_MAIN.
                        string_tag, root_tag = found + "si", found + "sst"
                        text_tag, run_tag = found + "t", found + "r"
                if element.tag == root_tag:
                    parent = element
                continue
            depth -= 1
            if element.tag != string_tag:
                continue
            table.append(unescape_cell(_si_text(element, text_tag, run_tag), report))
            element.clear()
            open_elements = 0
            # Same unlink as the row loop: clear() alone leaves an empty Element per
            # entry attached to the root, so a workbook with many distinct strings
            # pays twice for its vocabulary.
            if parent is not None and len(parent) > 64:
                del parent[:]
    return table


def _is_date_code(code: str) -> bool:
    """Does a number-format code render its value as a date or time?

    A format code is a mini-language, and the traps are all about characters that
    *look* like date tokens but are not: quoted literals (``"day "0`` is a
    number), bracketed colour/locale sections, and backslash escapes.
    """
    index, length = 0, len(code)
    while index < length:
        char = code[index]
        if char == '"':
            index = code.find('"', index + 1)
            if index < 0:
                return False
            index += 1
        elif char == "[":
            close = code.find("]", index + 1)
            if close < 0:
                return False
            if code[index + 1 : index + 2].lower() in {"h", "m", "s"}:
                return True  # [h]:mm -- an elapsed duration, still time-shaped
            index = close + 1
        elif char in "\\_":
            index += 2
        else:
            if char in "yYmMdDhHsS":
                return True
            index += 1
    return False


def _date_styles(archive: zipfile.ZipFile, part: str | None) -> set[int]:
    """Style indices whose cells are dates.

    ``styles.xml`` is bounded by the number of distinct formats in the workbook,
    never by its row count, so reading it whole is safe.
    """
    if part is None:
        return set()
    from xml.etree.ElementTree import fromstring

    raw = archive.read(part)
    _refuse_dtd(raw, name=archive.filename or "", part=part)
    root = fromstring(raw)
    main = _namespace_of(root.tag) or _MAIN
    custom: dict[int, bool] = {}
    for element in root.iter(f"{main}numFmt"):
        try:
            custom[int(element.get("numFmtId", "-1"))] = _is_date_code(
                element.get("formatCode", "")
            )
        except ValueError:
            continue
    dated: set[int] = set()
    for block in root.iter(f"{main}cellXfs"):
        for index, element in enumerate(block):
            try:
                number_format = int(element.get("numFmtId", "0"))
            except ValueError:
                continue
            if custom.get(number_format, number_format in _BUILTIN_DATE_FORMATS):
                dated.add(index)
    return dated


# A stored number only needs normalising when it carries float noise, and that is
# detectable by length alone: "81.26000000000001" is 17 characters, every honest
# authored decimal in a real export is far shorter. Gating on this keeps the
# float round-trip off the hot path for ~all cells while still fixing the ones
# that would otherwise put an unmatchable token in the index.
_NOISY_NUMBER_LENGTH = 13


def _clean_number(raw: str) -> str:
    """Strip IEEE-754 noise from a stored number, leaving the authored value.

    Writers serialise a double with more digits than it deserves --
    ``round(x, 2)`` reaches the sheet as ``81.26000000000001`` -- and indexing that
    is a retrieval bug, not a cosmetic one: a user searching for ``81.26`` gets no
    match, and the extra digits are junk tokens in the embedding. Python's
    ``repr`` is the shortest string that round-trips to the same double, which is
    exactly the normalisation wanted (and the same answer calamine gives).

    The three ``in`` tests are spelled out rather than written as
    ``any(char in raw for char in ".eE")``: this runs once per numeric cell, and a
    generator that yields three items costs a frame plus three resumptions where
    three C-level scans cost nothing. Measured over 200 stored numbers, 81.7 us to
    51.2 us -- same answer, same short-circuit order.
    """
    if len(raw) < _NOISY_NUMBER_LENGTH or not ("." in raw or "e" in raw or "E" in raw):
        return raw
    try:
        return repr(float(raw))
    except ValueError:
        return raw


def _serial_text(raw: str, *, epoch_1904: bool = False) -> str:
    """An Excel serial under a date format, rendered ISO-ish.

    Emitting the bare serial instead ("45292" for 2024-01-01) is silent
    corruption of every date column, so this is correctness, not cosmetics.
    """
    try:
        serial = float(raw)
    except ValueError:
        return raw
    if serial >= _MAX_SERIAL:
        return raw
    if serial < 1.0:  # no whole day: a time of day
        millis = round((serial % 1.0) * _MILLIS_PER_DAY)
        return str((datetime.min + timedelta(milliseconds=millis)).time())
    whole = int(serial)
    if epoch_1904:
        # 1904 has no phantom leap day, so there is no off-by-one to undo.
        day = _EPOCH_1904 + timedelta(days=whole)
    else:
        day = _SERIAL_EPOCH + timedelta(days=whole if whole < 60 else whole - 1)
    millis = round((serial - whole) * _MILLIS_PER_DAY)
    if not millis:
        return day.isoformat()
    return (datetime(day.year, day.month, day.day) + timedelta(milliseconds=millis)).isoformat(
        sep=" "
    )


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #


#: ``<mergeCell ref="C77:O82"/>``. Only the two row numbers matter here: a range
#: whose bottom row is below its top row leaves every row after the first blank in
#: the merged column. Anything the pattern cannot read is ignored rather than
#: guessed at -- a malformed reference must not raise inside the row stream.
#:
#: The digit count is bounded here rather than checked after the match, because that
#: promise was not being kept: ``\d+`` matched ``A1:B`` followed by 5,000 nines, and
#: the ``int()`` on it raised out of the row loop and truncated the sheet at whatever
#: row was pending -- a decorative element deleting real data. 2^20 rows is seven
#: digits, so a longer number is not a row number and the range is simply not read.
_MERGE_SPAN = re.compile(r"^[A-Za-z]+(\d{1,7}):[A-Za-z]+(\d{1,7})$")


#: Columns A..XFD -- the hard maximum SpreadsheetML allows (ECMA-376 §18.3.1.4). A
#: reference past it is not a big spreadsheet, it is a malformed one, and the number
#: matters because the row builder pads up to the index it is given: a **1.7 KB** file
#: naming ``r="XFDXFDXFD1"`` asked for a list of 10^14 entries and took the worker out
#: with a `MemoryError` -- which is not a `DiceoError`, so the caller's quarantine
#: handler never ran. Bounded input, bounded work (rule 2).
MAX_COLUMNS = 16_384


#: Trailing characters of a cell reference that carry no column information.
_ROW_DIGITS = "0123456789"


def _column_index(reference: str, cache: dict[str, int] | None = None) -> int:
    """``"BC12"`` -> 54. Letters only; the row number is ignored.

    Returns ``-1`` when the reference names no column at all. That case is not
    hypothetical and it used to be silent: ``r="7"`` gave ``index = -1``, and
    ``cells[-1] = value`` **overwrote the previous cell**, so a row reading
    ``FIRST | SECOND | CLOBBER`` came out as ``FIRST | CLOBBER`` with the middle value
    gone from the index and ``lost_data`` False.

    ``cache`` memoises on the *column letters* and is the reason this is not the
    per-character Python loop it reads as. A worksheet names the same handful of
    columns on every row -- ``A1``, ``A2``, ``A3`` all answer ``0`` -- so stripping
    the row number with one C-level ``rstrip`` and looking the letters up turns a
    loop over every character of every reference into a dict hit from the second row
    on: **1.91x** over 1,560 references naming 40 columns. The answer depends only on
    the leading alphabetic run, and a key is stored only when the stripped tail was
    non-empty and the head is entirely alphabetic, so the letters determine the value
    and a malformed reference like ``A1B2`` falls through to the scan every time.

    The cache belongs to **one worksheet part** and is bounded by
    ``MAX_COLUMNS`` -- at most one entry per column the format has -- so it cannot
    grow with the row count, cannot outlive the sheet, and is not shared between
    threads reading different workbooks.
    """
    if cache is not None:
        letters = reference.rstrip(_ROW_DIGITS)
        found = cache.get(letters)
        if found is not None:
            return found
    index = 0
    seen = False
    for char in reference:
        if not char.isalpha():
            break
        seen = True
        index = index * 26 + (ord(char.upper()) - 64)
        if index > MAX_COLUMNS:
            return MAX_COLUMNS  # clamped; the caller reports it rather than padding
    resolved = index - 1 if seen else -1
    if (
        cache is not None
        and len(cache) < MAX_COLUMNS
        and len(letters) < len(reference)
        and letters.isalpha()
    ):
        cache[letters] = resolved
    return resolved


#: Distinct from the -1 that means "this cell has no style at all", because 0 is a
#: real style index and a cell with no `s` must not be read as style 0.
_BAD_STYLE = -2


def _style_index(raw: str) -> int:
    """``s="3"`` -> 3; anything that is not an index -> :data:`_BAD_STYLE`.

    ``s`` is producer-written and the ``int()`` on it sat unguarded in the cell loop,
    so ``s="zz"`` raised a ``ValueError`` out of the row stream and cost the document
    every row after that cell. What is lost by not knowing the style is one cell's
    date *rendering* -- it reaches the index as ``45292`` rather than ``2024-01-01``,
    which is the rule-3 loss `_BUILTIN_DATE_FORMATS` exists to prevent, so it is
    counted. What was lost by raising was the rest of the sheet.
    """
    try:
        return int(raw)
    except ValueError:
        return _BAD_STYLE


def _dimension(archive: zipfile.ZipFile, part: str) -> tuple[int | None, int | None]:
    """``<dimension ref="A1:L1000001"/>``, read without parsing the sheet.

    It sits in the first few hundred bytes of the part, before ``sheetData``,
    which is what lets the summary chunk be emitted first. It is written by the
    producer and is allowed to be absent, stale or wrong -- callers get it as a
    *declared* value and the reader checks it against reality afterwards.
    """
    with archive.open(part) as stream:
        head = stream.read(4096)
    found = re.search(rb'<dimension\s+ref="([^"]+)"', head)
    if not found:
        return None, None
    reference = found.group(1).decode("ascii", "replace")
    end = reference.split(":")[-1]
    rows = re.search(r"(\d+)", end)
    return (int(rows.group(1)) if rows else None, _column_index(end) + 1)


def _iter_part_rows(
    archive: zipfile.ZipFile,
    part: str,
    name: str,
    strings: list[str],
    dated: set[int],
    epoch_1904: bool,
    report: SheetDiagnostics,
    max_rows: int | None,
) -> Iterator[Row]:
    """Rows of one worksheet part, streamed with ``iterparse`` + ``clear()``.

    ``clear()`` on the row is necessary but **not sufficient**, and the difference
    is the whole reason "just use iterparse" is not automatically streaming.
    ``clear()`` empties the row element, but the element itself stays in its
    parent's child list, so ``sheetData`` accumulates one empty ``Element`` per
    row and peak RSS grows linearly with the sheet after all. Measured here on
    ``sheet-big.xlsx``: 105 MB with only ``clear()``, 31 MB once the emptied rows
    are detached from the parent (``del parent[:]``).

    So the row is cleared *and* unlinked. The cell elements are cleared with the
    row rather than individually, because a per-cell Python call buys nothing --
    a row is already bounded.

    ``<mergeCells>`` is read on the way past, for the count only. It is serialised
    *after* ``<sheetData>``, which is why the header detector cannot consult it
    (see :func:`_flatten`) and equally why the count is not available until the
    sheet has been drained -- a run cut short by ``max_rows`` never reaches it.
    """
    cell_tag = f"{_MAIN}c"
    row_tag = f"{_MAIN}row"
    value_tag = f"{_MAIN}v"
    inline_tag = f"{_MAIN}is"
    text_tag = f"{_MAIN}t"
    merges_tag = f"{_MAIN}mergeCells"
    emitted = 0
    number = 0
    #: Column letters -> index, for this part only. See :func:`_column_index`.
    columns: dict[str, int] = {}

    with archive.open(part) as stream:
        guarded = _dtd_free_stream(stream, name=archive.filename or "", part=part)
        # "start" events are subscribed to only to capture the sheetData element --
        # the handle needed to drop accumulated rows. Captured once, not looked up
        # per row, because a find() per row is a linear scan of what we are trying
        # to keep short.
        sheet_data_tag = f"{_MAIN}sheetData"
        formula_tag = f"{_MAIN}f"
        parent = None
        first = True
        #: See `_MAX_DEPTH` and `_MAX_OPEN_ELEMENTS`. One `<row>` of 1.3 million
        #: `<c>` and a `<mergeCells>` of two million ranges are both bounded by the
        #: second: neither ever reaches the `clear()` that would release them.
        depth = 0
        open_elements = 0
        #: Hoisted into locals: both are read once per element, and a module global
        #: costs a dict lookup where a local costs an array index. Measured at ~4%
        #: of the whole docx and xlsx read before this line existed.
        max_depth = _MAX_DEPTH
        max_open = _MAX_OPEN_ELEMENTS
        for event, element in iterparse(guarded, ("start", "end")):
            # Hoisted: every event reads the tag at least once, and a non-row end
            # event -- one per cell, so most of them -- used to read it twice.
            tag = element.tag
            if event == "start":
                depth += 1
                open_elements += 1
                if depth > max_depth or open_elements > max_open:
                    _refuse_shape(depth, open_elements, name=archive.filename or "", part=part)
                if first:
                    first = False
                    found = _namespace_of(tag)
                    if found != _MAIN:
                        # ISO 29500 Strict, or any other spelling. See _STRICT_MAIN.
                        cell_tag, row_tag = found + "c", found + "row"
                        value_tag, inline_tag = found + "v", found + "is"
                        text_tag = found + "t"
                        sheet_data_tag, formula_tag = found + "sheetData", found + "f"
                        merges_tag = found + "mergeCells"
                if tag == sheet_data_tag:
                    parent = element
                continue
            depth -= 1
            if tag != row_tag:
                # One extra identity comparison per non-row end event, and it is the
                # only thing outside `sheetData` this loop looks at. `<mergeCells>`
                # arrives once, after every row, and holds one child per range.
                if tag == merges_tag:
                    vertical = 0
                    for child in element:
                        span = _MERGE_SPAN.match(child.get("ref", ""))
                        if span is not None and int(span.group(2)) > int(span.group(1)):
                            vertical += 1
                    if vertical:
                        report.vertical_merges.append((name, vertical))
                    element.clear()
                    open_elements = 0
                continue
            raw = element.get("r")
            if not raw:
                number += 1
            else:
                try:
                    number = int(raw)
                except ValueError:
                    # `r="abc"`, or 5,000 digits. Counted, and the row is numbered by
                    # position: its cells are real content and losing them to a bad
                    # attribute is the trade rule 3 forbids.
                    report.malformed_attributes += 1
                    number += 1
            cells: list[str] = []
            for cell in element:
                if cell.tag != cell_tag:
                    continue
                reference = cell.get("r")
                # A cell belongs to the column its reference names, not to its turn in
                # the file. The padding here used to be one-directional -- extend when
                # the index runs ahead, otherwise fall through to `append` -- so a row
                # written C then A came out as ['', '', 'THIRD', 'FIRST']: a phantom
                # fourth column and every value under the wrong header. Excel writes
                # cells in order; the spec does not require it and other producers do
                # not. `r` is optional, and without it the column *is* the position.
                index = (
                    _column_index(reference, columns) if reference is not None else len(cells)
                )
                kind = cell.get("t")
                if kind == "inlineStr":
                    node = cell.find(inline_tag)
                    value = (
                        unescape_cell(
                            "".join(part.text or "" for part in node.iter(text_tag)), report
                        )
                        if node is not None
                        else ""
                    )
                else:
                    node = cell.find(value_tag)
                    text = node.text if node is not None and node.text is not None else ""
                    if kind == "s":
                        try:
                            value = strings[int(text)]
                        except (IndexError, ValueError):
                            report.shared_string_misses += 1
                            value = ""
                    elif kind == "b":
                        # No cached value means the cell is *empty*, not false.
                        # Rendering FALSE there fabricates data, and a fabricated
                        # value is worse than a blank because nothing downstream can
                        # tell it from a real one.
                        value = ("TRUE" if text == "1" else "FALSE") if text else ""
                    elif kind == "e":
                        # An Excel error cell has no value to index. Count it so the
                        # loss is visible rather than silent, and keep the column, or
                        # every later value shifts one place left.
                        report.error_cells += 1
                        value = ""
                    elif kind is None or kind == "n":
                        if node is None and cell.find(formula_tag) is not None:
                            report.formula_cells_unevaluated += 1
                            value = ""
                        else:
                            style = cell.get("s")
                            # `dated` short-circuits first, so a workbook with no date
                            # formats at all never pays for any of this. Not named
                            # `index`: that one is the cell's *column* and is still
                            # needed below.
                            styled = _style_index(style) if dated and style is not None else -1
                            if styled == _BAD_STYLE:
                                report.malformed_attributes += 1
                            if styled in dated:
                                report.date_cells += 1
                                value = _serial_text(text, epoch_1904=epoch_1904)
                            else:
                                value = _clean_number(text) if text else text
                    else:
                        value = text
                if index < 0 or index >= MAX_COLUMNS:
                    # The reference names no column, or a column the format does not
                    # have. Both are malformed, and both used to do damage silently --
                    # -1 overwrote the previous cell, a huge index exhausted memory.
                    # The value itself is real, so it is appended rather than dropped
                    # (rule 3) and the reference is counted.
                    report.invalid_cell_references += 1
                    cells.append(value)
                    continue
                # The in-order case -- what every producer writes and what the padding
                # loop below spent two `len()` calls and a store on -- is one append.
                # Out of order or with gaps, pad in one `extend` rather than one
                # `append` per missing column.
                width = len(cells)
                if index == width:
                    cells.append(value)
                elif index > width:
                    cells.extend([""] * (index - width))
                    cells.append(value)
                else:
                    cells[index] = value
            element.clear()
            open_elements = 0
            # Unlink the emptied rows from sheetData. Safe because every child it
            # holds has already been handled -- rows arrive in document order. The
            # batch of 64 amortises the slice assignment over many rows.
            if parent is not None and len(parent) > 64:
                del parent[:]

            while cells and not cells[-1]:
                cells.pop()
            if not cells:
                continue
            report.rows += 1
            report.cells += len(cells)
            emitted += 1
            yield Row(name, number, cells)
            if max_rows is not None and emitted >= max_rows:
                report.truncated.append(("max_rows", max_rows, -1))
                return


def iter_rows(
    path: str | Path,
    *,
    diagnostics: SheetDiagnostics | None = None,
    max_rows: int | None = None,
) -> Iterator[Row]:
    """Stream every non-empty row of every sheet, in workbook order.

    Rows absent from the XML and rows carrying no values are not yielded: a
    spreadsheet's row numbering is sparse by nature and ``Row.number`` says where
    the gaps were. ``max_rows`` applies per sheet and lands in
    ``diagnostics.truncated`` when it bites (D7 -- a limit that is hit is data in
    the return value, not a log line).
    """
    report = diagnostics if diagnostics is not None else SheetDiagnostics()
    with zipfile.ZipFile(path) as archive:
        # Charts and images: not read (pixels are out of scope), but counted, because a
        # workbook whose only answer is in a chart must not look like a clean success.
        media = sum(1 for info in archive.infolist() if _MEDIA_DIR.match(info.filename))
        if media:
            report.notes.append(images_note(media))
        layout = _layout(archive)
        strings = _shared_strings(archive, layout.shared_strings, report)
        report.shared_strings = len(strings)
        dated = _date_styles(archive, layout.styles)
        # How many times a worksheet part is read is otherwise the *document's*
        # decision: 200 `<sheet>` elements all naming `rId1` turned a 5,098-byte file
        # into 40,000 rows and 800 chunks, and those elements are 47 near-identical
        # bytes each, so they deflate to almost nothing and the amplification is
        # bounded only by the archive's compression ratio. Excel gives every sheet its
        # own part; a repeat is a broken workbook or a bomb, and either way the rows
        # are already in the output under the first sheet's name.
        read_parts: set[str] = set()
        repeated = 0
        repeated_names: list[str] = []
        for name, part in layout.sheets:
            report.sheets += 1
            if name in layout.hidden:
                report.sheets_hidden.append((name, layout.hidden[name]))
            if part is None:
                report.sheets_without_part.append(name)
                continue
            if part in read_parts:
                repeated += 1
                if len(repeated_names) < 5:  # bounded by us, not by the sheet count
                    repeated_names.append(name)
                continue
            read_parts.add(part)
            declared, _ = _dimension(archive, part)
            seen = 0
            for row in _iter_part_rows(
                archive,
                part,
                name,
                strings,
                dated,
                layout.epoch_1904,
                report,
                max_rows,
            ):
                seen = row.number
                yield row
            if declared is not None and max_rows is None and seen and declared != seen:
                report.dimension_mismatch.append((name, declared, seen))
        if repeated:
            # `repeated_names` is already bounded at collection, so the total has to be
            # handed over separately -- `len()` of it is the truncated length.
            listed = bounded_list(repeated_names, limit=5, total=repeated)
            report.notes.append(
                f"repeated_sheet_parts={repeated} ({listed}): these sheets name a "
                f"worksheet part another sheet had already been read from. It is read "
                f"once, under the first sheet's name -- their rows are in the output, "
                f"their tab names are not"
            )
    if report.malformed_attributes:
        # One note for the document rather than one per sheet: the count is the
        # actionable part, and a per-sheet note on a workbook whose every row is
        # malformed is itself unbounded output.
        report.notes.append(
            f"malformed_attributes={report.malformed_attributes}: a row's `r` or a "
            f"cell's `s` held something that is not a number, so those rows are "
            f"numbered by position and those cells were not checked for a date "
            f"format. The values themselves are intact"
        )


# --------------------------------------------------------------------------- #
# Header detection: the part that has to be honest
# --------------------------------------------------------------------------- #

_NUMBER = re.compile(r"^-?[\d.,]+(?:[eE][-+]?\d+)?%?$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_BOOL = frozenset({"TRUE", "FALSE"})


def _looks_numeric(value: str) -> bool:
    return bool(value) and bool(_NUMBER.match(value))


def _dtype(values: list[str]) -> str:
    """A column's dtype from its sampled values, majority wins.

    Inferred from the rendered strings rather than from the cell's ``t``
    attribute, so it costs nothing per row in the hot loop -- the sample is
    bounded at :data:`SAMPLE_ROWS` and the inference runs once per column.
    """
    present = [value for value in values if value]
    if not present:
        return "empty"
    dates = sum(1 for value in present if _ISO_DATE.match(value))
    numbers = sum(1 for value in present if _looks_numeric(value))
    booleans = sum(1 for value in present if value in _BOOL)
    half = len(present) / 2
    if dates > half:
        return "date"
    if booleans > half:
        return "bool"
    if numbers > half:
        return "number"
    return "text"


def _dtypes_for(rows: list[Row], width: int) -> list[str]:
    """One dtype per column for the first ``width`` columns, short rows read as blank.

    ``width`` is the caller's decision and is deliberately not derived from the
    rows: when a header row overhangs its table the profile is ``len(header)``
    columns wide, and those trailing columns have to be typed (as ``"empty"``, or
    for real if some row does reach that far) rather than left off the end. They
    used to be left off, and the summary chunk then dropped the column *names* that
    had no dtype to pair with.
    """
    return [
        _dtype([row.cells[column] if column < len(row.cells) else "" for row in rows])
        for column in range(width)
    ]


def _row_is_texty(cells: list[str]) -> bool:
    """A candidate header row: has values, and they are overwhelmingly not numbers."""
    present = [value for value in cells if value]
    if not present:
        return False
    return sum(1 for value in present if not _looks_numeric(value)) >= len(present) * 0.8


def _flatten(header_rows: list[list[str]], width: int) -> list[str]:
    """Several header rows -> one label per column.

    This is where a merged multi-row header is recovered, and the whole difficulty
    is that a blank cell in a header row means two opposite things. A merged range
    stores its value only in its top-left cell, so:

    * ``Water`` merged *across* columns 2-4 leaves blanks at 3 and 4, which must be
      **filled** from the left;
    * ``Owner`` merged *down* rows 2-3 leaves a blank at row 3, which must be left
      **empty** -- filling it from the left would label that column
      ``Owner / Variance``, inventing a relationship that is not in the sheet.

    Both are blanks in the same shape of data, so position cannot distinguish
    them. What does: a horizontal span only exists if something *below* it names
    the sub-columns. So a blank is filled from the left only when some later
    header row has a value in that column. The last header row is never filled,
    because nothing sits below it to justify a span.

    (The authoritative answer is ``<mergeCells>``, and it is unavailable here on
    purpose: it is serialised *after* ``<sheetData>``, so a single-pass streaming
    reader cannot see it until every row has gone by. Using it would cost a second
    pass over the part. This heuristic is checked against the real merge ranges by
    the merge audit in the research repository.)
    """
    padded = [list(cells) + [""] * (width - len(cells)) for cells in header_rows]
    filled: list[list[str]] = []
    for depth, row in enumerate(padded):
        below = padded[depth + 1 :]
        carried = ""
        result = list(row)
        for index in range(width):
            if result[index]:
                carried = result[index]
            elif carried and any(later[index] for later in below):
                result[index] = carried
            else:
                carried = ""
        filled.append(result)

    labels: list[str] = []
    for index in range(width):
        parts: list[str] = []
        for row in filled:
            value = row[index].strip()
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        labels.append(" / ".join(parts))
    return labels


def _modal_width(sample: list[Row]) -> int:
    """The width of the sheet's *body*, which anchors everything else.

    The mode rather than the max: a stray wide row (a note typed off to the right,
    a merged title) must not redefine how wide the table is. Ties go to the wider
    candidate, because a table is more often padded than truncated.
    """
    tally: dict[int, int] = {}
    for row in sample:
        tally[len(row.cells)] = tally.get(len(row.cells), 0) + 1
    return max(tally.items(), key=lambda item: (item[1], item[0]))[0]


def _profile(
    sheet: str, sample: list[Row], declared: tuple[int | None, int | None]
) -> SheetProfile:
    """Decide a sheet's header from a bounded look-ahead.

    The algorithm is anchored on **width**, not on position, and that is the whole
    trick. The naive version -- "the leading run of text rows is the header" -- is
    what produces the disasters this fixture was built to catch: on
    ``sheet-messy.xlsx``'s *Report* sheet it swallows a key-value metadata block
    (``Prepared by | C. Halloran``, ``Period | ...``) and flattens it into the
    column labels, so every row group is then prefixed with
    ``Prepared by / Period / Status / Site | C. Halloran / ... / Excursions``.
    That is worse than having no header at all: it is confident nonsense repeated
    into every chunk of the sheet.

    A header row has to look like the data it heads. So:

    1. take the **modal row width** as the body width; a metadata block sitting
       above a five-column table is two cells wide and is disqualified by that
       alone;
    2. find the first **data-like** row -- body-width and not text-dominated;
    3. walk *backwards* from it: the header is the contiguous run of text rows
       that are still body-width (allowing for the trailing blanks that a
       vertically-merged label leaves behind);
    4. everything above that is **preamble**, not header -- kept as the sheet
       title and as preamble lines in the summary chunk, because a report's
       "Prepared by / Period / Status" block is real content and dropping it is
       the rule-3 failure;
    5. if no data-like row exists at all, say so. A sheet of prose has no header,
       and inventing one is what competitors do.
    """
    notes: list[str] = []
    if not sample:
        return SheetProfile(
            sheet,
            None,
            [],
            0,
            0,
            "empty",
            declared_rows=declared[0],
            declared_columns=declared[1],
            notes=["sheet has no rows"],
        )

    body_width = _modal_width(sample)
    # A header row may be narrower than the body when its last labels are
    # vertically merged (the value lives in the row above, so the cell is blank
    # and gets trimmed). 60% keeps those while still excluding a 2-of-5 metadata
    # row.
    floor = max(1, int(body_width * 0.6))

    def wide_enough(row: Row) -> bool:
        return len(row.cells) >= floor

    first_data = next(
        (
            index
            for index, row in enumerate(sample)
            if wide_enough(row) and not _row_is_texty(row.cells)
        ),
        None,
    )

    if first_data is None:
        # No numeric transition anywhere in the sample. Either this is prose, or
        # it is an all-text table. Guess a header only when row 1 looks like a
        # label row for a consistently-shaped body, and flag the guess either way.
        candidate = sample[0]
        consistent = sum(1 for row in sample[1:] if wide_enough(row))
        if (
            body_width >= 2
            and wide_enough(candidate)
            and consistent >= 2
            and len(set(value for value in candidate.cells if value))
            == len([value for value in candidate.cells if value])
        ):
            header = [value.strip() for value in candidate.cells] + [""] * (
                body_width - len(candidate.cells)
            )
            data = sample[1:]
            return SheetProfile(
                sheet,
                None,
                header,
                1,
                data[0].number if data else candidate.number,
                "uncertain",
                dtypes=_dtypes_for(data, len(header)),
                samples=[row.cells for row in data[:3]],
                declared_rows=declared[0],
                declared_columns=declared[1],
                notes=[
                    *notes,
                    "no numeric column found; row 1 taken as a header on shape alone",
                ],
            )
        return SheetProfile(
            sheet,
            None,
            [],
            0,
            sample[0].number,
            "uncertain",
            dtypes=_dtypes_for(sample, body_width),
            samples=[row.cells for row in sample[:3]],
            declared_rows=declared[0],
            declared_columns=declared[1],
            notes=[*notes, "no header/data split found; every row treated as data"],
        )

    # Walk back over the body-width text rows directly above the data.
    top = first_data
    while top > 0 and wide_enough(sample[top - 1]) and _row_is_texty(sample[top - 1].cells):
        top -= 1
        if first_data - top >= 4:  # deeper than this and we are guessing
            notes.append("header run capped at 4 rows")
            break

    header_rows = [row.cells for row in sample[top:first_data]]
    preamble = sample[:top]
    title: str | None = None
    preamble_lines: list[str] = []
    for row in preamble:
        values = [value for value in row.cells if value]
        if not values:
            continue
        if title is None and len(values) == 1:
            title = values[0]
        else:
            preamble_lines.append(" | ".join(values))
    if preamble:
        notes.append(
            f"rows {preamble[0].number}-{preamble[-1].number} read as preamble, not header"
        )

    data = sample[first_data:]
    if not header_rows:
        return SheetProfile(
            sheet,
            title,
            [],
            0,
            data[0].number,
            "uncertain",
            dtypes=_dtypes_for(data, body_width),
            samples=[row.cells for row in data[:3]],
            declared_rows=declared[0],
            declared_columns=declared[1],
            notes=[*notes, "data starts with no label row above it"],
            preamble=preamble_lines,
        )

    header = (
        _flatten(header_rows, body_width)
        if len(header_rows) > 1
        else [
            value.strip()
            for value in list(header_rows[0]) + [""] * (body_width - len(header_rows[0]))
        ]
    )
    return SheetProfile(
        sheet=sheet,
        title=title,
        header=header,
        header_rows=len(header_rows),
        first_data_row=data[0].number,
        confidence="clean" if len(header_rows) == 1 else "merged-header",
        dtypes=_dtypes_for(data, len(header)),
        samples=[row.cells for row in data[:3]],
        declared_rows=declared[0],
        declared_columns=declared[1],
        notes=notes,
        preamble=preamble_lines,
        preamble_rows=len(preamble),
    )


# --------------------------------------------------------------------------- #
# The chunker (D6)
# --------------------------------------------------------------------------- #


#: What one sample row may contribute to a sheet summary. The summary is a preamble
#: -- "what is in this file" -- so it is the one chunk that must never be
#: proportional to the document, and uncapped it was exactly that: a 200 MB
#: single-line CSV rendered its one row into the summary *and* into its row group,
#: 400,000,152 characters and 1.19 GB peak RSS out of a 200,000,000-byte file, with
#: `lost_data` False. `Limits` could not stop it either, because the oversized chunk
#: is emitted before the budget is checked -- the memory is already spent by the time
#: `max_chars` fires, and `max_rows=1` never applied to it at all.
#:
#: Sized from the corpus so that no real sheet moves. Over the 48 xlsx files in
#: `data/fixtures` and `data/` (253 sheets, measured 2026-08-01) the longest sample
#: cell is 579 characters and the longest rendered sample row is 5,253; these leave
#: 72% and 52% of headroom above those, and the sweep confirms not one of the 253
#: summaries changes. The cap is *not* a loss and is not reported as one: a sample
#: row is a copy of a row that is emitted in full in its own row group.
_SAMPLE_CELL_CHARS = 1_000
_SAMPLE_LINE_CHARS = 8_000


def _sample_line(cells: list[str]) -> tuple[str, bool]:
    """One sample row, rendered for the summary and bounded in size.

    Built incrementally rather than joined and then cut: ``" | ".join(cells)`` on a
    row of a million one-character cells materialises four megabytes before anything
    could trim it, and rule 2 applies to a summary line as much as to a document.

    Returns the line and whether the budget bit.
    """
    parts: list[str] = []
    size = 0
    shortened = False
    for cell in cells:
        if size >= _SAMPLE_LINE_CHARS:
            shortened = True
            break
        if len(cell) > _SAMPLE_CELL_CHARS:
            cell = cell[:_SAMPLE_CELL_CHARS]
            shortened = True
        parts.append(cell)
        size += len(cell) + 3  # the " | " this cell will be joined with
    line = " | ".join(parts)
    return (line + " ..." if shortened else line), shortened


def _summary_text(profile: SheetProfile, row_count: int | None) -> tuple[str, int]:
    """The sheet summary chunk's body, and how many of its sample rows were cut.

    Written as labelled lines rather than markdown: an embedder cannot see a pipe
    table, and the column *names* plus their dtypes are the retrievable content.
    """
    lines = [f"Sheet: {profile.sheet}"]
    if profile.title:
        lines.append(f"Title: {profile.title}")
    if row_count is not None:
        lines.append(f"Rows: {row_count:,}")
    for line in profile.preamble:
        lines.append(f"  {line}")
    if profile.header:
        lines.append(f"Columns ({len(profile.header)}):")
        # `strict=True`, deliberately. This used to be `strict=False`, which did not
        # tolerate an invariant violation so much as hide one: a header wider than
        # its body truncated the column list to the dtypes' length while the count
        # line above still claimed the full width. `_profile` now sizes the dtypes
        # from the header, so a mismatch here means a caller built the profile by
        # hand and deserves to hear about it rather than to lose column names.
        for label, dtype in zip(profile.header, profile.dtypes, strict=True):
            lines.append(f"  - {label or '(unnamed)'} [{dtype}]")
    else:
        lines.append("Columns: no header row detected")
    shortened = 0
    if profile.samples:
        lines.append("Sample rows:")
        for cells in profile.samples:
            line, cut = _sample_line(cells)
            shortened += cut
            lines.append("  " + line)
    lines.append(f"Layout confidence: {profile.confidence}")
    for note in profile.notes:
        lines.append(f"Note: {note}")
    return "\n".join(lines), shortened


def iter_sheet_chunks(
    path: str | Path,
    *,
    target: int = 1800,
    diagnostics: SheetDiagnostics | None = None,
    max_rows: int | None = None,
    rows: Iterable[Row] | None = None,
) -> Iterator[SheetChunk]:
    """Stream a workbook as one summary chunk plus row-group chunks per sheet.

    Group size follows ``target`` characters (D9's default). The header line is
    re-prefixed into **every** group, because a group of rows without its header
    is unreadable to an embedder and to an LLM -- that repetition is the whole
    point, and it is the one place diceo deliberately duplicates text.

    ``rows`` accepts a foreign row iterator so a faster backend can be
    substituted without touching the chunking; it must yield sheets contiguously,
    which every streaming reader does because the parts are read in order.
    """
    report = diagnostics if diagnostics is not None else SheetDiagnostics()
    declared: dict[str, tuple[int | None, int | None]] = {}
    if rows is None:
        with zipfile.ZipFile(path) as archive:
            layout = _layout(archive)
            for name, part in layout.sheets:
                declared[name] = _dimension(archive, part) if part else (None, None)
        rows = iter_rows(path, diagnostics=report, max_rows=max_rows)

    # Per-sheet state. `pending` holds the look-ahead sample until the profile is
    # decided; after that it is always empty and rows go straight into the group.
    state = _ChunkState(target)

    for row in rows:
        if row.sheet != state.sheet:
            # A sheet shorter than the look-ahead reaches its boundary with the
            # profile still undecided, so closing a sheet has to be able to decide
            # it. One code path, used at every boundary and at the end.
            yield from state.close(declared)
            state.open(row.sheet)
        yield from state.feed(row, declared)

    yield from state.close(declared)
    _report_group_cost(report, state, target)


#: Report the header-repetition cost only once it is worth a caller's attention. A
#: three-row sheet whose header is a third of its text is not a problem; 56.6% of a
#: 2.4 MB workbook is.
_HEADER_COST_MIN_CHARS = 2_000
_HEADER_COST_MIN_SHARE = 10.0


def _report_group_cost(report: SheetDiagnostics, state: _ChunkState, target: int) -> None:
    """Rule 3 for a cost rather than a loss.

    Neither of these is fixable and both are worth knowing. The header prefix is
    what makes a wide table retrievable at all -- experiment 016 (recovering
    boundaries blind) measured a group without it at ``wide_table_cell`` 0.065
    against 0.538 -- and a ``column: value`` rendering
    was simulated on real sheets and comes out *more* expensive on everything that
    is not both wide and sparse (2.4x worse on a 10-column budget table). So the
    honest move is to price it rather than to pretend it is free.
    """
    if state.widest > target:
        # Only blame the header prefix when there was one. The ONS CPI workbook
        # emitted exactly this note while 25 of its 41 sheets had `header_line == ""`,
        # so the one diagnostic it produced explained the overrun with a mechanism
        # that had not run -- a note that is worse than silence, because a caller who
        # reads it stops looking.
        cause = (
            "the header line is re-prefixed into every row group and is not counted "
            "against the target, and on a sheet this wide no group size both fits "
            "and carries a row"
            if state.header_chars
            else "a single row is wider than the target, so no group size both fits "
            "and carries a row"
        )
        report.notes.append(
            f"chunks_over_target={state.widest} chars against target={target}: "
            f"{cause}. Check this against your embedder's window."
        )
    if state.sheets_without_header:
        listed = bounded_list(state.sheets_without_header, limit=5)
        report.notes.append(
            f"sheets_without_header={len(state.sheets_without_header)} "
            f"({listed}): no header row was recognised, so {state.groups_without_header} "
            f"row group(s) carry bare values with nothing naming the columns. Those "
            f"chunks are hard to retrieve -- experiment 016 measured a group "
            f"without its header at wide_table_cell 0.065 against 0.538"
        )
    if state.samples_shortened:
        # A note rather than a `truncated` entry, deliberately. `lost_data` means the
        # caller's index is missing text; here it is not. Every row the summary
        # shortened is emitted in full in its own row-group chunk, so what the cap
        # removes is a *duplicate*. Calling that a loss would raise the one boolean
        # the API tells callers to branch on for a file where nothing was lost --
        # which is the same failure as staying silent, pointed the other way.
        report.notes.append(
            f"summary_samples_shortened={state.samples_shortened}: sample rows in a "
            f"sheet summary were cut to {_SAMPLE_CELL_CHARS:,} chars per cell and "
            f"{_SAMPLE_LINE_CHARS:,} per row. The summary is a preamble, not a second "
            f"copy of the sheet; each of those rows is still emitted in full in its "
            f"own row group"
        )
    if state.groups_without_letters:
        report.notes.append(
            f"chunks_without_letters={state.groups_without_letters}: row groups holding "
            f"no alphabetic character at all, so no text query can reach them. The "
            f"values are real and are still emitted; what is missing is anything "
            f"naming them"
        )
    total = state.header_chars + state.body_chars
    share = 100 * state.header_chars / total if total else 0.0
    if state.header_chars >= _HEADER_COST_MIN_CHARS and share >= _HEADER_COST_MIN_SHARE:
        report.notes.append(
            f"header_repeated={state.header_chars:,} chars, {share:.1f}% of the "
            f"sheet text emitted (deliberate: a row group without its column names "
            f"is not retrievable)"
        )


class _ChunkState:
    """The chunker's per-sheet state machine, factored out so that "finish this
    sheet" is one method called from both the boundary and the end of the stream.

    Inline, that logic has to be written twice and the second copy is where the
    bug lives: a sheet with fewer rows than the look-ahead never gets its summary,
    and its rows vanish from the output entirely.
    """

    def __init__(self, target: int) -> None:
        self.target = target
        self.sheet: str | None = None
        self.sample: list[Row] = []
        self.profile: SheetProfile | None = None
        self.header_line = ""
        self.group: list[str] = []
        self.group_rows = 0
        self.first = 0
        self.last = 0
        self.size = 0
        #: What the header re-prefixing actually costs, accumulated across sheets.
        #: A group is sized by its *rows* and the header is then added for free,
        #: which is the only workable arithmetic -- on the real World Bank
        #: commodity sheet the header is 1,781 characters against a 600-character
        #: target, so counting it would leave room for no rows at all -- but it
        #: means a caller who asked for 600 gets 2,344 and is entitled to know.
        self.header_chars = 0
        self.body_chars = 0
        self.widest = 0
        #: Sheets the detector could not find a header for. ``_profile`` already
        #: records this as ``confidence="uncertain"`` and writes it into the summary
        #: chunk's prose, where no caller can act on it. Without a header line every
        #: row group in the sheet is bare values -- a grid of numbers with nothing
        #: naming the columns -- and experiment 016 measured that at ``wide_table_cell`` 0.065
        #: against 0.538. It is the single largest retrieval failure this reader has
        #: and it was, until now, invisible: nothing in the ``Diagnostics`` a caller
        #: passes to ``chunk()`` and reads after the loop ever mentioned it.
        self.sheets_without_header: list[str] = []
        self.groups_without_header = 0
        #: Row groups with no alphabetic character anywhere in them. A chunk of pure
        #: digits and separators is not reachable by any text query -- it can only be
        #: found by a number the user would have to already know -- so it occupies an
        #: index slot and answers nothing. Counted rather than dropped: the values are
        #: real and rule 3 forbids discarding them silently. This is the observable
        #: form of "the sheet came out as an unlabelled grid".
        self.groups_without_letters = 0
        #: Sample rows the summary chunk had to shorten (`_SAMPLE_CELL_CHARS`,
        #: `_SAMPLE_LINE_CHARS`). A cap that bites has to be visible, even when --
        #: as here -- it costs the index nothing.
        self.samples_shortened = 0

    def open(self, sheet: str) -> None:
        self.sheet = sheet
        self.sample = []
        self.profile = None
        self.header_line = ""

    def _decide(
        self, declared: dict[str, tuple[int | None, int | None]]
    ) -> Iterator[SheetChunk]:
        """Profile the sheet from whatever sample we have, emit the summary, then
        release the held rows into groups."""
        assert self.sheet is not None
        self.profile = _profile(self.sheet, self.sample, declared.get(self.sheet, (None, None)))
        self.header_line = " | ".join(self.profile.header) if self.profile.header else ""
        if not self.header_line:
            self.sheets_without_header.append(self.sheet)
        summary, shortened = _summary_text(self.profile, self.profile.declared_rows)
        self.samples_shortened += shortened
        yield SheetChunk("sheet_summary", self.sheet, summary)
        # The held rows still have to be emitted -- minus the ones the profile
        # claimed as preamble or header, which live in the summary and in every
        # group's prefix instead of in the body.
        consumed = self.profile.preamble_rows + self.profile.header_rows
        held = self.sample[consumed:]
        self.sample = []
        for row in held:
            yield from self._add(row)

    def _add(self, row: Row) -> Iterator[SheetChunk]:
        line = " | ".join(row.cells)
        if self.group and self.size + len(line) + 1 > self.target:
            yield from self._flush()
        if not self.group:
            self.first = row.number
        self.last = row.number
        self.group.append(line)
        self.group_rows += 1
        self.size += len(line) + 1

    def _flush(self) -> Iterator[SheetChunk]:
        if not self.group:
            return
        body = "\n".join(self.group)
        self.body_chars += len(body)
        if self.header_line:
            body = self.header_line + "\n" + body
            self.header_chars += len(self.header_line) + 1
        else:
            self.groups_without_header += 1
        # Cheap in practice: prose short-circuits on the first character, and the
        # scan only runs to completion on the all-numeric groups this is here to
        # find, which are exactly the ones worth the walk. `map` rather than a
        # generator expression because that walk is per character and a genexpr pays
        # a Python frame resumption for each one: measured on a 1.5 KB all-numeric
        # group, 101 us to 47 us. Not the regex `[^\W\d_]`, which is faster still and
        # not the same test -- it matches Nl/No characters (Roman numerals, circled
        # digits) that `str.isalpha` calls non-alphabetic.
        if not any(map(str.isalpha, body)):
            self.groups_without_letters += 1
        if len(body) > self.widest:
            self.widest = len(body)
        yield SheetChunk(
            "row_group", self.sheet or "", body, self.first, self.last, self.group_rows
        )
        self.group = []
        self.group_rows = 0
        self.size = 0

    def feed(
        self, row: Row, declared: dict[str, tuple[int | None, int | None]]
    ) -> Iterator[SheetChunk]:
        if self.profile is None:
            self.sample.append(row)
            if len(self.sample) < SAMPLE_ROWS:
                return
            yield from self._decide(declared)
            return
        yield from self._add(row)

    def close(self, declared: dict[str, tuple[int | None, int | None]]) -> Iterator[SheetChunk]:
        if self.sheet is None:
            return
        if self.profile is None:
            yield from self._decide(declared)
        yield from self._flush()
        self.sheet = None
