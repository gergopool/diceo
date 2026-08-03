"""An HTML row must have one field per column, or its numbers mean nothing.

`table_row` blocks are tab-separated and the chunker prefixes the header row, so a
reader — human or embedding model — matches the *n*th field of a row against the
*n*th column of the header. Three things broke that, all in the same way:

* **empty cells were dropped** (`if cell:`), so `<td>EMEA</td><td></td><td>91</td>`
  emitted two fields and 91 landed under the second column's name;
* **`colspan` was ignored**, so `<th colspan="3">2023</th>` labelled one column and
  every later field shifted left by two;
* **`rowspan` was ignored**, so the row *after* a vertically-merged cell was short by
  one field from its first column onward.

None of these can be seen on the corpus: **0 empty cells and 0 span attributes across
all 56 held-out HTML files**, which is why the digests do not move and why nothing
caught it. Real tables — government statistics, Wikipedia infoboxes, any table with a
grouped header — use all three constantly.

The expansion repeats a spanned cell's text into each column it covers, which is what
the span *means*: `<th colspan="3">2023</th>` really does label three columns, and
emitting `2023` once plus two blanks would lose the label for two of them.
"""

from __future__ import annotations

import diceo
from diceo.plaintext import iter_html_blocks
from diceo.types import Diagnostics


def _rows(html: str) -> list[list[str]]:
    import io

    blocks = list(iter_html_blocks(io.BytesIO(html.encode()), Diagnostics()))
    return [b.text.split("\t") for b in blocks if b.kind == "table_row"]


# --------------------------------------------------------------------------- #
# empty cells
# --------------------------------------------------------------------------- #


def test_an_empty_cell_keeps_its_column():
    rows = _rows(
        "<table>"
        "<tr><th>Region</th><th>Q1</th><th>Q2</th></tr>"
        "<tr><td>EMEA</td><td></td><td>91</td></tr>"
        "</table>"
    )

    assert rows[0] == ["Region", "Q1", "Q2"]
    assert rows[1] == ["EMEA", "", "91"], "91 must stay under Q2, not slide into Q1"


def test_a_trailing_empty_cell_is_not_padding():
    """Trailing blanks carry no information and cost a tab each."""
    rows = _rows("<table><tr><td>EMEA</td><td>1200</td><td></td><td></td></tr></table>")

    assert rows[0] == ["EMEA", "1200"]


def test_a_row_of_nothing_but_empty_cells_is_not_a_row():
    rows = _rows("<table><tr><td></td><td></td></tr><tr><td>real</td></tr></table>")

    assert rows == [["real"]]


# --------------------------------------------------------------------------- #
# colspan
# --------------------------------------------------------------------------- #


def test_colspan_covers_the_columns_it_claims():
    rows = _rows(
        "<table>"
        '<tr><th>Region</th><th colspan="2">2023</th></tr>'
        "<tr><td>EMEA</td><td>46</td><td>91</td></tr>"
        "</table>"
    )

    assert rows[0] == ["Region", "2023", "2023"]
    assert rows[1] == ["EMEA", "46", "91"]


def test_a_colspan_data_cell_expands_too():
    rows = _rows(
        "<table>"
        "<tr><th>Region</th><th>Q1</th><th>Q2</th></tr>"
        '<tr><td>APAC</td><td colspan="2">n/a</td></tr>'
        "</table>"
    )

    assert rows[1] == ["APAC", "n/a", "n/a"]


def test_a_nonsense_colspan_is_ignored_rather_than_trusted():
    """`colspan="0"` is legal HTML meaning "to the end of the column group", and
    `colspan="9999"` is a hostile file. Neither may be expanded literally."""
    rows = _rows('<table><tr><td colspan="0">a</td><td colspan="99999">b</td></tr></table>')

    assert len(rows[0]) <= 66, rows[0]


def test_colspan_that_is_not_a_number_does_not_raise():
    rows = _rows('<table><tr><td colspan="two">a</td><td>b</td></tr></table>')

    assert rows[0] == ["a", "b"]


# --------------------------------------------------------------------------- #
# rowspan
# --------------------------------------------------------------------------- #


def test_rowspan_carries_down_into_the_next_row():
    rows = _rows(
        "<table>"
        "<tr><th>Region</th><th>Quarter</th><th>Revenue</th></tr>"
        '<tr><td rowspan="2">EMEA</td><td>Q1</td><td>46</td></tr>'
        "<tr><td>Q2</td><td>91</td></tr>"
        "</table>"
    )

    assert rows[1] == ["EMEA", "Q1", "46"]
    assert rows[2] == ["EMEA", "Q2", "91"], "the second quarter is still EMEA's"


def test_rowspan_expires():
    rows = _rows(
        "<table>"
        '<tr><td rowspan="2">EMEA</td><td>Q1</td></tr>'
        "<tr><td>Q2</td></tr>"
        "<tr><td>APAC</td><td>Q1</td></tr>"
        "</table>"
    )

    assert rows[2] == ["APAC", "Q1"], "EMEA must not leak into a third row"


def test_rowspan_in_a_middle_column_lands_in_the_middle():
    rows = _rows(
        "<table>"
        '<tr><td>a1</td><td rowspan="2">shared</td><td>c1</td></tr>'
        "<tr><td>a2</td><td>c2</td></tr>"
        "</table>"
    )

    assert rows[0] == ["a1", "shared", "c1"]
    assert rows[1] == ["a2", "shared", "c2"]


def test_a_new_table_forgets_the_previous_one_s_spans():
    rows = _rows(
        '<table><tr><td rowspan="5">stale</td><td>x</td></tr></table>'
        "<table><tr><td>fresh</td></tr></table>"
    )

    assert rows[-1] == ["fresh"]


def test_both_spans_at_once():
    rows = _rows(
        "<table>"
        '<tr><td rowspan="2" colspan="2">corner</td><td>c1</td></tr>'
        "<tr><td>c2</td></tr>"
        "</table>"
    )

    assert rows[0] == ["corner", "corner", "c1"]
    assert rows[1] == ["corner", "corner", "c2"]


# --------------------------------------------------------------------------- #
# the shape that motivated it
# --------------------------------------------------------------------------- #


def test_a_grouped_header_table_end_to_end(tmp_path):
    """The government-statistics shape: a spanning group header over quarters, and a
    row label merged down the side. Every number must sit under its own column."""
    path = tmp_path / "stats.html"
    path.write_bytes(
        b"<html><body><h1>Regional revenue</h1><table>"
        b'<tr><th rowspan="2">Region</th><th colspan="2">2023</th>'
        b'<th colspan="2">2024</th></tr>'
        b"<tr><th>H1</th><th>H2</th><th>H1</th><th>H2</th></tr>"
        b"<tr><td>EMEA</td><td>46</td><td>91</td><td>77</td><td>70</td></tr>"
        b"</table></body></html>"
    )

    text = "\n".join(piece.text for piece in diceo.chunk(path))

    assert "Region\t2023\t2023\t2024\t2024" in text
    assert "Region\tH1\tH2\tH1\tH2" in text
    assert "EMEA\t46\t91\t77\t70" in text
