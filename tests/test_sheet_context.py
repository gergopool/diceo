"""Regression checks for the row adapter and retrieval column context."""

from __future__ import annotations

from types import SimpleNamespace

import python_calamine
import xlsxwriter

from diceo.legacy_sheets import iter_rows
from diceo.sheets import Row, SheetDiagnostics, _profile, iter_sheet_chunks


def test_native_row_iterator_keeps_positions_empty_and_hidden_sheets(tmp_path):
    path = tmp_path / "positions.xlsx"
    with xlsxwriter.Workbook(path) as book:
        sheet = book.add_worksheet("Visible")
        sheet.write_row(3, 2, ["Region", "Value"])
        sheet.write_row(4, 2, ["EMEA", 1200])
        book.add_worksheet("Empty")
        sheet = book.add_worksheet("Hidden")
        sheet.hide()
        sheet.write(0, 0, "Retained")
        sheet = book.add_worksheet("Internal")
        sheet.very_hidden()
        sheet.write(0, 0, "Retained too")
    report = SheetDiagnostics()
    # The adapter delegates format detection to Calamine; XLSX exercises the
    # same binding without storing a legacy binary workbook in the test suite.
    assert list(iter_rows(path, kind="xls", diagnostics=report)) == [
        Row("Visible", 4, ["", "", "Region", "Value"]),
        Row("Visible", 5, ["", "", "EMEA", "1200"]),
        Row("Hidden", 1, ["Retained"]),
        Row("Internal", 1, ["Retained too"]),
    ]
    assert report.sheets_without_rows == ["Empty"]
    assert report.sheets_hidden == [("Hidden", "hidden"), ("Internal", "veryHidden")]


def test_small_sheet_bulk_conversion_is_bounded_including_leading_cells(tmp_path, monkeypatch):
    path = tmp_path / "physical_bounds.xlsx"
    positions = {
        "Small": (3, 2),
        "Boundary": (31, 127),
        "Tall": (4096, 0),
        "Wide": (0, 4096),
        "Offset": (32, 127),
    }
    with xlsxwriter.Workbook(path) as book:
        for name, (row, column) in positions.items():
            book.add_worksheet(name).write(row, column, name)
        book.add_worksheet("Empty")
    native = python_calamine.CalamineWorkbook.from_path(str(path))
    calls = []

    class TrackedSheet:
        def __init__(self, sheet):
            self.sheet = sheet
            self.start, self.end = sheet.start, sheet.end

        def to_python(self, **kwargs):
            calls.append((self.sheet.name, "bulk"))
            return self.sheet.to_python(**kwargs)

        def iter_rows(self):
            calls.append((self.sheet.name, "iterator"))
            return self.sheet.iter_rows()

    # sheet_names is deliberately absent: the adapter already has every name in
    # sheets_metadata and must not allocate another native-to-Python name list.
    book = SimpleNamespace(
        sheets_metadata=native.sheets_metadata,
        get_sheet_by_name=lambda name: TrackedSheet(native.get_sheet_by_name(name)),
    )
    monkeypatch.setattr(
        python_calamine, "CalamineWorkbook", SimpleNamespace(from_path=lambda path: book)
    )
    report = SheetDiagnostics()
    assert list(iter_rows(path, kind="xls", diagnostics=report, max_rows=1)) == [
        Row(name, row + 1, [""] * column + [name]) for name, (row, column) in positions.items()
    ]
    assert calls == [
        ("Small", "bulk"),
        ("Boundary", "bulk"),
        ("Tall", "iterator"),
        ("Wide", "iterator"),
        ("Offset", "iterator"),
    ]
    assert report.sheets_without_rows == ["Empty"]


def test_year_columns_are_headers_but_year_valued_body_rows_are_not():
    cells = ["Road user type", "Sex", "2016", "2017 [note 3]", "2018"]
    rows = [Row("S", 1, ["Published statistics"]), Row("S", 2, cells)]
    rows += [
        Row("S", number, ["Pedestrian", "Male", "22", "12", "15"]) for number in range(3, 12)
    ]
    profile = _profile("S", rows, (None, None))
    assert profile.header == cells
    assert profile.first_data_row == 3 and profile.preamble_rows == 1
    groups = list(iter_sheet_chunks("unused", rows=rows, target=80))
    assert all("2017 [note 3]" in c.text for c in groups if c.kind == "row_group")

    body = [Row("S", 1, ["Name", "Value A", "Value B", "Value C"])]
    body += [
        Row("S", number, ["Actual values", "2016", "2017", "2018"]) for number in range(2, 12)
    ]
    assert _profile("S", body, (None, None)).header == body[0].cells
    numeric = [Row("S", number, ["2016", "2017", "2018"]) for number in range(1, 12)]
    assert not _profile("S", numeric, (None, None)).header
    repeated = [Row("S", number, ["Actual values", "2025", "2025"]) for number in range(1, 12)]
    assert not _profile("S", repeated, (None, None)).header
    assert not _profile("S", body[1:], (None, None)).header


def test_stacked_tables_reset_units_without_swallowing_notes_or_numeric_rows():
    rows = [Row("S", 1, ["Capacity (MW)", "2024", "2025"])]
    rows += [Row("S", number, ["Wind", "100", "110"]) for number in range(2, 14)]
    rows += [
        Row("S", 15, ["Generation (GWh)", "2024", "2025"]),
        Row("S", 16, ["Solar", "5.24", "6.85"]),
        Row("S", 18, ["Numeric after a gap", "23", "29"]),
        Row("S", 20, ["Note", "Still text", "Still text"]),
        Row("S", 21, ["Continuation", "Still text", "Still text"]),
        Row("S", 23, ["Unconfirmed header", "A", "B"]),
    ]
    report = SheetDiagnostics()
    groups = [
        c
        for c in iter_sheet_chunks("unused", rows=rows, diagnostics=report, target=50)
        if c.kind == "row_group"
    ]
    solar = next(c.text for c in groups if "Solar | 5.24 | 6.85" in c.text)
    assert solar.startswith("Generation (GWh)") and "Capacity (MW)" not in solar
    combined = "\n".join(c.text for c in groups)
    assert all(" | ".join(row.cells) in combined for row in rows[14:])
    assert sum("table_header_resets=1 " in note for note in report.notes) == 1


def test_failed_header_replay_keeps_group_bounds_and_resets_between_sheets():
    rows = [Row("First", 1, ["Label", "Measure", "Amount"])]
    rows += [Row("First", n, ["Wind", "100", "110"]) for n in range(2, 14)]
    rows += [
        Row("First", 15, ["Note", "Still text", "Still text"]),
        Row("First", 17, ["Continuation", "Still text", "Still text"]),
        Row("First", 18, ["Solar", "5", "6"]),
        Row("First", 20, ["Trailing", "Unconfirmed", "Header"]),
        Row("Second", 1, ["Category", "Description"]),
        Row("Second", 30, ["Apple", "Fruit"]),
        Row("Second", 31, ["Pear", "Fruit"]),
        Row("Second", 32, ["Carrot", "Vegetable"]),
        Row("Third", 1, ["$", "/"]),
        Row("Third", 2, ["10", "20"]),
        Row("Third", 3, ["30", "40"]),
    ]
    report = SheetDiagnostics()
    groups = [
        c
        for c in iter_sheet_chunks("unused", rows=rows, diagnostics=report, target=50)
        if c.kind == "row_group"
    ]
    assert [(c.sheet, c.first_row, c.last_row, c.rows) for c in groups] == [
        *[("First", n, n + 1, 2) for n in range(2, 14, 2)],
        ("First", 15, 15, 1),
        ("First", 18, 20, 2),
        ("Second", 30, 32, 3),
        ("Third", 2, 3, 2),
    ]
    assert groups[6].text == "Label | Measure | Amount\nNote | Still text | Still text"
    assert groups[7].text == (
        "Continuation | Still text | Still text\nSolar | 5 | 6\nTrailing | Unconfirmed | Header"
    )
    assert groups[8].text == (
        "Category | Description\nApple | Fruit\nPear | Fruit\nCarrot | Vegetable"
    )
    assert groups[9].text == "$ | /\n10 | 20\n30 | 40"
    assert sum("table_header_resets=1 " in note for note in report.notes) == 1
    assert sum("chunks_without_letters=1:" in note for note in report.notes) == 1


def test_ambiguous_multirow_headers_keep_labels_without_inventing_associations():
    rows = [
        Row("S", 4, ["Industry", "Amounts"]),
        Row("S", 5, ["", "outstanding"]),
        Row("S", 6, ["", "£ billions"]),
        Row("S", 9, ["Agriculture", "ZKR7", "15.588"]),
        Row("S", 10, ["Production", "ZKS2", "0.915"]),
        Row("S", 11, ["Mining", "ZKS5", "10.068"]),
    ]
    report = SheetDiagnostics()
    chunks = list(iter_sheet_chunks("unused", rows=rows, diagnostics=report))
    summary, group = chunks
    assert "Layout confidence: uncertain" in summary.text
    assert "Amounts [text]" not in summary.text
    assert "Column 3 [number]" in summary.text
    assert all(label in group.text for label in ["Amounts", "outstanding", "£ billions"])
    assert "Agriculture | ZKR7 | 15.588" in group.text
    assert any("sheets_with_unresolved_header=1" in note for note in report.notes)
