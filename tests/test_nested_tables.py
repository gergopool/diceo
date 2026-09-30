"""A table inside a table deleted the outer row's cells. In both readers.

Word and every HTML page-layout tool nest tables. Both readers kept the row being
built in **one** variable and cleared it whenever a row opened, so the inner table's
first `<tr>` threw away everything the outer row had collected — and then the inner
table's *closing* row reset the "we are in a cell" flag, so the rest of the outer row
was demoted from table cells to loose paragraphs.

Reproduced independently in both readers. On a DOCX with outer cells
`[OUTER CELL 1][OUTER CELL 2 + inner table + AFTER INNER][OUTER CELL 3]`, diceo
emitted the inner row, then `AFTER INNER` and `OUTER CELL 3` as *paragraphs*, and
**`OUTER CELL 1` and `OUTER CELL 2` appeared nowhere at all** — with
`lost_data=False`.

Neither corpus can see it: 0 nested `w:tbl` across the 17 real DOCX files we measure
on, and 0 in the 56 held-out HTML files. The project's own experimental OOXML scanner
already keeps a stack; the shipped readers did not.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks
from diceo.plaintext import iter_html_blocks
from diceo.types import Diagnostics

from .fixtures import _CONTENT_TYPES, _W_NS


def _docx(body: str) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS}>'
        f"<w:body>{body}</w:body></w:document>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _cell(*paragraphs: str) -> str:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return f"<w:tc>{body}</w:tc>"


def _blocks(path: Path):
    return list(iter_docx_blocks(path, diagnostics=OoxmlDiagnostics()))


#: An outer 3-cell row whose middle cell also contains a 2-cell table, then a second
#: ordinary outer row. The shape every layout-nested Word document has.
_NESTED = (
    "<w:tbl><w:tr>"
    + _cell("OUTER CELL 1")
    + "<w:tc><w:p><w:r><w:t>OUTER CELL 2</w:t></w:r></w:p>"
    + "<w:tbl><w:tr>"
    + _cell("INNER A")
    + _cell("INNER B")
    + "</w:tr></w:tbl>"
    + "<w:p><w:r><w:t>AFTER INNER</w:t></w:r></w:p></w:tc>"
    + _cell("OUTER CELL 3")
    + "</w:tr>"
    + "<w:tr>"
    + _cell("ROW2 A")
    + _cell("ROW2 B")
    + "</w:tr></w:tbl>"
)


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #


def test_no_outer_cell_text_disappears(tmp_path):
    """The rule-3 assertion, and the one that was failing: whatever the grouping,
    every authored string has to be *somewhere* in the output."""
    path = tmp_path / "nested.docx"
    path.write_bytes(_docx(_NESTED))

    joined = "\n".join(b.text for b in _blocks(path))

    for text in (
        "OUTER CELL 1",
        "OUTER CELL 2",
        "OUTER CELL 3",
        "AFTER INNER",
        "INNER A",
        "INNER B",
        "ROW2 A",
        "ROW2 B",
    ):
        assert text in joined, f"{text!r} vanished: {joined!r}"


def test_the_outer_row_is_still_one_row(tmp_path):
    path = tmp_path / "nested.docx"
    path.write_bytes(_docx(_NESTED))

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]

    outer = next((r for r in rows if "OUTER CELL 1" in r), None)
    assert outer is not None, rows
    assert "OUTER CELL 3" in outer, f"the outer row was cut in half: {outer!r}"


def test_the_inner_table_is_its_own_row(tmp_path):
    path = tmp_path / "nested.docx"
    path.write_bytes(_docx(_NESTED))

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]

    assert "INNER A | INNER B" in rows, rows


def test_text_after_the_inner_table_stays_a_table_cell(tmp_path):
    """`AFTER INNER` is cell content. Emitting it as a loose paragraph loses which
    row it belonged to, which is the whole reason `table_row` exists."""
    path = tmp_path / "nested.docx"
    path.write_bytes(_docx(_NESTED))

    loose = [b.text for b in _blocks(path) if b.kind == "paragraph"]

    assert "AFTER INNER" not in loose, loose
    assert "OUTER CELL 3" not in loose, loose


def test_the_second_outer_row_is_row_one(tmp_path):
    """Row numbering is part of a chunk's identity (D8). The inner table reset the
    outer counter, so both outer rows reported row 0."""
    path = tmp_path / "nested.docx"
    path.write_bytes(_docx(_NESTED))

    rows = [b for b in _blocks(path) if b.kind == "table_row"]
    second = next(b for b in rows if "ROW2 A" in b.text)
    first = next(b for b in rows if "OUTER CELL 1" in b.text)

    assert first.row == 0
    assert second.row == 1, f"the outer row counter was reset by the inner table: {second.row}"


def test_nesting_is_counted(tmp_path):
    path = tmp_path / "nested.docx"
    path.write_bytes(_docx(_NESTED))

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.nested_tables == 1


def test_an_ordinary_table_is_untouched(tmp_path):
    path = tmp_path / "flat.docx"
    path.write_bytes(_docx("<w:tbl><w:tr>" + _cell("a") + _cell("b") + "</w:tr></w:tbl>"))

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]

    assert rows == ["a | b"]


def test_three_levels_deep(tmp_path):
    """A stack, not a single saved frame. (An *unbalanced* `</w:tbl>` cannot be
    tested here: it is not well-formed XML, so it never reaches this code.)"""
    path = tmp_path / "deep.docx"
    path.write_bytes(
        _docx(
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>L1</w:t></w:r></w:p>"
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>L2</w:t></w:r></w:p>"
            "<w:tbl><w:tr>" + _cell("L3") + "</w:tr></w:tbl>"
            "<w:p><w:r><w:t>L2 END</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "<w:p><w:r><w:t>L1 END</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        )
    )

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]
    joined = "\n".join(rows)

    assert "L3" in joined
    for text in ("L1", "L2", "L1 END", "L2 END"):
        assert text in joined, f"{text!r} lost at depth: {rows}"


# --------------------------------------------------------------------------- #
# HTML -- the same defect, the same shape
# --------------------------------------------------------------------------- #

_NESTED_HTML = (
    "<table><tr><td>OUTER 1</td>"
    "<td>OUTER 2<table><tr><td>INNER A</td><td>INNER B</td></tr></table>AFTER</td>"
    "<td>OUTER 3</td></tr>"
    "<tr><td>ROW2 A</td><td>ROW2 B</td></tr></table>"
)


def _html_rows(html: str) -> list[str]:
    blocks = list(iter_html_blocks(io.BytesIO(html.encode()), Diagnostics()))
    return [b.text for b in blocks if b.kind == "table_row"]


def test_html_keeps_the_outer_row(tmp_path):
    rows = _html_rows(_NESTED_HTML)
    joined = "\n".join(rows)

    assert "INNER A | INNER B" in rows, rows
    for text in ("OUTER 1", "OUTER 2", "OUTER 3", "ROW2 A", "ROW2 B"):
        assert text in joined, f"{text!r} vanished: {rows}"


def test_html_outer_row_is_not_cut_in_half():
    rows = _html_rows(_NESTED_HTML)

    outer = next((r for r in rows if "OUTER 1" in r), None)
    assert outer is not None, rows
    assert "OUTER 3" in outer, f"the outer row was split: {outer!r}"


def test_html_second_row_survives():
    rows = _html_rows(_NESTED_HTML)

    assert any(r == "ROW2 A | ROW2 B" for r in rows), rows


def test_html_flat_table_unchanged():
    rows = _html_rows("<table><tr><td>a</td><td>b</td></tr></table>")

    assert rows == ["a | b"]


def test_nested_html_restores_the_outer_header():
    html = (
        "<table><tr><th>Outer header</th><th>Context"
        "<table><tr><th>Inner header</th></tr><tr><td>Inner value</td></tr></table>"
        "</th></tr>"
        + "".join(f"<tr><td>Outer value {i}</td><td>42</td></tr>" for i in range(8))
        + "</table>"
    ).encode()
    rows = [b for b in diceo.extract(html, name="nested.html") if b.kind == "table_row"]
    outer = next(row for row in rows if "Outer header" in row.text)
    assert outer.locator.row == 0
    chunks = list(diceo.chunk(html, name="nested.html", limits=diceo.Limits(target_chars=40)))
    values = [c.text for c in chunks if "Outer value 7" in c.text]
    assert values and all(
        "Outer header" in text and "Inner header" not in text for text in values
    )
