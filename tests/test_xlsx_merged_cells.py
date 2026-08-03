"""A vertically merged body label reaches only its anchor row -- measured, then priced.

A merged range stores its value in the top-left cell only. So a category column
merged down five data rows puts the label on row 1 of the range and leaves rows
2..5 blank, and those four rows go into the index with no key:

    Region | Country | Sales
    EMEA   | France  | 100
           | Germany | 200      <- A2:A4 merged; the reader yields ['', 'Germany', '200']
           | Spain   | 150

Reproduced against `iter_rows` before anything was written here, and the rows above
are the actual output.

**We do not forward-fill, and that is a measurement rather than a shrug.** Over the
five real public-office workbooks -- 70 sheets, `<mergeCells>` on
30 of them, 63 ranges -- exactly **5** ranges span more than one row, and only **2**
of those start below the detected first data row. Both are in
`worldbank-pink-sheet-monthly.xlsx`, sheet *Index Weights*: `C77:O78` and `C79:O82`,
which are footnote paragraphs Excel merged so the text wraps, not row labels. Their
continuation rows are entirely empty and the reader never yields them, so a
forward-fill would have filled nothing. The classic merged-key shape does not occur
once in the corpus.

Against that, filling is not free: experiment 037 measured us as the most verbose
tool on real spreadsheets, with 56.6% of the World Bank output already being one
repeated header, and a filled key repeats into *every* row of its range. Rule 4
says a change ships when the harness says it helps; nothing here says it would, and
the corpus cannot even exercise it.

What rule 3 does demand is that the shape stop being invisible, so the ranges are
counted and reported. Zero characters of chunk text, so no digest moves.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.sheets import SheetDiagnostics, iter_rows

from .fixtures import _CONTENT_TYPES

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _book(sheet_body: str, merges: str = "", *, sheet: str = "S") -> bytes:
    """`<mergeCells>` goes *after* `</sheetData>`, which is where the schema puts it
    and the reason the header detector cannot consult it in a single pass."""
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        f'Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{_NS}" '
        f'xmlns:r="{_REL}"><sheets><sheet name="{sheet}" sheetId="1" r:id="rId1"/>'
        "</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL}/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="{_NS}">'
        f"<sheetData>{sheet_body}</sheetData>{merges}</worksheet>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _merge_cells(*references: str) -> str:
    inner = "".join(f'<mergeCell ref="{reference}"/>' for reference in references)
    return f'<mergeCells count="{len(references)}">{inner}</mergeCells>'


def _text(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def _number(reference: str, value: str) -> str:
    return f'<c r="{reference}"><v>{value}</v></c>'


def _merged_key_book() -> bytes:
    """`Region` merged down A2:A4, the shape the finding is about."""
    body = [
        '<row r="1">'
        + _text("A1", "Region")
        + _text("B1", "Country")
        + _text("C1", "Sales")
        + "</row>",
        '<row r="2">'
        + _text("A2", "EMEA")
        + _text("B2", "France")
        + _number("C2", "100")
        + "</row>",
        '<row r="3">' + _text("B3", "Germany") + _number("C3", "200") + "</row>",
        '<row r="4">' + _text("B4", "Spain") + _number("C4", "150") + "</row>",
    ]
    return _book("".join(body), _merge_cells("A2:A4"))


def _rows(path: Path, report: SheetDiagnostics | None = None) -> list[list[str]]:
    return [row.cells for row in iter_rows(path, diagnostics=report or SheetDiagnostics())]


# --------------------------------------------------------------------------- #
# the loss, pinned as a decision rather than fixed
# --------------------------------------------------------------------------- #


def test_rows_below_a_merged_key_are_read_blank(tmp_path):
    """Deliberate. Pinned so that a later forward-fill has to change this test on
    purpose, with a measurement, rather than drift into the output."""
    path = tmp_path / "merged-key.xlsx"
    path.write_bytes(_merged_key_book())

    assert _rows(path) == [
        ["Region", "Country", "Sales"],
        ["EMEA", "France", "100"],
        ["", "Germany", "200"],
        ["", "Spain", "150"],
    ]


def test_the_merged_value_still_reaches_the_index_once(tmp_path):
    """The label is not *lost* -- it is under-attributed. A query for `EMEA` still
    hits the sheet; a query for `EMEA Spain` does not. The observed row group is

        Region | Country | Sales
        EMEA | France | 100
         | Germany | 200
         | Spain | 150
    """
    path = tmp_path / "merged-key.xlsx"
    path.write_bytes(_merged_key_book())

    group = next(piece for piece in diceo.chunk(path) if "sheet_row" in piece.kinds)

    assert group.text.count("EMEA") == 1, group.text
    assert group.text.splitlines()[2:] == [" | Germany | 200", " | Spain | 150"], group.text


# --------------------------------------------------------------------------- #
# rule 3: the shape is counted
# --------------------------------------------------------------------------- #


def test_a_vertical_merge_is_counted(tmp_path):
    """Before this, `mergeCells` was not read at all -- the element name appeared in
    the codebase only in a docstring explaining why the header detector cannot use
    it -- so a sheet full of merged keys looked like a clean rectangle."""
    path = tmp_path / "merged-key.xlsx"
    path.write_bytes(_merged_key_book())

    report = SheetDiagnostics()
    _rows(path, report)

    assert report.vertical_merges == [("S", 1)], report.vertical_merges


def test_a_purely_horizontal_merge_is_not_counted(tmp_path):
    """A range merged *across* loses nothing: its blanks sit to the right of the
    value and `_flatten` already resolves them in a header. Counting those would
    bury the signal -- 58 of the 63 real ranges measured are horizontal."""
    path = tmp_path / "horizontal.xlsx"
    path.write_bytes(
        _book(
            '<row r="1">' + _text("A1", "Water") + "</row>"
            '<row r="2">' + _text("A2", "In") + _text("B2", "Out") + "</row>",
            _merge_cells("A1:B1"),
        )
    )

    report = SheetDiagnostics()
    _rows(path, report)

    assert report.vertical_merges == [], report.vertical_merges


def test_a_sheet_with_no_merges_says_nothing(tmp_path):
    path = tmp_path / "plain.xlsx"
    path.write_bytes(_book('<row r="1">' + _text("A1", "a") + "</row>"))

    report = SheetDiagnostics()
    _rows(path, report)

    assert report.vertical_merges == []


def test_a_single_cell_merge_reference_does_not_crash(tmp_path):
    """`ref="A1"` with no colon is unusual but legal-looking, and a reference the
    regex cannot read must be ignored rather than raise inside the row stream."""
    path = tmp_path / "odd.xlsx"
    path.write_bytes(
        _book(
            '<row r="1">' + _text("A1", "a") + "</row>",
            _merge_cells("A1", "not a reference", "", "A1:A3"),
        )
    )

    report = SheetDiagnostics()

    assert _rows(path, report) == [["a"]]
    assert report.vertical_merges == [("S", 1)], report.vertical_merges


def test_the_count_reaches_the_public_diagnostics(tmp_path):
    """A counter nobody can read is a log line, not a diagnostic (rule 3)."""
    path = tmp_path / "merged-key.xlsx"
    path.write_bytes(_merged_key_book())

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any(note.startswith("vertical_merges=1 in S") for note in report.notes), report.notes


def test_merges_do_not_make_the_document_a_loss(tmp_path):
    """`lost_data` means characters did not reach the index. A merged key is a
    weaker claim than that and must not be inflated into one."""
    path = tmp_path / "merged-key.xlsx"
    path.write_bytes(_merged_key_book())

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert not report.lost_data, report.as_dict()
