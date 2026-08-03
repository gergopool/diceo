"""A cell belongs to the column its reference names, not to its turn in the file.

Two defects, both found by reading the SpreadsheetML spec against the reader:

* **Cells arriving out of column order were appended rather than placed.** The
  padding was one-directional — `if index > len(cells)` extends, and anything else
  falls through to `append` — so a row written `C2` then `A2` came out as
  `['', '', 'THIRD', 'FIRST']`: `FIRST` sitting in a phantom fourth column, and every
  value under the wrong header. Excel writes cells in order; plenty of other
  producers do not, and the spec does not require it.
* **`t="b"` with no cached `<v>` rendered `FALSE`.** An empty boolean cell is empty.
  Rendering it as `FALSE` is *fabricated data* — worse than a blank, because nothing
  downstream can tell it apart from a real FALSE.

Also pinned here: an error cell (`t="e"`) and an unevaluated formula keep their
*position* even though their text is not indexed, because dropping the cell shifts
every later column one place left.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from diceo.sheets import SheetDiagnostics, iter_rows

from .fixtures import _CONTENT_TYPES

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _book(sheet_body: str) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        f'Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{_NS}" '
        f'xmlns:r="{_REL}"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>",
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL}/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="{_NS}">'
        f"<sheetData>{sheet_body}</sheetData></worksheet>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _rows(path: Path, report: SheetDiagnostics | None = None) -> list[list[str]]:
    return [row.cells for row in iter_rows(path, diagnostics=report or SheetDiagnostics())]


def _s(reference: str, text: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{text}</t></is></c>'


# --------------------------------------------------------------------------- #
# column order
# --------------------------------------------------------------------------- #


def test_out_of_order_cells_land_in_their_own_columns(tmp_path):
    path = tmp_path / "unordered.xlsx"
    path.write_bytes(
        _book(
            f'<row r="1">{_s("A1", "alpha")}{_s("B1", "beta")}{_s("C1", "gamma")}</row>'
            f'<row r="2">{_s("C2", "THIRD")}{_s("A2", "FIRST")}</row>'
        )
    )

    rows = _rows(path)

    assert rows[0] == ["alpha", "beta", "gamma"]
    assert rows[1] == ["FIRST", "", "THIRD"], rows


def test_a_fully_reversed_row(tmp_path):
    path = tmp_path / "reversed.xlsx"
    path.write_bytes(
        _book(f'<row r="1">{_s("D1", "d")}{_s("C1", "c")}{_s("B1", "b")}{_s("A1", "a")}</row>')
    )

    assert _rows(path) == [["a", "b", "c", "d"]]


def test_a_duplicate_reference_does_not_grow_the_row(tmp_path):
    """Two cells claiming A1 is malformed. Whichever wins, the row must stay one
    column wide rather than gaining a phantom second."""
    path = tmp_path / "duplicate.xlsx"
    path.write_bytes(_book(f'<row r="1">{_s("A1", "first")}{_s("A1", "second")}</row>'))

    rows = _rows(path)

    assert len(rows[0]) == 1, rows


def test_ordered_cells_are_unaffected(tmp_path):
    path = tmp_path / "ordered.xlsx"
    path.write_bytes(_book(f'<row r="1">{_s("A1", "a")}{_s("C1", "c")}{_s("E1", "e")}</row>'))

    assert _rows(path) == [["a", "", "c", "", "e"]]


def test_cells_with_no_reference_still_follow_each_other(tmp_path):
    """`r` is optional; without it a cell's column is its position in the row."""
    path = tmp_path / "noref.xlsx"
    path.write_bytes(
        _book(
            '<row r="1"><c t="inlineStr"><is><t>a</t></is></c>'
            '<c t="inlineStr"><is><t>b</t></is></c></row>'
        )
    )

    assert _rows(path) == [["a", "b"]]


# --------------------------------------------------------------------------- #
# typed cells with nothing in them
# --------------------------------------------------------------------------- #


def test_a_boolean_with_no_value_is_empty_not_false(tmp_path):
    path = tmp_path / "bool.xlsx"
    path.write_bytes(
        _book(
            '<row r="1"><c r="A1" t="b"/>'
            '<c r="B1" t="b"><v>1</v></c>'
            '<c r="C1" t="b"><v>0</v></c></row>'
        )
    )

    assert _rows(path) == [["", "TRUE", "FALSE"]], _rows(path)


def test_a_real_boolean_still_renders(tmp_path):
    path = tmp_path / "bool.xlsx"
    path.write_bytes(_book('<row r="1"><c r="A1" t="b"><v>1</v></c></row>'))

    assert _rows(path) == [["TRUE"]]


def test_an_error_cell_keeps_its_column(tmp_path):
    """Its text is *not* indexed — `#DIV/0!` is a spreadsheet defect, not content —
    but the column has to survive, or every later value shifts one place left."""
    path = tmp_path / "error.xlsx"
    path.write_bytes(
        _book(
            f'<row r="1">{_s("A1", "left")}'
            '<c r="B1" t="e"><v>#DIV/0!</v></c>'
            f"{_s('C1', 'right')}</row>"
        )
    )

    report = SheetDiagnostics()
    rows = _rows(path, report)

    assert rows[0][0] == "left"
    assert rows[0][2] == "right", rows
    assert report.error_cells == 1


def test_an_unevaluated_formula_keeps_its_column(tmp_path):
    path = tmp_path / "formula.xlsx"
    path.write_bytes(
        _book(
            f'<row r="1">{_s("A1", "left")}'
            '<c r="B1"><f>SUM(X1:Y1)</f></c>'
            f"{_s('C1', 'right')}</row>"
        )
    )

    report = SheetDiagnostics()
    rows = _rows(path, report)

    assert rows[0] == ["left", "", "right"], rows
    assert report.formula_cells_unevaluated == 1
