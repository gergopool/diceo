"""Tiny but *real* documents, built from the standard library only.

Every other test module in this suite skips itself when ``data/corpus`` has not
been generated. That is fine for the measurement-heavy tests, and wrong for the
robustness contract: the gate that says "diceo never raises anything but a
DiceoError" has to run for a contributor who has just cloned the repository and
typed ``pytest``. A gate that skips by default is not a gate.

So these builders emit genuine files -- a PDF with a real xref table whose offsets
are computed, OOXML packages with the parts and relationships the readers actually
look for, an ODF package whose ``mimetype`` member is stored first and uncompressed
as the spec requires. They are small enough to build thousands of times in a fuzz
loop and complete enough that our readers cannot tell them from Word's output.
"""

from __future__ import annotations

import email.message
import email.policy
import zipfile
from pathlib import Path

# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def tiny_pdf(lines: list[str] | None = None, *, pages: int = 1) -> bytes:
    """A valid one-or-more-page PDF with a real text layer.

    The xref offsets are computed rather than faked, because PDFium validates them
    and a fixture that only *looks* like a PDF would make every test that depends
    on it meaningless.
    """
    lines = lines or ["Hello from diceo.", "The revenue was 1200 million."]
    objects: list[bytes] = []

    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for page in range(pages):
        content = ["BT", "/F1 12 Tf", "14 TL", "72 720 Td"]
        for line in lines:
            escaped = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content.append(f"({escaped}) Tj T*")
        content.append("ET")
        stream = "\n".join(content).encode("latin-1", "replace")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + f"{5 + 2 * page} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def matrix_scaled_pdf(items: list[tuple[float, str]] | None = None, *, pages: int = 1) -> bytes:
    """A PDF whose producer put the type scale in the *text matrix*, not in ``Tf``.

    ``/F1 1 Tf`` followed by ``20 0 0 20 ... Tm`` renders 20 pt text, and it is
    what real-world producers emit: both real PDFs this project holds
    (911-report.pdf, 585 pages, and the IPBES assessment) do exactly this. The
    consequence is that ``FPDFText_GetFontSize`` reports ``1.0`` for every
    character in the document, so a font-size census taken from it cannot tell a
    chapter title from body text.

    ``items`` is ``(rendered size, text)``. The sizes here are the ones a reader
    sees; the nominal size in the file is 1 throughout. Pass a list of lists to
    give each page different content -- which is what a report with front matter
    looks like, and the shape that defeats a calibration window.
    """
    items = items or [
        (20, "Annual Report 2026"),
        (10, "Revenue rose to 1200 million in the fourth quarter."),
        (14, "Regional Breakdown"),
        (10, "EMEA contributed 640 million of that total."),
    ]
    per_page: list[list[tuple[float, str]]]
    if items and isinstance(items[0], list):
        per_page = items  # type: ignore[assignment]
        pages = len(per_page)
    else:
        per_page = [items] * pages  # type: ignore[list-item]

    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    return _scaled_pdf([[(1.0, scale, text) for scale, text in page] for page in per_page])


def mixed_scale_pdf() -> bytes:
    """One page holding both an honest ``Tf`` size *and* matrix-scaled labels.

    This is the trap the earlier structure prototype recorded and the reason its
    fallback was gated: on ``paper-tables.pdf`` pages 13-15 the attention-heatmap
    figures are typeset at a unit font size with a scaled matrix, so their labels
    measure a glyph box of 27 against a body of 8.9. A reader that switches to
    glyph boxes because it saw *some* unit sizes promotes a figure label to h1.

    Nominal sizes must win here, and they do because the page as a whole is not
    degenerate -- which is why the probe is decided per page rather than per file.
    """
    return _scaled_pdf(
        [
            [
                (11.0, 1.0, "Attention Is All You Need"),
                (9.0, 1.0, "The dominant sequence transduction models are based on"),
                (9.0, 1.0, "complex recurrent or convolutional neural networks."),
                # A figure label: unit font size, scale in the matrix.
                (1.0, 28.0, "Input-Input Layer5 The Law will never beperfect"),
            ]
        ]
    )


def _scaled_pdf(pages_of: list[list[tuple[float, float, str]]]) -> bytes:
    """``(nominal Tf size, matrix scale, text)`` per line, per page."""
    pages = len(pages_of)
    objects: list[bytes] = []
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(pages))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for page in range(pages):
        content = ["BT"]
        y = 720.0
        for nominal, scale, text in pages_of[page]:
            content.append(f"/F1 {nominal:g} Tf")
            escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content.append(f"{scale:g} 0 0 {scale:g} 72 {y:g} Tm")
            content.append(f"({escaped}) Tj")
            # The rendered size is nominal x matrix scale, and the advance has to
            # follow it: leading the lines by the *scale* alone put four lines 1.6
            # points apart, and PDFium merged them into one.
            y -= nominal * scale * 1.6
        content.append("ET")
        stream = "\n".join(content).encode("latin-1", "replace")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents "
            + f"{5 + 2 * page} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def image_only_pdf() -> bytes:
    """A page that draws a bitmap and has no text: what a scanner produces.

    The whole point of ``Diagnostics.pages_image_only`` is telling this apart from
    a deliberately blank page, so the suite needs one of each.
    """
    # A 1x1 8-bit grey image is enough: the check asks what kind of object is on
    # the page, never what the pixels are.
    image = b"\xff"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im0 5 0 R >> >> /Contents 4 0 R >>",
        None,  # content stream, filled below
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace "
        b"/DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n" + image + b"\nendstream",
    ]
    content = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    objects[3] = f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"  # type: ignore[operator]
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def stamped_image_pdf(stamp: str = "38") -> bytes:
    """A page that is a picture, with a page number stamped on it.

    The shape that defeated the image-only check for as long as the check was
    ``if not found``: the page is entirely a bitmap -- a table drawn as an image --
    but the producer's running footer puts a folio on it, so the page yields two
    characters of text and stops looking textless.

    Found on the IPBES French assessment, where seven such pages held four named
    tables and reported ``pages_image_only=0``, ``needs_ocr=False``,
    ``lost_data=False``. Every one of those is now the opposite.
    """
    image = b"\xff"
    content = f"q 612 0 0 792 0 0 cm /Im0 Do Q BT /F1 9 Tf 300 36 Td ({stamp}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject "
        b"<< /Im0 5 0 R >> /Font << /F1 6 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace "
        b"/DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n" + image + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def blank_pdf() -> bytes:
    """A page with neither text nor images -- a separator, not a loss."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


# --------------------------------------------------------------------------- #
# OOXML
# --------------------------------------------------------------------------- #

_RELS_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    f'<Default Extension="rels" ContentType="{_RELS_TYPE}"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    "</Types>"
)

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _zip(parts: dict[str, bytes | str], *, first: tuple[str, bytes] | None = None) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if first is not None:
            # ODF requires `mimetype` to be the first member and STORED.
            info = zipfile.ZipInfo(first[0])
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, first[1])
        for name, body in parts.items():
            archive.writestr(name, body)
    return buffer.getvalue()


def tiny_docx(paragraphs: list[str] | None = None) -> bytes:
    paragraphs = paragraphs or ["Quarterly report", "The revenue was 1200 million."]
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="{"Heading1" if i == 0 else "Normal"}"/></w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
        for i, text in enumerate(paragraphs)
    )
    return _zip(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
            "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS}><w:body>'
            f"{body}</w:body></w:document>",
            "word/styles.xml": f'<?xml version="1.0"?><w:styles {_W_NS}>'
            f'<w:style w:styleId="Heading1" w:type="paragraph"><w:name w:val="heading 1"/>'
            f"</w:style></w:styles>",
        }
    )


def tiny_xlsx(rows: list[list[str]] | None = None, *, sheet: str = "Sheet1") -> bytes:
    rows = rows or [["region", "revenue"], ["EMEA", "1200"], ["APAC", "890"]]
    body = []
    for number, cells in enumerate(rows, start=1):
        cols = "".join(
            f'<c r="{chr(65 + i)}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
            for i, value in enumerate(cells)
        )
        body.append(f'<row r="{number}">{cols}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    return _zip(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="xl/workbook.xml"/></Relationships>',
            "xl/workbook.xml": f'<?xml version="1.0"?><workbook {ns} {rel_ns}><sheets>'
            f'<sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships '
            'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
            "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet {ns}>'
            f'<dimension ref="A1:B{len(rows)}"/><sheetData>{"".join(body)}</sheetData>'
            f"</worksheet>",
        }
    )


def tiny_pptx(slides: list[str] | None = None) -> bytes:
    slides = slides or ["Results", "Revenue grew to 1200 million"]
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    p = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    parts: dict[str, bytes | str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="ppt/presentation.xml"/></Relationships>',
    }
    sldids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides)))
    parts["ppt/presentation.xml"] = (
        f'<?xml version="1.0"?><p:presentation {p} {r}><p:sldIdLst>{sldids}'
        f"</p:sldIdLst></p:presentation>"
    )
    rels = "".join(
        f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
        for i in range(len(slides))
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        f'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.'
        f'org/package/2006/relationships">{rels}</Relationships>'
    )
    for i, text in enumerate(slides, start=1):
        placeholder = '<p:nvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>'
        parts[f"ppt/slides/slide{i}.xml"] = (
            f'<?xml version="1.0"?><p:sld {a} {p}><p:cSld><p:spTree><p:sp>'
            f"{placeholder}<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
            f"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
    return _zip(parts)


def tiny_ods(rows: list[list[str]] | None = None) -> bytes:
    """An OpenDocument spreadsheet, with the ``mimetype`` member stored first."""
    rows = rows or [["region", "revenue"], ["EMEA", "1200"], ["APAC", "890"]]
    office = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    table = 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
    text = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    body = "".join(
        "<table:table-row>"
        + "".join(
            f'<table:table-cell office:value-type="string">'
            f"<text:p>{value}</text:p></table:table-cell>"
            for value in cells
        )
        + "</table:table-row>"
        for cells in rows
    )
    manifest = (
        '<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:'
        'opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" '
        'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" '
        'manifest:media-type="text/xml"/></manifest:manifest>'
    )
    return _zip(
        {
            "META-INF/manifest.xml": manifest,
            "content.xml": f'<?xml version="1.0"?><office:document-content {office} '
            f"{table} {text}><office:body><office:spreadsheet>"
            f'<table:table table:name="Sheet1">{body}</table:table>'
            f"</office:spreadsheet></office:body></office:document-content>",
        },
        first=("mimetype", b"application/vnd.oasis.opendocument.spreadsheet"),
    )


# --------------------------------------------------------------------------- #
# the container-free formats
# --------------------------------------------------------------------------- #


def tiny_csv(delimiter: str = ",") -> bytes:
    rows = [["region", "revenue", "units"], ["EMEA", "1200", "34"], ["APAC", "890", "21"]]
    return "\n".join(delimiter.join(row) for row in rows).encode() + b"\n"


def tiny_html() -> bytes:
    return (
        b"<!DOCTYPE html><html><head><title>Q3</title><style>p{color:red}</style>"
        b"</head><body><h1>Quarterly results</h1><p>Revenue rose to 1200 million.</p>"
        b"<table><tr><th>Region</th><th>Rev</th></tr><tr><td>EMEA</td><td>1200</td>"
        b"</tr></table><ul><li>First</li></ul><script>var hidden=1;</script>"
        b"</body></html>"
    )


def tiny_eml(*, attachment: bool = True) -> bytes:
    message = email.message.EmailMessage()
    message["From"] = "a@example.com"
    message["To"] = "b@example.com"
    message["Subject"] = "Budget approval"
    message["Date"] = "Tue, 29 Jul 2026 10:00:00 +0200"
    message.set_content("Please approve the budget of 1200 EUR.")
    if attachment:
        message.add_attachment(
            tiny_pdf(), maintype="application", subtype="pdf", filename="budget.pdf"
        )
    return message.as_bytes()


def tiny_text() -> bytes:
    return b"# Title\n\nBody paragraph with 1200 in it.\n\n- item one\n- item two\n"


#: Every format, as ``suffix -> bytes``. The fuzz suite iterates this, so adding a
#: reader to diceo and a builder here gets it fuzzed for free.
def every_format() -> dict[str, bytes]:
    return {
        ".pdf": tiny_pdf(),
        ".docx": tiny_docx(),
        ".xlsx": tiny_xlsx(),
        ".pptx": tiny_pptx(),
        ".ods": tiny_ods(),
        ".csv": tiny_csv(),
        ".html": tiny_html(),
        ".eml": tiny_eml(),
        ".txt": tiny_text(),
    }


def write_every_format(directory: Path) -> dict[str, Path]:
    out = {}
    for suffix, payload in every_format().items():
        path = directory / f"sample{suffix}"
        path.write_bytes(payload)
        out[suffix] = path
    return out
