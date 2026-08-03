"""A document with a figure must say so.

experiment 033 (adjudication) adjudicated five extractors against the
corpus oracle and found diceo is the **only** one that gives a caller no signal that a
document contains an image. Docling emits a positional marker for 100% of figures in
every format; MarkItDown does for html/docx/pptx. We emitted zero everywhere except
markdown -- where it is accidental, the raw ``![](...)`` line passing through as a
paragraph -- while reporting ``lost_data=False`` on a report with seven charts holding
thirteen facts reachable only by OCR.

Rule 3 says a loss is counted, never silent. Reading pixels is out of scope -- diceo
does not OCR -- but saying that a figure is there is not optional: the diagnostics are
how a caller finds the documents that need an OCR pass at all.

    uv run pytest tests/test_figures_reported.py -q
"""

from __future__ import annotations

import io
import zipfile

import diceo
from diceo.types import Diagnostics

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PNG = (  # a real 1x1 PNG, so nothing has to be faked
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00"
    b"\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _images_noted(report: Diagnostics) -> bool:
    return any("image" in note or "media" in note for note in report.notes)


def _docx_with_images(count: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{_W}"><w:body>'
            "<w:p><w:r><w:t>Figure 1 shows the quarterly trend.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        )
        for index in range(count):
            archive.writestr(f"word/media/image{index + 1}.png", _PNG)
    return buffer.getvalue()


def _xlsx_with_image() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{_S}" xmlns:r="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
            'openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<?xml version="1.0"?><worksheet xmlns="{_S}"><sheetData>'
            '<row r="1"><c r="A1" t="inlineStr"><is><t>Revenue</t></is></c></row>'
            "</sheetData></worksheet>",
        )
        archive.writestr("xl/media/image1.png", _PNG)
    return buffer.getvalue()


def test_a_docx_with_charts_reports_them():
    report = Diagnostics()
    list(diceo.chunk(_docx_with_images(7), diagnostics=report))
    assert _images_noted(report), f"seven charts went unmentioned: {report.notes}"


def test_a_docx_without_images_says_nothing():
    report = Diagnostics()
    list(diceo.chunk(_docx_with_images(0), diagnostics=report))
    assert not _images_noted(report), report.notes


def test_a_workbook_with_a_chart_reports_it():
    report = Diagnostics()
    list(diceo.chunk(_xlsx_with_image(), diagnostics=report))
    assert _images_noted(report), f"chart went unmentioned: {report.notes}"


def test_html_alt_text_is_indexed():
    """Alt text is authored, human-written content about the figure -- often the only
    machine-readable form of a chart's message. MarkItDown keeps it; we dropped it."""
    html = (
        b"<html><body><h1>Quarterly review</h1>"
        b'<img src="chart.png" alt="Revenue by quarter: Q1 118.4M, Q2 131.9M">'
        b"<p>See the chart above.</p></body></html>"
    )
    text = "\n".join(block.text for block in diceo.extract(html))
    assert "Revenue by quarter" in text, f"alt text dropped: {text!r}"
    assert "131.9M" in text


def test_html_image_without_alt_is_still_reported():
    report = Diagnostics()
    list(
        diceo.chunk(b'<html><body><p>x</p><img src="c.png"></body></html>', diagnostics=report)
    )
    assert _images_noted(report), report.notes


# --------------------------------------------------------------------------- #
# a diagnostic nobody can read is not a diagnostic
# --------------------------------------------------------------------------- #


def test_hidden_sheets_reach_the_public_diagnostics(tmp_path):
    """`SheetDiagnostics.sheets_hidden` was added and then never copied out by
    `api._sheet_chunks`, so through `diceo.chunk()` a workbook with five hidden
    sheets -- 20.8% of its extracted characters on the real UK fire statistics file --
    reported nothing at all. A diagnostic the public API drops on the floor is worse
    than no diagnostic, because `docs/` claimed it worked.
    """
    import zipfile

    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "xl/workbook.xml",
            f'<?xml version="1.0"?><workbook xmlns="{main}" xmlns:r="{rel}"><sheets>'
            '<sheet name="Published" sheetId="1" r:id="rId1"/>'
            '<sheet name="QA checks" sheetId="2" state="hidden" r:id="rId2"/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
            f'package/2006/relationships"><Relationship Id="rId1" Type="{rel}/worksheet" '
            f'Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="{rel}/worksheet" '
            'Target="worksheets/sheet2.xml"/></Relationships>',
        )
        for index, value in ((1, "published figure"), (2, "internal scratch value")):
            archive.writestr(
                f"xl/worksheets/sheet{index}.xml",
                f'<?xml version="1.0"?><worksheet xmlns="{main}"><sheetData>'
                f'<row r="1"><c r="A1" t="inlineStr"><is><t>{value}</t></is></c></row>'
                "</sheetData></worksheet>",
            )

    report = Diagnostics()
    text = "\n".join(chunk.text for chunk in diceo.chunk(buffer.getvalue(), diagnostics=report))
    # Still read -- rule 3 forbids dropping it. But now the caller can tell.
    assert "internal scratch value" in text
    assert any("QA checks" in note and "hidden" in note for note in report.notes), report.notes
