"""Equations are text, and dropping them leaves a sentence that reads as complete.

OMML (`m:oMath`) keeps its characters in `m:t`, a different namespace from `w:t`. The
readers matched `w:t` only, so:

* `The relation <m:oMath>E=mc²</m:oMath> is famous.` came out as
  **`The relation is famous.`** — fluent, and false. That is worse than a gap,
  because nothing downstream can tell that anything is missing.
* a paragraph holding *only* display math (`m:oMathPara`, which is how every
  numbered equation in a technical document is written) produced **no block at all**
  and **no diagnostic** — a whole authored paragraph gone with `lost_data=False`.

Flattening to characters rather than converting to LaTeX is deliberate: `E=mc2` is
what an embedding model can use, experiment 020 measured markup as worth +0.2pp
(p = 1.000), and a real OMML-to-LaTeX converter is a large dependency for no measured
gain. The count is reported so a caller who *does* need typeset maths knows the file
had some.
"""

from __future__ import annotations

import io
import zipfile

from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks, iter_pptx_blocks

from .fixtures import _CONTENT_TYPES, _W_NS

_M_NS = 'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"'


def _sup(base: str, exponent: str) -> str:
    return (
        f"<m:sSup><m:e><m:r><m:t>{base}</m:t></m:r></m:e>"
        f"<m:sup><m:r><m:t>{exponent}</m:t></m:r></m:sup></m:sSup>"
    )


def _docx(body: str) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS} {_M_NS}>'
        f"<w:body>{body}</w:body></w:document>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _texts(path) -> list[str]:
    return [b.text for b in iter_docx_blocks(path, diagnostics=OoxmlDiagnostics())]


def test_inline_math_stays_in_its_sentence(tmp_path):
    path = tmp_path / "math.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>The relation </w:t></w:r>"
            f"<m:oMath><m:r><m:t>E=mc</m:t></m:r>{_sup('c', '2')}</m:oMath>"
            "<w:r><w:t> is famous.</w:t></w:r></w:p>"
        )
    )

    texts = _texts(path)

    assert len(texts) == 1
    assert "E=mc" in texts[0], texts
    assert texts[0] != "The relation is famous.", "fluent and false is the worst outcome"


def test_a_display_math_paragraph_is_not_lost(tmp_path):
    """`m:oMathPara` with no surrounding prose. Before: zero blocks, zero record."""
    path = tmp_path / "display.docx"
    path.write_bytes(
        _docx(
            "<w:p><m:oMathPara><m:oMath>"
            "<m:r><m:t>H</m:t></m:r>"
            "<m:sSub><m:e><m:r><m:t>2</m:t></m:r></m:e>"
            "<m:sub><m:r><m:t>O</m:t></m:r></m:sub></m:sSub>"
            "<m:r><m:t> + NaCl</m:t></m:r>"
            "</m:oMath></m:oMathPara></w:p>"
        )
    )

    texts = _texts(path)

    assert texts, "an authored paragraph produced nothing at all"
    assert "NaCl" in texts[0]


def test_equations_are_counted(tmp_path):
    path = tmp_path / "math.docx"
    path.write_bytes(
        _docx(
            "<w:p><m:oMath><m:r><m:t>a=b</m:t></m:r></m:oMath></w:p>"
            "<w:p><m:oMath><m:r><m:t>c=d</m:t></m:r></m:oMath></w:p>"
        )
    )

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.equations == 2


def test_a_document_with_no_maths_counts_none(tmp_path):
    path = tmp_path / "plain.docx"
    path.write_bytes(_docx("<w:p><w:r><w:t>no equations here</w:t></w:r></w:p>"))

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.equations == 0


def test_math_inside_a_table_cell_reaches_the_row(tmp_path):
    path = tmp_path / "mathtable.docx"
    path.write_bytes(
        _docx(
            "<w:tbl><w:tr>"
            "<w:tc><w:p><w:r><w:t>Ideal gas</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><m:oMath><m:r><m:t>PV=nRT</m:t></m:r></m:oMath></w:p></w:tc>"
            "</w:tr></w:tbl>"
        )
    )

    rows = [
        b.text
        for b in iter_docx_blocks(path, diagnostics=OoxmlDiagnostics())
        if b.kind == "table_row"
    ]

    assert rows == ["Ideal gas | PV=nRT"], rows


def test_suppressed_regions_still_suppress_maths(tmp_path):
    """Maths inside the `w:moveFrom` copy of moved text is still a duplicate."""
    path = tmp_path / "movedmath.docx"
    path.write_bytes(
        _docx(
            '<w:p><w:moveFrom w:id="1">'
            "<m:oMath><m:r><m:t>x=1</m:t></m:r></m:oMath></w:moveFrom></w:p>"
            '<w:p><w:moveTo w:id="2">'
            "<m:oMath><m:r><m:t>x=1</m:t></m:r></m:oMath></w:moveTo></w:p>"
        )
    )

    assert _texts(path) == ["x=1"]


# --------------------------------------------------------------------------- #
# PPTX -- same namespace, same defect
# --------------------------------------------------------------------------- #


def _pptx(slide_body: str) -> bytes:
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    p = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    r = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="ppt/presentation.xml"/></Relationships>',
        "ppt/presentation.xml": f'<?xml version="1.0"?><p:presentation {p} {r}>'
        '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>'
        "</Relationships>",
        "ppt/slides/slide1.xml": f'<?xml version="1.0"?><p:sld {a} {p} {_M_NS}>'
        f"<p:cSld><p:spTree>{slide_body}</p:spTree></p:cSld></p:sld>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def test_slide_maths_is_not_dropped(tmp_path):
    path = tmp_path / "math.pptx"
    path.write_bytes(
        _pptx(
            "<p:sp><p:txBody><a:p><a:r><a:t>Einstein said </a:t></a:r>"
            "<m:oMath><m:r><m:t>E=mc2</m:t></m:r></m:oMath>"
            "<a:r><a:t> in 1905.</a:t></a:r></a:p></p:txBody></p:sp>"
        )
    )

    texts = [b.text for b in iter_pptx_blocks(path, diagnostics=OoxmlDiagnostics())]

    assert texts, texts
    assert "E=mc2" in texts[0], texts
