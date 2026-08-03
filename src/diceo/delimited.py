"""CSV and its relatives, routed through the spreadsheet chunker.

A CSV is a spreadsheet that lost its container, and treating it as prose throws
away the one thing that makes tabular retrieval work: **the header line repeated
into every row group**. That repetition is worth 0.528 against MarkItDown's 0.449
on held-out workbooks (experiment 022, formats and overlap),
and it costs nothing to apply here -- ``iter_sheet_chunks`` already accepts a
foreign row iterator, so this module is a reader and not a second chunker.

Two details that decide whether this works on real files:

*The delimiter is measured, not assumed.* Half of Europe's Excel exports are
semicolon-separated, because in a locale where the decimal separator is a comma
Excel switches. A reader that assumes ``,`` turns those files into one column of
garbage -- and reports success.

*Quoting is the stdlib's job.* ``csv.reader`` handles embedded delimiters,
doubled quotes and newlines inside fields. Splitting on the delimiter by hand is
faster and wrong, and it is wrong in the way that silently truncates a row.

*But the stdlib will not tell you when quoting went wrong.* One ``"`` at a field
start and ``csv.reader`` consumes every remaining record into that field: measured
in experiment 038 (edge case mining), a 10,000-row file came
back as 5 rows and 3 chunks with ``lost_data`` False. Nothing raises, because a
quote that never closes is not a syntax error -- the file just ends. So the shape
of the output is checked against the shape the sample promised, and a row that
swallowed the rest of the document is reported (rule 3). See ``_swallowed_records``
for the line drawn between that and a multi-line postal address, which is ordinary
CSV and must survive untouched.
"""

from __future__ import annotations

import csv
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from itertools import chain
from typing import IO

from diceo.plaintext import text_lines
from diceo.sheets import Row, SheetDiagnostics

#: Candidates, in the order a tie is broken. Comma first because it is the name of
#: the format; tab before semicolon because a .tsv is unambiguous when it appears.
_CANDIDATES = (",", "\t", ";", "|")

#: How many lines to look at when measuring the delimiter. Enough to see a
#: consistent column count, few enough that a 4 GB file does not pay for it.
_SAMPLE_LINES = 50

#: Physical lines one logical row may span before it is examined. A postal address
#: is three, a pasted legal notice is tens, a swallowed file is thousands. This is
#: a gate, not a verdict -- what makes the row pathological is decided by
#: ``_swallowed_records``.
_ROW_LINE_CEILING = 64

#: A field this big is no longer a cell. Excel's own cell limit is 32,767
#: characters; a megabyte leaves room for a pasted document and still catches a
#: runaway whose records happen to be long enough to stay under the line ceiling.
_FIELD_CHAR_CEILING = 1 << 20

#: How much of a suspicious field to look at. Bounded, because the check may not
#: cost what the defect costs (rule 2): 64 KiB is hundreds of records, which is
#: more than enough to see whether the content is records at all.
_INSPECT_CHARS = 1 << 16

#: Distinct pathologies recorded per file. Diagnostics live for the whole document,
#: so a file that is nothing but stray quotes must not become an unbounded list.
_MAX_REPORTED = 3

#: csv's field-size limit defaults to 128 KB and raises on anything larger. A cell
#: holding a pasted document is unusual but not corrupt, so we raise the ceiling --
#: but `csv.field_size_limit` is **process-global**, and this module used to call it
#: at import time and never put it back.
#:
#: That is a library reaching into its host. From the moment a caller ran
#: `diceo.chunk()` on any .csv, their *own* unrelated `csv.reader` stopped raising
#: `_csv.Error: field larger than field limit` and started materialising multi-gigabyte
#: fields instead -- the exact guard they were relying on, removed by a dependency, with
#: no way to know.
#:
#: Wrapping the *generator* in a save/restore did not fix that, and measuring is the only
#: way to find out: a generator holds its scope open across every `yield`, so a caller
#: reading the chunks of one CSV saw the limits {131072, 2147483647} between them. Their
#: guard was still gone, only for a shorter while. Worse under threads -- which this
#: format supports, see `PDFIUM_LOCK` in `pdf/extract.py` for the one that does not --
#: because each enter saved the value it found: with three readers running, the second
#: saved 2147483647 as "the caller's" and handed that back at the end, leaving the
#: process permanently unguarded after every reader had finished.
#:
#: So nothing here raises the ceiling for the length of a read. The ceiling goes up only
#: for the parse of a row that is actually about to need it (`metered_lines` below), and
#: comes down before the row is yielded, which on a file whose cells are cells means the
#: process-global is never written at all.
#:
#: What is deliberately *not* claimed: while one reader is inside such a row, a different
#: thread's caller can still read the raised value. A process-global cannot be made
#: thread-local, and the only way to close that window would be to serialise all CSV
#: parsing behind a lock -- which would cost every caller far more than the window is
#: worth, on documents that will never contain a cell that large.
_FIELD_LIMIT = min(sys.maxsize, 2**31 - 1)

#: Guards the pair of module globals below, which stand between concurrent readers and
#: the one process-global they all have to share.
_LIMIT_LOCK = threading.Lock()

#: How many reads currently need the ceiling up, and what it was before the first of
#: them raised it. Counting is what makes concurrent readers safe: the caller's value is
#: read once, when the count leaves zero, and written back once, when it returns.
_RAISED_READERS = 0
_CALLER_LIMIT = 0


def _enter_raised() -> None:
    """Take a share in the raised ceiling."""
    global _RAISED_READERS, _CALLER_LIMIT
    with _LIMIT_LOCK:
        if _RAISED_READERS == 0:
            _CALLER_LIMIT = csv.field_size_limit()
            # A caller who has set an even higher ceiling keeps it. Lowering theirs for
            # the length of our read is the same trespass in the other direction.
            if _CALLER_LIMIT < _FIELD_LIMIT:
                csv.field_size_limit(_FIELD_LIMIT)
        _RAISED_READERS += 1


def _leave_raised() -> None:
    """Give it back, restoring the caller's value when the last reader lets go."""
    global _RAISED_READERS
    with _LIMIT_LOCK:
        _RAISED_READERS -= 1
        if _RAISED_READERS == 0:
            csv.field_size_limit(_CALLER_LIMIT)


def _caller_field_limit() -> int:
    """The host process's own ceiling, whether or not a reader is holding it up."""
    with _LIMIT_LOCK:
        return _CALLER_LIMIT if _RAISED_READERS else csv.field_size_limit()


@contextmanager
def _raised_field_limit() -> Iterator[None]:
    """Raise csv's ceiling across a stretch of parsing that yields nothing outward.

    Only legitimate where no ``yield`` can reach the caller from inside it, which is why
    this wraps sniffing and not reading: sniffing happens inside the first ``next()`` on
    our generator, so control never leaves the package while it is raised.
    """
    _enter_raised()
    try:
        yield
    finally:
        _leave_raised()


#: How much of the sample must agree on the field count before a file is called a
#: table rather than prose with punctuation in it. Only `looks_delimited` applies it:
#: `sniff_delimiter`'s caller already knows the file is delimited.
_MIN_AGREEMENT = 0.8


def _best_delimiter(sample: list[str]) -> tuple[str | None, float, int]:
    """``(delimiter, agreement, modal fields)``, or ``(None, ...)`` if nothing scored.

    Scores each candidate by how *consistent* its field count is across lines, not by
    how often it appears. Prose containing many commas scores badly because its count
    per line varies wildly, which is exactly the discrimination wanted.
    """
    rows = [line for line in sample if line.strip()][:_SAMPLE_LINES]
    if not rows:
        return None, 0.0, 0

    best: str | None = None
    best_score = (-1.0, 0)
    for candidate in _CANDIDATES:
        counts = [len(next(csv.reader([line], delimiter=candidate))) for line in rows]
        fields = max(counts)
        if fields < 2:
            continue
        modal = max(set(counts), key=counts.count)
        if modal < 2:
            continue
        agreement = counts.count(modal) / len(counts)
        score = (agreement, modal)
        if score > best_score:
            best, best_score = candidate, score
    return best, best_score[0], best_score[1]


def sniff_delimiter(sample: list[str]) -> str:
    """Which character separates the fields. Assumes the file *is* delimited.

    Always answers, falling back to ``","`` when no candidate scores, because its
    caller has already decided the file is a CSV and only needs to know how to cut it.
    Use :func:`looks_delimited` to ask the other question.
    """
    best, _, _ = _best_delimiter(sample)
    return best or ","


def looks_delimited(sample: list[str]) -> str | None:
    """The delimiter if these lines really are a table, ``None`` if they merely
    contain commas.

    Split out from ``sniff_delimiter`` because format *detection* needs the question
    that one cannot answer: a file renamed ``.xlsx`` has to be classified before
    anybody knows it is a CSV, and a sniffer that always names a delimiter would call
    every paragraph of prose a spreadsheet. Keeping the scoring in one place is the
    point -- it lived here and in a near-copy in ``source.py``, one modal-agreement
    test away from drifting apart.
    """
    best, agreement, modal = _best_delimiter(sample)
    if best is None or modal < 2 or agreement < _MIN_AGREEMENT:
        return None
    return best


def _modal_fields(sample: list[str], delimiter: str) -> int:
    """How wide a row is, according to the file itself, for the winning delimiter.

    ``sniff_delimiter`` scores each candidate line by line, which is right for
    picking between candidates and wrong for this: a legitimate multi-line cell in
    the first 50 lines makes every line of it look like a short row, and the width
    comes out too small. So the sample is parsed as *records* here, and a record
    left unterminated by the 50-line cut -- the quote count is then odd -- is
    dropped rather than allowed to set the width.
    """
    counts = [
        len(row)
        for row in csv.reader(sample, delimiter=delimiter)
        if any(cell.strip() for cell in row)
    ]
    if counts and sum(line.count('"') for line in sample) % 2:
        counts.pop()
    return max(set(counts), key=counts.count) if counts else 0


def _swallowed_records(field: str, delimiter: str, modal: int) -> bool:
    """Does this field hold the rest of the file rather than a value?

    This is the whole difficulty of the fix. Size cannot decide it: a multi-line
    address, a pasted contract and a swallowed document are all "a big field with
    newlines in it", and reporting the first two as data loss would be a worse bug
    than the one being fixed -- a caller who is warned about every address stops
    reading warnings.

    The content decides it. A swallowed region is *made of records*, so nearly
    every line in it carries a full row's worth of delimiters. An address carries
    none; prose carries them where the sentences fall, which is nowhere near
    ``modal - 1`` on four lines out of five. Only complete lines are counted --
    the first and last are cut by the field boundary and the inspection window.

    Where this is deliberately silent: a two-column file whose multi-line cells
    carry a comma per line is genuinely ambiguous -- the same bytes are a runaway
    quote and a pasted address -- and the caller of the two gates above never sees
    it, because a cell that small does not reach them.
    """
    if modal < 2:
        # A single-column file offers no delimiter to count, so there is no
        # structure to have lost. The size ceiling is the only signal left.
        return len(field) > _FIELD_CHAR_CEILING
    inner = field[:_INSPECT_CHARS].split("\n")[1:-1]
    if not inner:
        return False
    full = sum(1 for line in inner if line.count(delimiter) >= modal - 1)
    return full >= 0.8 * len(inner)


def iter_rows(
    handle: IO[bytes],
    *,
    sheet: str = "",
    diagnostics: SheetDiagnostics | None = None,
    max_rows: int | None = None,
) -> Iterator[Row]:
    """Stream a delimited file as sheet rows, so the sheet chunker can group them.

    Ragged rows are kept, not padded and not dropped: a short row is what the file
    says, and a reader that silently pads one hides a mis-quoted field.

    A row that swallowed the rest of the document -- the unbalanced-quote case --
    is kept too, and reported as ``truncated`` so ``lost_data`` is True: the
    characters are all still there, but the row structure they were in is gone,
    and a caller cannot see that from the chunks.
    """
    yield from _iter_rows(handle, sheet=sheet, diagnostics=diagnostics, max_rows=max_rows)


def _iter_rows(
    handle: IO[bytes],
    *,
    sheet: str = "",
    diagnostics: SheetDiagnostics | None = None,
    max_rows: int | None = None,
) -> Iterator[Row]:
    report = diagnostics if diagnostics is not None else SheetDiagnostics()
    report.sheets += 1

    lines = text_lines(handle, report)
    sample: list[str] = []
    for line in lines:
        sample.append(line)
        if len(sample) >= _SAMPLE_LINES:
            break
    budget = _caller_field_limit()

    # `_best_delimiter` and `_modal_fields` run `csv.reader` over the sample themselves,
    # so a file whose first 50 lines hold one enormous cell would trip the ceiling here
    # rather than in the loop. The sample is already materialised, so whether that is
    # even possible is one sum over 50 strings -- and for any ordinary file it is not,
    # which is what keeps the global untouched for the whole read.
    room = _raised_field_limit() if sum(map(len, sample)) > budget else nullcontext()
    with room:
        delimiter = sniff_delimiter(sample)
        modal = _modal_fields(sample, delimiter)
    if delimiter != ",":
        report.notes.append(f"delimiter={delimiter!r}")

    # Characters we may still hand the parser for the row in progress before one of its
    # fields could reach `budget`. A field is built out of the text of its own row, so it
    # can never be longer than what has been fed since the last row ended -- which makes
    # this the last moment at which raising the ceiling is still early enough.
    headroom = budget
    holding = False

    def metered_lines() -> Iterator[str]:
        # Counted through a loop rather than passed on with `yield from` because the
        # parser has to be told before it sees the characters, not after. That costs a
        # frame resume per line: 3% of the row reader on a 16 MB, 200k-row fixture
        # (0.90 -> 0.87 Mrow/s, 2.4-4.7% across interleaved repeats), and 1.2-1.8% end
        # to end through `diceo.chunk`. The arithmetic itself measured free --
        # stripping it and keeping the loop gave the same time. Cheap only by
        # comparison: raising and restoring around each `next(reader)` instead costs
        # 24% with the save/restore hand-inlined and 58% as a context manager.
        nonlocal headroom, holding
        for line in chain(sample, lines):
            headroom -= len(line)
            if headroom < 0 and not holding:
                _enter_raised()
                holding = True
            yield line

    number = 0
    reported = 0
    reader = csv.reader(metered_lines(), delimiter=delimiter)
    consumed = 0
    try:
        for fields in reader:
            if holding:
                # Before the `yield` below, never after: that ordering is the whole
                # point, and it is what the generator-wide scope could not give.
                _leave_raised()
                holding = False
            headroom = budget
            number += 1
            # `line_num` is the reader's own count of *physical* lines pulled from the
            # iterator, so the delta is what this one logical row cost -- one for a
            # normal row, a handful for a multi-line cell, the whole file for a runaway
            # quote. Free, and O(1): no row is held to compute it.
            spanned, consumed = reader.line_num - consumed, reader.line_num
            if max_rows is not None and number > max_rows:
                report.truncated.append(("max_rows", max_rows, -1))
                return
            # A runaway quote always eats newlines, so a single-line row cannot be one
            # and pays nothing but the comparison above. Only the widest field is
            # examined: nothing narrower swallowed the file.
            if spanned > 1 and reported < _MAX_REPORTED:
                widest = max(fields, key=len, default="")
                if (
                    spanned > _ROW_LINE_CEILING or len(widest) > _FIELD_CHAR_CEILING
                ) and _swallowed_records(widest, delimiter, modal):
                    reported += 1
                    report.truncated.append(("unbalanced_quote_at_row", number, spanned))
                    report.notes.append(
                        f"unbalanced_quote_at_row={number}: an unclosed quote pulled "
                        f"{spanned} lines into one field ({len(widest)} chars); that text "
                        "is in the index as one cell rather than as rows"
                    )
            cells = [field.strip() for field in fields]
            if not any(cells):
                continue
            report.rows += 1
            report.cells += len(cells)
            yield Row(sheet=sheet, number=number, cells=cells)
    finally:
        # A caller who stops reading half way through a runaway row abandons the
        # generator here, and that is the one path where a leak would be permanent.
        if holding:
            _leave_raised()
