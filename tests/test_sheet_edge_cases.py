"""Spreadsheet cases that reach a search index looking plausible and being wrong.

Written after surveying the issue trackers of openpyxl, calamine, python-calamine,
xlrd, pandas ``read_excel``, odfpy and SheetJS for what actually goes wrong in
production (experiment 029, bug-tracker audit).

An ``.xlsx`` is a ZIP of XML, so every fixture here is built with ``zipfile`` and
hand-written strings -- no binary fixture, no network, runs on a fresh clone.

    uv run pytest tests/test_sheet_edge_cases.py -q
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from diceo.sheets import SheetDiagnostics, iter_rows

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

_CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
    '2006/content-types"><Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _workbook(sheets: str, properties: str = "") -> str:
    return (
        f'<?xml version="1.0"?><workbook xmlns="{_MAIN}" xmlns:r="{_REL}">'
        f"{properties}<sheets>{sheets}</sheets></workbook>"
    )


def _relationships(*items: str) -> str:
    return (
        f'<?xml version="1.0"?><Relationships xmlns="{_PKG_REL}">'
        + "".join(items)
        + "</Relationships>"
    )


def _relationship(rid: str, kind: str, target: str) -> str:
    return f'<Relationship Id="{rid}" Type="{_REL}/{kind}" Target="{target}"/>'


def _sheet(rows: str) -> str:
    return (
        f'<?xml version="1.0"?><worksheet xmlns="{_MAIN}">'
        f"<sheetData>{rows}</sheetData></worksheet>"
    )


def _shared_strings(*entries: str) -> str:
    return f'<?xml version="1.0"?><sst xmlns="{_MAIN}">' + "".join(entries) + "</sst>"


def _write(tmp_path: Path, parts: dict[str, str], name: str = "book.xlsx") -> Path:
    path = tmp_path / name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for part_name, data in parts.items():
            archive.writestr(part_name, data)
    path.write_bytes(buffer.getvalue())
    return path


def _one_sheet_book(
    tmp_path: Path,
    rows: str,
    *,
    strings: str | None = None,
    styles: str | None = None,
    properties: str = "",
    name: str = "book.xlsx",
) -> Path:
    """A minimal single-sheet workbook. Only the parts a case needs are written."""
    items = [_relationship("rId1", "worksheet", "worksheets/sheet1.xml")]
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "xl/workbook.xml": _workbook(
            '<sheet name="Sheet1" sheetId="1" r:id="rId1"/>', properties
        ),
        "xl/worksheets/sheet1.xml": _sheet(rows),
    }
    if strings is not None:
        items.append(_relationship("rId2", "sharedStrings", "sharedStrings.xml"))
        parts["xl/sharedStrings.xml"] = strings
    if styles is not None:
        items.append(_relationship("rId3", "styles", "styles.xml"))
        parts["xl/styles.xml"] = styles
    parts["xl/_rels/workbook.xml.rels"] = _relationships(*items)
    return _write(tmp_path, parts, name)


def _rows(path: Path) -> tuple[list[list[str]], SheetDiagnostics]:
    report = SheetDiagnostics()
    return [row.cells for row in iter_rows(path, diagnostics=report)], report


# --------------------------------------------------------------------------- #
# phonetic (ruby) hints: the same defect shape as OOXML's AlternateContent
# --------------------------------------------------------------------------- #


def test_japanese_phonetic_hints_do_not_glue_onto_the_word(tmp_path: Path):
    """A shared string may carry a ``<rPh>`` reading hint. Walking *all* descendant
    ``<t>`` elements concatenates the hint onto the word: 東京 becomes 東京トウキョウ,
    which matches no query for either the name or its reading.

    This is the identical fork the OOXML survey found for ``mc:AlternateContent``
    (walk everything and duplicate, walk too narrowly and lose) -- a second format,
    same root cause. calamine shipped this bug and fixed it in 0.16.2.
    """
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        strings=_shared_strings(
            '<si><t>東京</t><rPh sb="0" eb="2"><t>トウキョウ</t></rPh></si>'
        ),
    )
    rows, _ = _rows(path)
    assert rows == [["東京"]]


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("<si><t>plain</t></si>", "plain"),
        ("<si><r><t>one </t></r><r><t>two</t></r></si>", "one two"),
        # A bare <t> followed by runs: what a user gets by bolding the second half of
        # a cell. Walking only r/t drops the lead -- calamine 636.
        ("<si><t>lead </t><r><t>bold</t></r></si>", "lead bold"),
        ("<si><r><rPr><b/></rPr><t>bold</t></r><t> tail</t></si>", "bold tail"),
        # Runs each carry their own phonetic hint in some Japanese workbooks.
        ("<si><r><t>東</t></r><rPh><t>ヒガシ</t></rPh><r><t>京</t></r></si>", "東京"),
        ('<si><t>x</t><phoneticPr fontId="1"/></si>', "x"),
    ],
)
def test_a_shared_string_is_its_text_and_nothing_else(
    tmp_path: Path, entry: str, expected: str
):
    """The guard on the fix above: narrowing the walk must not start losing runs."""
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        strings=_shared_strings(entry),
    )
    rows, _ = _rows(path)
    assert rows == [[expected]]


# --------------------------------------------------------------------------- #
# _xHHHH_ escapes: OOXML's way of carrying characters XML cannot hold
# --------------------------------------------------------------------------- #


def test_an_in_cell_newline_is_a_newline_not_the_literal_escape(tmp_path: Path):
    """A character XML forbids is written as ``_xHHHH_``. Left undecoded, a two-line
    cell indexes as the single run-together token ``line1_x000D_line2`` -- neither
    line matches, and a hex marker enters the vocabulary. calamine fixed this in
    0.31.0; openpyxl's issue is still open.
    """
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        strings=_shared_strings("<si><t>line1_x000D_line2</t></si>"),
    )
    rows, _ = _rows(path)
    assert rows == [["line1\rline2"]]


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("line1_x000D_line2", "line1\rline2"),
        ("a_x000A_b", "a\nb"),
        ("tab_x0009_here", "tab\there"),
        # A cell whose real text IS the escape is stored with the underscore itself
        # escaped. Decoding twice, or right-to-left, makes the two indistinguishable.
        ("_x005F_x000D_", "_x000D_"),
        # Not escapes: the pattern must not eat ordinary text.
        ("_x00_", "_x00_"),
        ("_xZZZZ_", "_xZZZZ_"),
        ("snake_case_name", "snake_case_name"),
        ("width_x_height", "width_x_height"),
    ],
)
def test_escapes_are_decoded_and_ordinary_underscores_are_left_alone(
    tmp_path: Path, stored: str, expected: str
):
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" t="s"><v>0</v></c></row>',
        strings=_shared_strings(f"<si><t>{stored}</t></si>"),
    )
    rows, _ = _rows(path)
    assert rows == [[expected]]


def test_escapes_are_decoded_in_inline_strings_too(tmp_path: Path):
    """A sheet written without a shared-string table takes a different code path."""
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" t="inlineStr"><is><t>line1_x000D_line2</t></is></c></row>',
    )
    rows, _ = _rows(path)
    assert rows == [["line1\rline2"]]


# --------------------------------------------------------------------------- #
# the 1904 date system
# --------------------------------------------------------------------------- #

_DATE_STYLES = (
    f'<?xml version="1.0"?><styleSheet xmlns="{_MAIN}">'
    '<cellXfs count="1"><xf numFmtId="14" applyNumberFormat="1"/></cellXfs></styleSheet>'
)


@pytest.mark.parametrize(
    ("properties", "expected"),
    [
        ("", "2024-03-15"),
        ('<workbookPr date1904="1"/>', "2028-03-16"),
        ('<workbookPr date1904="true"/>', "2028-03-16"),
        ('<workbookPr date1904="0"/>', "2024-03-15"),
        ('<workbookPr date1904="false"/>', "2024-03-15"),
        # An unrelated attribute on the same element must not switch the calendar.
        ('<workbookPr showObjects="all"/>', "2024-03-15"),
    ],
)
def test_a_mac_authored_workbook_dates_are_not_four_years_out(
    tmp_path: Path, properties: str, expected: str
):
    """Excel for Mac counts days from 1904-01-01, and says so in ``workbookPr``.

    Ignoring that flag shifts **every date in the workbook by 1462 days** -- four
    years and a day -- while still producing a perfectly well-formed date, so
    nothing downstream can tell. openpyxl shipped this for years ("we were obviously
    missing a test"); calamine gave callers no way to even ask until 2026.

    The 1904 calendar also has no phantom leap day, which is why the offset is 1462
    above serial 60 and 1461 below it -- see the serial-60 test.
    """
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" s="0"><v>45366</v></c></row>',
        styles=_DATE_STYLES,
        properties=properties,
    )
    rows, report = _rows(path)
    assert rows == [[expected]]
    assert report.date_cells == 1


def test_serial_60_is_the_phantom_leap_day_and_1904_has_no_such_thing(tmp_path: Path):
    """Excel's 1900 calendar contains a 1900-02-29 that never existed, so the offset
    to the 1904 calendar is 1461 days below serial 60 and 1462 above it. Getting this
    wrong shifts only *old* dates, which is exactly the kind of bug a test on one
    modern date cannot see."""
    rows_xml = "".join(
        f'<row r="{i}"><c r="A{i}" s="0"><v>{serial}</v></c></row>'
        for i, serial in enumerate((59, 61), start=1)
    )
    plain, _ = _rows(_one_sheet_book(tmp_path, rows_xml, styles=_DATE_STYLES))
    mac, _ = _rows(
        _one_sheet_book(
            tmp_path,
            rows_xml,
            styles=_DATE_STYLES,
            properties='<workbookPr date1904="1"/>',
            name="mac.xlsx",
        )
    )
    assert plain == [["1900-02-28"], ["1900-03-01"]]
    assert mac == [["1904-02-29"], ["1904-03-02"]]


# --------------------------------------------------------------------------- #
# hidden sheets: reported, never dropped
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("state", ["hidden", "veryHidden"])
def test_a_hidden_sheet_is_read_but_reported(tmp_path: Path, state: str):
    """A hidden sheet is real content, so rule 3 forbids dropping it -- but a caller
    filtering by ACL or freshness needs to know. "Very hidden" cannot be un-hidden
    from Excel's UI at all (it is set from the VBA editor), so it is disproportionately
    stale scratch data: old client lists, internal lookup tables. openpyxl and pandas
    both return these sheets with nothing marking them.
    """
    items = (
        _relationship("rId1", "worksheet", "worksheets/sheet1.xml"),
        _relationship("rId2", "worksheet", "worksheets/sheet2.xml"),
    )
    path = _write(
        tmp_path,
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "xl/workbook.xml": _workbook(
                '<sheet name="Public" sheetId="1" r:id="rId1"/>'
                f'<sheet name="Scratch" sheetId="2" state="{state}" r:id="rId2"/>'
            ),
            "xl/_rels/workbook.xml.rels": _relationships(*items),
            "xl/worksheets/sheet1.xml": _sheet(
                '<row r="1"><c r="A1" t="inlineStr"><is><t>published figure</t></is></c></row>'
            ),
            "xl/worksheets/sheet2.xml": _sheet(
                '<row r="1"><c r="A1" t="inlineStr"><is><t>old client list</t></is></c></row>'
            ),
        },
    )
    rows, report = _rows(path)
    assert rows == [["published figure"], ["old client list"]]
    assert report.sheets_hidden == [("Scratch", state)]


def test_visible_sheets_are_not_reported_as_hidden(tmp_path: Path):
    path = _one_sheet_book(tmp_path, '<row r="1"><c r="A1"><v>1</v></c></row>')
    _, report = _rows(path)
    assert report.sheets_hidden == []


# --------------------------------------------------------------------------- #
# cells we cannot read: counted, never just blank
# --------------------------------------------------------------------------- #


def test_a_formula_with_no_cached_value_is_counted(tmp_path: Path):
    """Any workbook last written by a library rather than by Excel has **no cached
    formula results at all** -- openpyxl drops them on write, by design ("either the
    values or the formulae, never both"). Every formula cell then reads as empty.

    There is nothing to recover without evaluating the formula, so this is a real and
    unavoidable loss. Rule 3 says it must therefore be *counted*: a caller seeing
    "412 of 900 cells were unevaluated formulas" knows the document is a template,
    not data. Silently returning blanks tells them nothing.
    """
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1"><v>7</v></c>'
        '<c r="B1"><f>A1*2</f><v>14</v></c>'  # cached: readable
        '<c r="C1"><f>A1*3</f></c>'  # no cached value: unreachable
        '<c r="D1"><v>9</v></c></row>',
    )
    rows, report = _rows(path)
    # The unreadable cell keeps its column, so D1 stays in column 4 rather than
    # sliding left -- the misalignment that turns every later value into a lie.
    assert rows == [["7", "14", "", "9"]]
    assert report.formula_cells_unevaluated == 1


def test_a_shared_string_index_that_does_not_exist_is_counted(tmp_path: Path):
    """An out-of-range index, or an empty ``<v/>``, means a cell's text is
    unreachable. calamine resolved the empty case to index 0 and stamped the
    workbook's *first string* into every such cell -- confidently wrong text, whole
    columns of it. Returning empty is right; returning empty silently is not.
    """
    path = _one_sheet_book(
        tmp_path,
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>99</v></c>'
        '<c r="C1" t="s"><v/></c>'
        '<c r="D1" t="s"><v>0</v></c></row>',
        strings=_shared_strings("<si><t>real</t></si>"),
    )
    rows, report = _rows(path)
    assert rows == [["real", "", "", "real"]]
    assert report.shared_string_misses == 2
