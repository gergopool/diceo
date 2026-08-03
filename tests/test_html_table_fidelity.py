"""A cell's content is the cell's, whatever the author wrapped it in.

Two independent defects, both of which turned a real table into loose prose:

* **a block-level element inside `<td>`** -- `<p>`, `<div>`, `<ul>`, `<br>`, a heading,
  which is how most hand-written and every CMS-generated table marks up a cell -- ran
  `_flush`, and `_flush` empties `self._text` and appends to `self.out`. So the cell's
  text left as a standalone paragraph and the cell itself was placed *empty*: with
  every cell wrapped, `any(self._cells)` was False for every row and the table emitted
  **no `table_row` blocks at all**. The numbers then reached the index as bare
  paragraphs with no column name anywhere near them, because header prefixing
  (experiment 016, recovering boundaries blind) only happens for
  `table_row` -- the retrieval mechanism this package's HTML and DOCX advantage rests
  on was simply off for that table.
* **a source newline inside a cell** survived into the row. `table_row` blocks are
  tab-separated and the chunker joins the rows of a group with `"\\n"`, so a cell whose
  source text merely wrapped onto two lines forged a row boundary the document never
  had -- one row became two, and every field after the break sat under the wrong
  column name. HTML says the opposite: a segment break is a space (CSS Text 3 phase 1),
  which is why `_HTML_WHITESPACE` exists next to `_WHITESPACE`.

Neither is visible on the corpus: **0 wrapped cells and 0 newlines inside a block
across all 56 held-out HTML files** (they are generated one block per line, every end
tag present), which is why the digests do not move and why nothing caught this.
"""

from __future__ import annotations

import io

import diceo
from diceo.plaintext import iter_html_blocks
from diceo.types import Diagnostics


def _blocks(html: str) -> list[tuple[str, str]]:
    return [
        (b.kind, b.text) for b in iter_html_blocks(io.BytesIO(html.encode()), Diagnostics())
    ]


def _rows(html: str) -> list[list[str]]:
    return [text.split("\t") for kind, text in _blocks(html) if kind == "table_row"]


# --------------------------------------------------------------------------- #
# a block-level element inside a cell
# --------------------------------------------------------------------------- #


def test_a_paragraph_in_every_cell_still_makes_a_row():
    """The headline case: the table emitted no data row whatsoever."""
    rows = _rows(
        "<table>"
        "<tr><th>Region</th><th>Q1</th></tr>"
        "<tr><td><p>EMEA</p></td><td><p>1200</p></td></tr>"
        "</table>"
    )

    assert rows == [["Region", "Q1"], ["EMEA", "1200"]]


def test_a_div_in_one_cell_does_not_shift_the_others():
    rows = _rows("<table><tr><td><div>A</div></td><td>B</td></tr></table>")

    assert rows == [["A", "B"]], "A used to leave as a paragraph, leaving column 1 blank"


def test_a_list_in_a_cell_stays_in_the_cell():
    """A bulleted cell is one field, not two `list_item` blocks outside the table."""
    rows = _rows(
        "<table><tr><td>Cell</td><td><ul><li>one</li><li>two</li></ul></td></tr></table>"
    )

    assert rows == [["Cell", "one two"]]


def test_a_line_break_in_a_cell_is_a_space():
    rows = _rows("<table><tr><td>Line1<br>Line2</td><td>x</td></tr></table>")

    assert rows == [["Line1 Line2", "x"]]


def test_a_heading_in_a_cell_is_the_cell_text():
    """Infobox markup. It used to emit a `heading`, which also corrupts the trail."""
    html = "<table><tr><th><h3>Region</h3></th><td>1</td></tr></table>"

    assert _blocks(html) == [("table_row", "Region\t1")]


def test_wrapped_cells_still_honour_colspan_and_empty_cells():
    """The geometry fix and the wrapper fix have to hold at the same time."""
    rows = _rows(
        "<table>"
        "<tr><th colspan='2'><p>2023</p></th><th>2024</th></tr>"
        "<tr><td><p>EMEA</p></td><td><p></p></td><td><p>91</p></td></tr>"
        "</table>"
    )

    assert rows[0] == ["2023", "2023", "2024"]
    assert rows[1] == ["EMEA", "", "91"], "91 must stay in the third column"


def test_wrapped_cells_still_honour_rowspan():
    rows = _rows(
        "<table><tr><td rowspan='2'><p>EMEA</p></td><td>1</td></tr><tr><td>2</td></tr></table>"
    )

    assert rows == [["EMEA", "1"], ["EMEA", "2"]]


def test_a_nested_table_inside_a_wrapped_cell():
    """Both halves at once: layout nesting plus `<p>` wrappers, the real-world shape."""
    rows = _rows(
        "<table><tr><td>OUTER1</td>"
        "<td><p>OUTER 2</p>"
        "<table><tr><td><p>IN A</p></td><td>IN B</td></tr></table>"
        "<p>AFTER</p></td>"
        "<td>OUTER3</td></tr></table>"
    )

    assert rows == [["IN A", "IN B"], ["OUTER1", "OUTER 2 AFTER", "OUTER3"]]


# --------------------------------------------------------------------------- #
# holding a cell's text means never dropping it
# --------------------------------------------------------------------------- #


def test_a_row_left_open_at_the_end_of_the_table_is_still_emitted():
    """`</td>` and `</tr>` are both optional in HTML5, so this markup is legal.

    A cell now holds its text until it is placed, so the frame pop at `</table>`
    would take an unclosed row with it -- and the text is no longer loose in
    `self.out` as a paragraph to soften that. Rule 3: it is emitted.
    """
    assert _rows("<table><tr><td><p>A</p><td><p>B</p></table>") == [["A", "B"]]


def test_a_row_left_open_at_the_end_of_the_file_is_still_emitted():
    """A truncated download stops mid-table. The last row is still content."""
    assert _rows("<table><tr><td><p>A</p></td><td>B</td>") == [["A", "B"]]


# --------------------------------------------------------------------------- #
# source newlines
# --------------------------------------------------------------------------- #


def test_a_wrapped_cell_does_not_forge_a_row_boundary():
    rows = _rows("<table><tr><td>Total\nrevenue</td><td>5</td></tr></table>")

    assert rows == [["Total revenue", "5"]]
    assert "\n" not in "".join("".join(row) for row in rows)


def test_the_chunk_a_reader_gets_has_one_row_per_row():
    """The defect as the caller meets it: rows are joined on newlines downstream.

    Without the collapse this chunk reads as three data rows under a two-column
    header, and `5` sits under `Region`.
    """
    html = (
        b"<table>"
        b"<tr><th>Region</th><th>Total</th></tr>"
        b"<tr><td>Northern\nEurope</td><td>5</td></tr>"
        b"</table>"
    )
    pieces = list(diceo.chunk(html, name="t.html"))

    assert len(pieces) == 1
    body = pieces[0].text
    assert body.count("\n") == 1, body
    assert "Northern Europe\t5" in body
    assert "Region\tTotal" in body, "the header must still travel with the row"


def test_a_newline_in_alt_text_is_a_space():
    assert _blocks('<img alt="A chart\nof revenue">') == [("caption", "A chart of revenue")]


# --------------------------------------------------------------------------- #
# CONTROL -- the fix must not fire on ordinary documents
# --------------------------------------------------------------------------- #


def test_an_ordinary_table_is_byte_for_byte_what_it_was():
    """Bare `<td>text</td>` is what the whole held-out corpus contains.

    Pinned as exact blocks rather than a property, because this is the output the
    published html digest (245aa1e2e01e1b27) was measured on.
    """
    assert _blocks(
        "<table>"
        "<tr><th>Region</th><th>Q1</th><th>Q2</th></tr>"
        "<tr><td>EMEA</td><td>1200</td><td>91</td></tr>"
        "<tr><td>APAC</td><td>800</td><td>64</td></tr>"
        "</table>"
    ) == [
        ("table_row", "Region\tQ1\tQ2"),
        ("table_row", "EMEA\t1200\t91"),
        ("table_row", "APAC\t800\t64"),
    ]


def test_ordinary_prose_still_becomes_the_blocks_it_always_did():
    """Nothing outside a cell may take the fragment path."""
    assert _blocks(
        "<h1>Title</h1><p>First para.</p><div>A div.</div>"
        "<ul><li>one</li><li>two</li></ul><p>Last.</p>"
    ) == [
        ("heading", "Title"),
        ("paragraph", "First para."),
        ("paragraph", "A div."),
        ("list_item", "one"),
        ("list_item", "two"),
        ("paragraph", "Last."),
    ]


def test_a_pre_block_keeps_its_line_breaks():
    """`<pre>` is the one element whose segment breaks CSS does not collapse.

    Its line breaks are the only structure a pasted code block has, so the newline
    collapse that fixes the table must not reach it.
    """
    assert _blocks("<pre>line one\nline two\nline three</pre>") == [
        ("paragraph", "line one\nline two\nline three")
    ]


def test_a_paragraph_next_to_a_table_is_unaffected():
    assert _blocks("<p>Before.</p><table><tr><td>a</td></tr></table><p>After.</p>") == [
        ("paragraph", "Before."),
        ("table_row", "a"),
        ("paragraph", "After."),
    ]


def test_text_before_a_stray_td_is_still_emitted():
    """A `<td>` outside any `<tr>` cannot be placed, so it must not swallow the

    text in front of it -- the cell guard deliberately lets `td`/`th` fall through.
    """
    assert _blocks("<div>hello<td>x</td></div>") == [("paragraph", "hello")]


def test_the_boilerplate_counter_still_sees_a_nav():
    """The chrome path shares `_flush`; a cell must not disable it."""
    report = Diagnostics()
    blocks = list(
        iter_html_blocks(
            io.BytesIO(b"<nav>menu</nav><table><tr><td>a</td></tr></table>"), report
        )
    )

    assert [b.text for b in blocks] == ["a"]
    assert any(note.startswith("boilerplate_chars=") for note in report.notes)
