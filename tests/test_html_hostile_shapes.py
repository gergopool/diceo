"""Three HTML shapes a crawler meets and a browser shrugs at, carried as "still open"
on 2026-07-31 and fixed here.

* **A `<table>` whose rows never close lost every row but the last.** `</tr>` is
  *optional* in HTML5 -- the next `<tr>` is what closes a row -- and the start-tag
  handler reset `self._cells` instead of emitting them. A three-row table came back as
  `r3c1\tr3c2` and nothing else, `truncated` empty and `lost_data` False: two thirds of
  a table missing from the index with every observable reporting success, which is
  rule 3's stated worst case. A `<thead>` written the same way lost the header row,
  and that is the one row the chunker prefixes into all the others
  (experiment 016, recovering boundaries blind), so the damage is
  not proportional to the row.

* **`colspan`/`rowspan` replication was unbounded.** `_MAX_SPAN = 64` bounds one cell;
  nothing bounded a document. Measured through `iter_html_blocks`: **952 bytes reached
  1,232,832 characters and 1,852 bytes reached 4,919,232** -- a 2,656x amplification
  out of a file that fits in a tweet, with `truncated` empty and `lost_data` False. It
  is charged against a per-document budget now, and the refusal is a truncation
  entry. All 56 held-out HTML files replicate **0** characters, so this cannot fire on
  anything the corpus contains -- which is also why no digest moves.

* **The tolerant end-tag unwind was quadratic.** An end tag matching nothing on the
  ancestor stack walked the whole stack and deleted nothing, so it bought no
  amortisation: **4,000 stray `</u>` on a 28 KB page cost 16,012,002 stack reads, and
  the count squared with the document** (n -> 2n quadrupled it). Scraped HTML supplies
  both halves constantly -- unclosed `<b>`/`<font>` and end tags with no start tag. A
  census of open tag names answers "is there anything to unwind to?" in O(1), which
  leaves the scan paid for by the frames it deletes.

The last test is the control the other three are worth nothing without: an ordinary,
well-formed page must gain no note and no truncation entry from any of this. A
diagnostic that fires on healthy documents is noise a caller learns to ignore inside a
day, and then the real one goes unread too.
"""

from __future__ import annotations

import io

import diceo
from diceo.plaintext import _HtmlBlocks, iter_html_blocks
from diceo.types import Diagnostics


def _read(html: str) -> tuple[list[tuple[str, str]], Diagnostics]:
    report = Diagnostics()
    blocks = [(b.kind, b.text) for b in iter_html_blocks(io.BytesIO(html.encode()), report)]
    return blocks, report


def _rows(html: str) -> list[list[str]]:
    return [text.split("\t") for kind, text in _read(html)[0] if kind == "table_row"]


# --------------------------------------------------------------------------- #
# a table whose rows never close
# --------------------------------------------------------------------------- #


def test_rows_without_end_tags_all_survive():
    """The headline case: two of three rows used to be gone without a trace."""
    rows = _rows(
        "<table>"
        "<tr><td>r1c1</td><td>r1c2</td>"
        "<tr><td>r2c1</td><td>r2c2</td>"
        "<tr><td>r3c1</td><td>r3c2</td>"
        "</table>"
    )

    assert rows == [["r1c1", "r1c2"], ["r2c1", "r2c2"], ["r3c1", "r3c2"]]


def test_neither_end_tag_present():
    """`</td>` is optional too, and a hand-written table omits both."""
    rows = _rows(
        "<table>\n<tr><td>r1c1<td>r1c2\n<tr><td>r2c1<td>r2c2\n<tr><td>r3c1<td>r3c2\n</table>"
    )

    assert rows == [["r1c1", "r1c2"], ["r2c1", "r2c2"], ["r3c1", "r3c2"]]


def test_the_header_row_survives_an_unclosed_thead():
    """The expensive one. Header prefixing is the whole reason a `table_row` is
    recovered at all, so losing the header costs every data row, not one."""
    rows = _rows(
        "<table>"
        "<thead><tr><th>Region</th><th>Q1</th></thead>"
        "<tbody><tr><td>EMEA</td><td>1200</td>"
        "<tr><td>APAC</td><td>900</td></tbody>"
        "</table>"
    )

    assert rows == [["Region", "Q1"], ["EMEA", "1200"], ["APAC", "900"]]


def test_the_lost_rows_reach_a_caller_of_chunk():
    """End to end, because the reader is not what a caller holds."""
    html = (
        b"<html><body><h1>Regional sales</h1><table>"
        b"<tr><th>Region</th><th>Q1</th>"
        b"<tr><td>EMEA</td><td>1200</td>"
        b"<tr><td>APAC</td><td>900</td>"
        b"</table></body></html>"
    )
    report = diceo.Diagnostics()
    text = "\n".join(c.text for c in diceo.chunk(html, name="sales.html", diagnostics=report))

    assert "EMEA" in text and "1200" in text
    assert "Region" in text and "APAC" in text


def test_a_row_spanning_the_gap_still_carries_down():
    """`rowspan` is resolved when a row closes, so the implicit close has to do it."""
    rows = _rows(
        '<table><tr><td rowspan="3">EMEA</td><td>Q1</td><tr><td>Q2</td><tr><td>Q3</td></table>'
    )

    assert rows == [["EMEA", "Q1"], ["EMEA", "Q2"], ["EMEA", "Q3"]]


def test_loose_text_is_not_traded_for_the_row():
    """Recovering the row must not cost the text beside it.

    `_flush_cell` drops a fragment with no cell open, so routing the implicit close
    through it bought the row back by losing text that used to survive as a
    paragraph. Invalid markup -- a browser lifts it out of the table -- but rule 3
    has no clause for invalid markup, and trading one silent loss for another is not
    a fix.
    """
    blocks, _ = _read("<table><tr><td>a</td>STRAY<tr><td>b</td></table>")

    assert ("paragraph", "STRAY") in blocks
    assert ("table_row", "a") in blocks
    assert ("table_row", "b") in blocks


def test_a_closed_table_is_unaffected():
    """CONTROL for the row fix: well-formed markup takes the path it always took."""
    rows = _rows(
        "<table><tr><td>r1c1</td><td>r1c2</td></tr><tr><td>r2c1</td><td>r2c2</td></tr></table>"
    )

    assert rows == [["r1c1", "r1c2"], ["r2c1", "r2c2"]]


# --------------------------------------------------------------------------- #
# span replication, bounded and counted
# --------------------------------------------------------------------------- #


def _span_bomb(text_chars: int = 300, rows: int = 63) -> str:
    """A cell claiming 64 columns x 64 rows, then rows to pour it into."""
    return (
        "<html><body><table>"
        f'<tr><td colspan="64" rowspan="64">{"A" * text_chars}</td></tr>'
        + "<tr></tr>" * rows
        + "</table></body></html>"
    )


def test_span_expansion_is_bounded():
    """952 bytes produced 1,232,832 characters before this."""
    html = _span_bomb()
    blocks, report = _read(html)
    produced = sum(len(text) for _, text in blocks)

    assert len(html) < 1000
    assert produced < 5_000, f"{len(html)} bytes still produced {produced:,} characters"


def test_the_capped_expansion_is_reported():
    """Rule 3: bounded *and* counted. `lost_data` is what callers branch on."""
    _, report = _read(_span_bomb())

    assert report.lost_data is True
    entry = " || ".join(report.truncated)
    assert "table_span_expansion_capped=1" in entry
    assert "262,144" in entry


def test_the_cell_text_itself_still_comes_back():
    """The readable prefix survives the cap -- only the *repetition* is refused."""
    blocks, _ = _read(_span_bomb(text_chars=300))

    assert any("A" * 300 in text for _, text in blocks)


def test_the_budget_is_per_document_not_per_cell():
    """Many small cells add up to the same attack, so the budget accumulates."""
    cells = '<td colspan="64">QQQQQQQQQQ</td>' * 500
    _, report = _read(f"<table><tr>{cells}</tr></table>")

    assert report.lost_data is True
    assert any("table_span_expansion_capped" in item for item in report.truncated)


def test_a_document_under_the_budget_is_untouched():
    """CONTROL for the cap. 300 cells replicate 189,000 characters -- under
    262,144 -- and must be honoured in full, or the guard has become the defect."""
    cells = '<td colspan="64">QQQQQQQQQQ</td>' * 300
    blocks, report = _read(f"<table><tr>{cells}</tr></table>")

    assert report.truncated == []
    assert len(blocks[0][1].split("\t")) == 300 * 64


def test_ordinary_spans_are_still_honoured():
    """CONTROL for the cap. A merged header labels every column it covers, which is
    what the span *means* -- the cap must not quietly turn that off."""
    rows = _rows(
        "<table>"
        '<tr><th colspan="3">2023</th></tr>'
        "<tr><td>a</td><td>b</td><td>c</td></tr>"
        "</table>"
    )

    assert rows == [["2023", "2023", "2023"], ["a", "b", "c"]]


def test_ordinary_rowspans_are_still_honoured():
    rows = _rows(
        '<table><tr><td rowspan="2">EMEA</td><td>1200</td></tr><tr><td>900</td></tr></table>'
    )

    assert rows == [["EMEA", "1200"], ["EMEA", "900"]]


# --------------------------------------------------------------------------- #
# the tolerant unwind, linear by construction
# --------------------------------------------------------------------------- #


class _CountingStack(list):
    """A stack that records how often the unwind looks at a frame.

    Counted rather than timed on purpose: a wall-clock number measured on a shared
    box is not evidence, and the claim here is about the *shape* of the work.
    """

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def __getitem__(self, item):  # type: ignore[override]
        self.reads += 1
        return list.__getitem__(self, item)


def _stack_reads(html: str) -> int:
    parser = _HtmlBlocks()
    parser._stack = _CountingStack()
    parser.feed(html)
    parser.close()
    return parser._stack.reads


def test_stray_end_tags_cost_linear_work():
    """n unclosed `<b>` then n `</u>` that match nothing: the classic scraped page.

    Was 16,012,002 stack reads at n=4,000 and quadrupled with every doubling. The
    assertion is an absolute linear bound rather than a ratio, so it fails loudly if
    the census is ever removed.
    """
    for n in (250, 500, 1000, 2000, 4000):
        html = "<html><body>" + "<b>" * n + "text" + "</u>" * n + "</body></html>"
        reads = _stack_reads(html)

        assert reads <= 4 * n + 100, f"n={n} cost {reads:,} stack reads"


def test_doubling_the_document_doubles_the_work():
    """The exponent itself, since that is what the finding was about."""
    small = _stack_reads("<html><body>" + "<b>" * 1000 + "x" + "</u>" * 1000 + "</body></html>")
    large = _stack_reads("<html><body>" + "<b>" * 2000 + "x" + "</u>" * 2000 + "</body></html>")

    assert large / small < 2.5, f"{small:,} -> {large:,} is not linear"


def test_mis_nesting_still_unwinds_to_the_matching_tag():
    """CONTROL for the census: it must only skip scans that would have found nothing.

    A stray `</div>` must not desynchronise the depth tests, and a `</b>` that *is*
    open must still unwind everything opened after it.
    """
    rows = _rows("<table><tr><td><b><i>EMEA</b></td></div><td>1200</td></tr></table>")

    assert rows == [["EMEA", "1200"]]


def test_a_cell_closed_by_an_outer_tag_releases_the_cell_depth():
    """`_cell_depth` rides on the same unwind, and a wrong depth turns every later
    paragraph into a table fragment."""
    blocks, _ = _read("<table><tr><td>EMEA</table><p>After the table</p>")

    assert ("paragraph", "After the table") in blocks
    assert ("table_row", "EMEA") in blocks


# --------------------------------------------------------------------------- #
# the control that all three fixes are judged against
# --------------------------------------------------------------------------- #


def test_a_healthy_page_gains_nothing_at_all():
    """No note, no truncation entry, `lost_data` False. If this ever fails, one of the
    guards above has started firing on ordinary documents and is worth less than
    nothing."""
    blocks, report = _read(
        "<body><h1>Quarterly report</h1>"
        "<p>Revenue rose in every region.</p>"
        "<table>"
        "<tr><th>Region</th><th>Q1</th></tr>"
        "<tr><td>EMEA</td><td>1200</td></tr>"
        "<tr><td>APAC</td><td>900</td></tr>"
        "</table>"
        "<ul><li>First</li><li>Second</li></ul>"
        "</body>"
    )

    assert report.notes == []
    assert report.truncated == []
    assert report.lost_data is False
    assert ("heading", "Quarterly report") in blocks
    assert ("table_row", "EMEA\t1200") in blocks


def test_a_realistic_page_gains_only_its_title():
    """The same, with the furniture a real page carries: the only diagnostic a clean
    document produces is the one that was always there."""
    _, report = _read(
        "<html><head><title>Quarterly report</title></head><body>"
        "<nav><a href='/'>Home</a></nav>"
        "<main><h1>Quarterly report</h1><p>Revenue rose.</p>"
        "<table><tr><th>Region</th><th>Q1</th></tr>"
        "<tr><td>EMEA</td><td>1200</td></tr></table></main>"
        "</body></html>"
    )

    assert report.truncated == []
    assert report.lost_data is False
    assert not any("table_span_expansion_capped" in note for note in report.notes)
    assert [note for note in report.notes if note.startswith("title=")]
