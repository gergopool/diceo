"""One stray `"` must not delete a CSV and be reported as a clean success.

Found by the experiment 038 (edge case mining) audit and measured
here before the fix. A 10,000-row, three-column file in which one field opens a
quote and never closes it: `csv.reader` enters quoted mode and consumes every
remaining record into that one field, so

    good file     10,001 rows, 140 chunks, lost_data False
    one stray "        5 rows,   3 chunks, lost_data False

-- 9,996 rows gone, one 208,830-character cell in their place, and a caller with
no way to know. That is rule 3's worst case: the document is in the index, so
nothing looks broken, and almost none of it is findable.

The whole difficulty is telling that apart from a **legitimate** quoted field with
embedded newlines. A multi-line postal address is ordinary CSV and must keep
working untouched, so half the tests below are about the file that is *fine*:
addresses, a pasted multi-paragraph note, and a bare title line among three-column
rows. The discriminator the fix uses is what is *inside* the oversized field --
a swallowed region is made of records, and nearly every line in it carries a full
record's worth of delimiters, which is true of no real cell.

Rule 2 is the other constraint: the reader is a generator over a 1M-row file, so
the check may not buffer rows or take a second pass. `test_the_check_does_not_read
_ahead` pins that.
"""

from __future__ import annotations

import io

import pytest

import diceo
from diceo import Diagnostics
from diceo.delimited import iter_rows
from diceo.sheets import SheetDiagnostics

#: Big enough that the collapse is unmistakable, small enough to stay fast.
_ROWS = 10_000


def _sales_csv(rows: int = _ROWS, *, stray_quote_at: int | None = None) -> bytes:
    """`rows` three-column records, one of which may open a quote it never closes.

    The stray quote sits at the *start* of a field, which is the only place it
    matters: `csv.reader` only enters quoted mode there.
    """
    lines = ["id,region,note"]
    for index in range(rows):
        note = '"unterminated' if index == stray_quote_at else "plain note"
        lines.append(f"{index},EMEA,{note}")
    return ("\n".join(lines) + "\n").encode()


def _read(payload: bytes) -> tuple[list[list[str]], SheetDiagnostics]:
    report = SheetDiagnostics()
    rows = [row.cells for row in iter_rows(io.BytesIO(payload), sheet="s", diagnostics=report)]
    return rows, report


def _chunk(payload: bytes, name: str = "sales.csv") -> tuple[list, Diagnostics]:
    report = Diagnostics()
    pieces = list(diceo.chunk(payload, name=name, diagnostics=report))
    return pieces, report


# --------------------------------------------------------------------------- #
# The file that is fine. First, because a check that fires on it is worse than
# the defect it replaces.
# --------------------------------------------------------------------------- #


def test_the_undamaged_file_still_reads_every_row():
    rows, report = _read(_sales_csv())

    assert len(rows) == _ROWS + 1
    assert not report.truncated, report.truncated


def test_a_quoted_field_with_embedded_newlines_is_not_a_defect():
    """A multi-line postal address is ordinary CSV, not a runaway quote. A fix
    that cannot tell them apart breaks more files than it saves."""
    payload = (
        b"name,address,city\n"
        b'Acme,"12 Long Road\nSuite 4\nPO Box 9",Utrecht\n'
        b'Beta,"1 Short Street",Delft\n'
    )

    rows, report = _read(payload)

    assert len(rows) == 3, rows
    assert rows[1] == ["Acme", "12 Long Road\nSuite 4\nPO Box 9", "Utrecht"]
    assert not report.truncated, report.truncated


def test_a_file_of_multi_line_addresses_stays_clean():
    """Not one address -- five hundred, so the check cannot be accumulating
    anything across rows and calling the total a pathology."""
    lines = ["name,address,city"]
    for index in range(500):
        lines.append(f'Client {index},"{index} Long Road\nSuite {index}\nPO Box 9",Utrecht')
    payload = ("\n".join(lines) + "\n").encode()

    rows, report = _read(payload)

    assert len(rows) == 501
    assert not report.truncated, report.truncated


def test_a_pasted_multi_paragraph_note_is_not_a_runaway():
    """The hard legitimate case: one cell holding a long pasted document, spanning
    far more lines than any address and carrying commas of its own. It is prose,
    not records, and prose is what tells the two apart."""
    prose = "\n".join(
        f"Paragraph {index}, which runs on for a while and ends here." for index in range(400)
    )
    payload = b"id,note,owner\n" + f'1,"{prose}",alice\n'.encode() + b"2,short note,bob\n"

    rows, report = _read(payload)

    assert len(rows) == 3, [len(row) for row in rows]
    assert rows[1][1].startswith("Paragraph 0,")
    assert not report.truncated, report.truncated


def test_an_inch_mark_is_not_an_unbalanced_quote():
    """`5" pipe` leaves the file with an odd number of quote characters on every
    row, and `csv.reader` is right to read it literally: a quote only opens a
    field at the start of one. Anything counting quotes would condemn the file."""
    lines = ["id,size,note"]
    lines += [f'{index},5" pipe,plain note' for index in range(3_000)]
    payload = ("\n".join(lines) + "\n").encode()

    rows, report = _read(payload)

    assert len(rows) == 3_001
    assert not report.truncated, report.truncated


def test_a_bare_title_line_is_not_a_runaway():
    """A one-field line among three-field rows -- a section title, a trailing
    "generated at" stamp -- is common and lossless. Reporting on field count
    alone would flag every one of them."""
    payload = b"id,region,note\n1,EMEA,plain note\nRegional totals follow\n2,APAC,plain note\n"

    rows, report = _read(payload)

    assert rows[2] == ["Regional totals follow"]
    assert not report.truncated, report.truncated


# --------------------------------------------------------------------------- #
# The damaged file.
# --------------------------------------------------------------------------- #


def test_the_silent_success_it_replaces():
    """The regression itself: before the fix this returned 5 rows out of 10,001
    and said nothing at all. Either the rows or a diagnostic is acceptable;
    silence is not."""
    rows, report = _read(_sales_csv(stray_quote_at=3))

    if len(rows) < _ROWS + 1:
        assert report.truncated, f"{len(rows)} rows out of {_ROWS + 1} and nothing reported"


def test_a_runaway_quote_is_reported_as_lost_data():
    rows, report = _read(_sales_csv(stray_quote_at=3))

    assert len(rows) == 5, "the fixture no longer collapses; the test proves nothing"
    assert [what for what, _, _ in report.truncated] == ["unbalanced_quote_at_row"]


def test_the_diagnostic_names_the_row_that_opened_the_quote():
    """A caller triaging a corpus needs the line to look at, not just a flag."""
    _, report = _read(_sales_csv(stray_quote_at=3))

    what, value, spanned = report.truncated[0]
    assert what == "unbalanced_quote_at_row"
    assert value == 5, report.truncated  # header + rows 0..2, then the damaged one
    assert spanned > _ROWS - 10, "the swallowed line count is the size of the loss"


def test_lost_data_reaches_the_caller_through_the_public_api():
    """`chunk` plus `Diagnostics` is all a caller has. Rule 3 is about *that*
    surface, not about an internal counter."""
    _, good = _chunk(_sales_csv())
    _, damaged = _chunk(_sales_csv(stray_quote_at=3))

    assert not good.lost_data, good.truncated
    assert damaged.lost_data, damaged.truncated
    assert any("unbalanced_quote" in item for item in damaged.truncated), damaged.truncated


def test_a_quote_opening_at_the_start_of_a_line_is_caught_too():
    """The shape 038 named: the stray quote is the first character of a record, so
    the swallowed remainder holds no unquoted delimiter at all and the row arrives
    as a single field. Field count alone would call it a title line."""
    lines = ["id,region,note"]
    for index in range(2_000):
        lines.append(f'"{index},EMEA,note' if index == 1 else f"{index},EMEA,note")
    payload = ("\n".join(lines) + "\n").encode()

    rows, report = _read(payload)

    assert len(rows[-1]) == 1, "the fixture no longer collapses into one field"
    assert [what for what, _, _ in report.truncated] == ["unbalanced_quote_at_row"]


def test_a_semicolon_export_is_measured_the_same_way():
    """The modal field count comes from the sniffed delimiter, so the check must
    work on the half of Europe whose Excel writes `;`."""
    lines = ["id;region;note"]
    for index in range(2_000):
        note = '"unterminated' if index == 3 else "plain note"
        lines.append(f"{index};EMEA;{note}")
    payload = ("\n".join(lines) + "\n").encode()

    rows, report = _read(payload)

    assert len(rows) == 5, rows
    assert [what for what, _, _ in report.truncated] == ["unbalanced_quote_at_row"]


def test_a_file_that_is_both_is_read_correctly():
    """Legitimate multi-line cells *and* a stray quote, in that order -- the case
    that decides how the row width is measured. Parsing the 50-line sample line by
    line would call this file two columns wide, and then every line of every
    address looks like a whole record and the addresses are condemned with the
    genuine defect."""
    lines = ["name,address,city"]
    lines += [f'Client {i},"{i} Long Road\nSuite {i}",Utrecht' for i in range(30)]
    lines += [f"Client {i},{i} Short Road,Delft" for i in range(30, 3_000)]
    lines[131] = 'Client 131,"unterminated,Delft'
    payload = ("\n".join(lines) + "\n").encode()

    rows, report = _read(payload)

    assert rows[1] == ["Client 0", "0 Long Road\nSuite 0", "Utrecht"]
    reported = [(what, row) for what, row, _ in report.truncated]
    assert reported == [("unbalanced_quote_at_row", 132)], reported


def test_the_damaged_rows_are_still_yielded():
    """Rule 3 cuts both ways: the swallowed text is reported *and* kept. Dropping
    the giant field would turn a reported loss into a real one."""
    rows, _ = _read(_sales_csv(stray_quote_at=3))

    assert "9999,EMEA,plain note" in rows[-1][-1]


def test_only_a_bounded_number_of_pathologies_is_recorded():
    """Diagnostics are held in memory for the whole document, so a file that is
    nothing but stray quotes must not turn into an unbounded list."""
    lines = ["id,region,note"]
    for index in range(4_000):
        note = '"unterminated' if index % 100 == 0 else "plain note"
        lines.append(f"{index},EMEA,{note}")
    payload = ("\n".join(lines) + "\n").encode()

    _, report = _read(payload)

    assert report.truncated, "a file of stray quotes reported nothing"
    assert len(report.truncated) <= 3, len(report.truncated)


# --------------------------------------------------------------------------- #
# Rule 2.
# --------------------------------------------------------------------------- #


class _CountingHandle(io.BytesIO):
    """A handle that remembers how much of itself was read."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.bytes_read = 0

    def read(self, size: int = -1, /) -> bytes:
        data = super().read(size)
        self.bytes_read += len(data)
        return data


def test_the_check_does_not_read_ahead():
    """Rule 2: the check may cost a counter, not a pass over the file. Taking one
    row must not pull a 10,000-row document into memory."""
    payload = _sales_csv()
    handle = _CountingHandle(payload)

    rows = iter_rows(handle, sheet="s")
    first = next(rows)
    read_for_one_row = handle.bytes_read
    rows.close()

    assert first.cells == ["id", "region", "note"]
    assert read_for_one_row < len(payload) // 4, f"{read_for_one_row} of {len(payload)} bytes"


@pytest.mark.parametrize("rows", [2_000, 20_000])
def test_the_report_does_not_grow_with_the_file(rows):
    """The diagnostic is O(1) in the document, not one entry per swallowed row."""
    _, report = _read(_sales_csv(rows, stray_quote_at=3))

    assert len(report.truncated) == 1
    assert len(report.notes) <= 2, report.notes
