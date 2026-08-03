"""ISO 29500 Strict workbooks came back empty, and said nothing at all.

Excel has a "Strict Open XML Spreadsheet" save option. It is valid OOXML — the same
elements, under `http://purl.oclc.org/ooxml/spreadsheetml/main` instead of
`http://schemas.openxmlformats.org/spreadsheetml/2006/main` — and some public-sector
archives require it. Every tag name in the reader was built from the Transitional
namespace, so nothing matched: measured, a three-row workbook produced **zero chunks,
`lost_data=False`, no note, no truncation and no exception**. A document that is
silently not in the index is the worst failure this project has, and this was a whole
class of them.

The fix reads the namespace off each part's **root element** rather than matching two
namespaces in the hot loop, so it costs nothing per row and works for any spelling —
including whatever ECMA does next.
"""

from __future__ import annotations

import io
import zipfile

import diceo
from diceo.sheets import SheetDiagnostics, iter_sheet_chunks

from .fixtures import _CONTENT_TYPES, tiny_xlsx

STRICT = "http://purl.oclc.org/ooxml/spreadsheetml/main"
STRICT_REL = "http://purl.oclc.org/ooxml/officeDocument/relationships"
TRANSITIONAL = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _strict_xlsx(*, shared: bool = False) -> bytes:
    """A Strict workbook. `shared` puts the text in `sharedStrings.xml` instead of
    inline, because that part has its own root element and its own namespace."""
    rows = [["region", "revenue"], ["EMEA", "1200"], ["APAC", "890"]]
    parts: dict[str, str] = {}
    body = []
    if shared:
        table = [value for row in rows for value in row]
        index = {value: i for i, value in enumerate(table)}
        for number, cells in enumerate(rows, start=1):
            cols = "".join(
                f'<c r="{chr(65 + i)}{number}" t="s"><v>{index[value]}</v></c>'
                for i, value in enumerate(cells)
            )
            body.append(f'<row r="{number}">{cols}</row>')
        items = "".join(f"<si><t>{value}</t></si>" for value in table)
        parts["xl/sharedStrings.xml"] = (
            f'<?xml version="1.0"?><sst xmlns="{STRICT}" count="{len(table)}" '
            f'uniqueCount="{len(table)}">{items}</sst>'
        )
    else:
        for number, cells in enumerate(rows, start=1):
            cols = "".join(
                f'<c r="{chr(65 + i)}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
                for i, value in enumerate(cells)
            )
            body.append(f'<row r="{number}">{cols}</row>')

    shared_rel = (
        f'<Relationship Id="rId2" Type="{STRICT_REL}/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        if shared
        else ""
    )
    parts.update(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            f'Type="{STRICT_REL}/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
            "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{STRICT}" '
            f'xmlns:r="{STRICT_REL}"><sheets>'
            '<sheet name="Prices" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{STRICT_REL}/worksheet" '
            f'Target="worksheets/sheet1.xml"/>{shared_rel}</Relationships>',
            "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet '
            f'xmlns="{STRICT}"><dimension ref="A1:B3"/>'
            f"<sheetData>{''.join(body)}</sheetData></worksheet>",
        }
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_a_strict_workbook_is_read(tmp_path):
    path = tmp_path / "strict.xlsx"
    path.write_bytes(_strict_xlsx())

    text = "\n".join(piece.text for piece in diceo.chunk(path))

    assert "EMEA" in text, text
    assert "1200" in text
    assert "APAC" in text


def test_a_strict_workbook_with_shared_strings_is_read(tmp_path):
    """`sharedStrings.xml` is a separate part with its own root, so its namespace has
    to be sniffed separately -- and a workbook whose cells are all `t="s"` would
    otherwise come out as a grid of empty cells rather than as nothing, which is
    harder to notice."""
    path = tmp_path / "strict-shared.xlsx"
    path.write_bytes(_strict_xlsx(shared=True))

    text = "\n".join(piece.text for piece in diceo.chunk(path))

    assert "EMEA" in text, text
    assert "890" in text


def test_the_sheet_name_survives(tmp_path):
    path = tmp_path / "strict.xlsx"
    path.write_bytes(_strict_xlsx())

    text = "\n".join(piece.text for piece in diceo.chunk(path))

    assert "Prices" in text, text


def test_rows_are_counted(tmp_path):
    path = tmp_path / "strict.xlsx"
    path.write_bytes(_strict_xlsx())

    report = SheetDiagnostics()
    list(iter_sheet_chunks(path, diagnostics=report))

    assert report.rows == 3
    assert report.sheets == 1


def test_a_transitional_workbook_is_unaffected(tmp_path):
    path = tmp_path / "normal.xlsx"
    path.write_bytes(tiny_xlsx())

    text = "\n".join(piece.text for piece in diceo.chunk(path))

    assert "EMEA" in text


def test_a_sheet_that_cannot_be_reached_is_a_reported_loss(tmp_path):
    """The pre-existing guard, pinned here because it is the one that would have made
    the Strict failure visible and did not fire for it.

    A `<sheet>` with no relationship id cannot be mapped to a part, and that path
    *does* report: `truncated=['unreadable_sheet=S']`, `lost_data=True`. The Strict
    workbook slipped past precisely because nothing was unreachable — the part was
    found and opened, and every element inside it simply failed to match, which no
    existing counter could see. Namespace sniffing removes the cause; this test
    records the shape of the guard that was not enough."""
    path = tmp_path / "unreadable.xlsx"
    rows = "".join(f'<row r="{n}"><c r="A{n}"><v>{n}</v></c></row>' for n in (1, 2))
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>',
        # No `r:id` on the sheet, so it cannot be mapped to a part.
        "xl/workbook.xml": '<?xml version="1.0"?><workbook xmlns="urn:invented">'
        '<sheets><sheet name="S" sheetId="1"/></sheets></workbook>',
        "xl/worksheets/sheet1.xml": '<?xml version="1.0"?>'
        f'<worksheet xmlns="urn:invented"><sheetData>{rows}</sheetData></worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    path.write_bytes(buffer.getvalue())

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert report.lost_data, report.as_dict()
