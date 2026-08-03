"""Locale date builtins reached the index as raw serials; the rest of the date path did not.

The finding under test was "a date cell reads `45292` instead of `2024-01-01`,
because the `numFmt` in `styles.xml` is never applied". Run against the reader, the
general claim does **not** reproduce -- `_date_styles` has read `styles.xml` since
experiment 029, and the western builtins and custom date codes render:

    numFmtId=  14 -> '2024-01-01 12:00:00'
    numFmtId=  22 -> '2024-01-01 12:00:00'
    numFmtId= 164 -> '2024-01-01 12:00:00'   (custom, formatCode="yyyy-mm")

What does reproduce is the East Asian half of the builtin table, ECMA-376 §18.8.30
ids **27-36 and 50-58** -- Japanese, Chinese, Korean and Thai date and era formats
that Excel references by id and does not write a `<numFmt>` element for:

    numFmtId=  27 -> '45292.5'      (yyyy"年"m"月")
    numFmtId=  31 -> '45292.5'      (yyyy"年"m"月"d"日")
    numFmtId=  50 -> '45292.5'
    numFmtId=  58 -> '45292.5'

A bare serial is unfindable by any query a human writes, and the workbooks that hit
this are exactly the ones the corpus has none of -- the same blind spot experiment
029 recorded when it found the 1904-calendar bug.

**Only the date half is implemented.** Currency, accounting, percent, fraction and
scientific formats still index the stored double, deliberately: the full ECMA-376
format grammar is a different piece of work, and unlike a date serial the stored
number is at least the right number. Those are pinned below so the scope is a
decision rather than an omission.

One deliberate divergence is created here: the experimental byte scanner excludes
27-36 and 50-58 to stay cell-for-cell with calamine, and a gate in the research
repository holds it to that agreement. The shipped reader is not part of that gate
(it already diverges on builtin 46) and answers to rule 3 instead.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from diceo.sheets import _BUILTIN_DATE_FORMATS, SheetDiagnostics, iter_rows

from .fixtures import _CONTENT_TYPES

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: 2024-01-01 12:00 in the 1900 calendar. Chosen with a fractional part so a
#: date-shaped render is unmistakable against the serial.
_SERIAL = "45292.5"
_RENDERED = "2024-01-01 12:00:00"


def _styled_book(format_ids: list[int], custom: str = "") -> bytes:
    """One row, one cell per style, every cell holding the same serial."""
    xfs = "".join(
        f'<xf numFmtId="{numfmt}" fontId="0" fillId="0" borderId="0"/>' for numfmt in format_ids
    )
    cells = "".join(
        f'<c r="{chr(65 + index)}1" s="{index}"><v>{_SERIAL}</v></c>'
        for index in range(len(format_ids))
    )
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
        'Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{_REL}/styles" Target="styles.xml"/>'
        "</Relationships>",
        "xl/styles.xml": f'<?xml version="1.0"?><styleSheet xmlns="{_NS}">'
        f"<numFmts>{custom}</numFmts>"
        f'<cellXfs count="{len(format_ids)}">{xfs}</cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="{_NS}">'
        f'<sheetData><row r="1">{cells}</row></sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _values(path: Path, format_ids: list[int], report: SheetDiagnostics | None = None):
    """``{numFmtId: rendered cell}``."""
    rows = list(iter_rows(path, diagnostics=report or SheetDiagnostics()))
    return dict(zip(format_ids, rows[0].cells, strict=True))


# --------------------------------------------------------------------------- #
# the gap: locale builtin date ids
# --------------------------------------------------------------------------- #


def test_east_asian_builtin_date_formats_render_as_dates(tmp_path):
    """Observed before the fix: every one of these came back `'45292.5'`."""
    ids = [27, 28, 29, 30, 31, 32, 33, 34, 35, 36]
    path = tmp_path / "ja.xlsx"
    path.write_bytes(_styled_book(ids))

    values = _values(path, ids)

    assert set(values.values()) == {_RENDERED}, values


def test_the_second_locale_builtin_block_renders_as_dates(tmp_path):
    ids = [50, 51, 52, 53, 54, 55, 56, 57, 58]
    path = tmp_path / "ja2.xlsx"
    path.write_bytes(_styled_book(ids))

    values = _values(path, ids)

    assert set(values.values()) == {_RENDERED}, values


def test_locale_date_cells_are_counted(tmp_path):
    """`date_cells` is what tells a caller the date path ran at all."""
    ids = [27, 31, 50, 58]
    path = tmp_path / "counted.xlsx"
    path.write_bytes(_styled_book(ids))

    report = SheetDiagnostics()
    _values(path, ids, report)

    assert report.date_cells == 4, report


def test_the_builtin_date_set_is_exactly_the_documented_one():
    """A frozenset is easy to widen by accident. Both blocks in, and the ids that
    are *not* dates -- currency 37-44, scientific 48, text 49, and the reserved
    23-26 -- explicitly out."""
    assert set(range(14, 23)) | {45, 46, 47} <= _BUILTIN_DATE_FORMATS
    assert set(range(27, 37)) | set(range(50, 59)) <= _BUILTIN_DATE_FORMATS
    assert not _BUILTIN_DATE_FORMATS & (set(range(23, 27)) | set(range(37, 45)) | {48, 49})


# --------------------------------------------------------------------------- #
# what already worked, pinned because the finding claimed it did not
# --------------------------------------------------------------------------- #


def test_western_builtin_date_formats_were_already_applied(tmp_path):
    ids = [14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47]
    path = tmp_path / "western.xlsx"
    path.write_bytes(_styled_book(ids))

    values = _values(path, ids)

    assert set(values.values()) == {_RENDERED}, values


def test_a_custom_format_code_with_date_tokens_was_already_applied(tmp_path):
    """`_is_date_code` reads the format code, so a workbook that spells its date
    format out has never needed the builtin table."""
    ids = [164]
    path = tmp_path / "custom.xlsx"
    path.write_bytes(
        _styled_book(ids, custom='<numFmt numFmtId="164" formatCode="yyyy-mm-dd"/>')
    )

    assert _values(path, ids) == {164: _RENDERED}


def test_a_custom_format_that_only_looks_like_a_date_is_not_one(tmp_path):
    """The quoted-literal trap: `"day "0` is a number with the word day in front of
    it, and rendering it as a date would invent one."""
    ids = [165]
    path = tmp_path / "quoted.xlsx"
    path.write_bytes(
        _styled_book(ids, custom='<numFmt numFmtId="165" formatCode="&quot;day &quot;0"/>')
    )

    assert _values(path, ids) == {165: _SERIAL}


def test_a_custom_code_on_a_locale_builtin_id_still_wins(tmp_path):
    """A `<numFmt>` element overrides the builtin meaning of its id. Widening the
    builtin set must not take that away, or a workbook that reuses id 30 for a plain
    number gets a fabricated date."""
    ids = [30]
    path = tmp_path / "override.xlsx"
    path.write_bytes(_styled_book(ids, custom='<numFmt numFmtId="30" formatCode="0.00"/>'))

    assert _values(path, ids) == {30: _SERIAL}


# --------------------------------------------------------------------------- #
# out of scope, on purpose
# --------------------------------------------------------------------------- #


def test_currency_and_percent_still_index_the_stored_number(tmp_path):
    """Deliberate scope line. Unlike a date serial, the stored double is the right
    number and is findable -- `1234.5` matches a query for 1234.5, where `45292`
    matches nothing a human would type. Implementing the full ECMA-376 format
    grammar is a separate piece of work; this pins that it has not happened."""
    ids = [9, 10, 37, 38, 39, 40, 41, 42, 43, 44, 48]
    path = tmp_path / "money.xlsx"
    path.write_bytes(_styled_book(ids))

    values = _values(path, ids)

    assert set(values.values()) == {_SERIAL}, values


def test_an_unstyled_number_is_untouched(tmp_path):
    ids = [0]
    path = tmp_path / "plain.xlsx"
    path.write_bytes(_styled_book(ids))

    assert _values(path, ids) == {0: _SERIAL}
