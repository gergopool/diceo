"""Flat-list regression minimized from NASA Lunar Delivery slides 3 and 15.

Original/render evidence is recorded in research experiment 058. The compact XML
uses replacement labels; public originals remain in the hash-pinned research cache.
"""

import io
import zipfile

import pytest

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_pptx_blocks

from .test_pptx_missing_parts import _pptx, _slide

_D = 'xmlns:d="http://schemas.openxmlformats.org/drawingml/2006/diagram"'
_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
_MODEL = (
    f"<d:dataModel {_D} {_A}><d:ptLst>"
    '<d:pt modelId="root" type="doc"/>'
    '<d:pt modelId="second"><d:t><a:p><a:r><a:t>Second item</a:t></a:r></a:p></d:t></d:pt>'
    '<d:pt modelId="first"><d:t><a:p><a:r><a:t>First</a:t></a:r><a:br/>'
    "<a:r><a:t>item</a:t></a:r></a:p><a:p><a:r><a:t>Detail</a:t></a:r></a:p></d:t></d:pt>"
    '<d:pt modelId="copy" type="pres"><d:t><a:p><a:r><a:t>First item</a:t>'
    "</a:r></a:p></d:t></d:pt></d:ptLst><d:cxnLst>"
    '<d:cxn srcId="root" destId="second" srcOrd="1"/>'
    '<d:cxn srcId="root" destId="first" srcOrd="0"/>'
    "</d:cxnLst></d:dataModel>"
)


def _deck(model=_MODEL, *, target="../diagrams/data1.xml", mode=""):
    # Only the chosen diagram branch should be read; the fallback repeats its text.
    body = (
        f'<mc:AlternateContent {_MC}><mc:Choice Requires="d" {_D} {_R}>'
        '<p:graphicFrame><a:graphic><a:graphicData><d:relIds r:dm="data"/>'
        "</a:graphicData></a:graphic></p:graphicFrame></mc:Choice><mc:Fallback>"
        "<p:sp><p:txBody><a:p><a:r><a:t>First item</a:t></a:r></a:p></p:txBody></p:sp>"
        "</mc:Fallback></mc:AlternateContent>"
    )
    return _pptx(
        {1: _slide("Title").replace("</p:spTree>", body + "</p:spTree>"), 2: _slide("Later")},
        extra={
            "ppt/slides/_rels/slide1.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="data" Type="http://schemas.'
                'openxmlformats.org/officeDocument/2006/relationships/diagramData" '
                f'Target="{target}" {mode}/></Relationships>'
            ),
            "ppt/diagrams/data1.xml": model,
        },
    )


def test_flat_smartart_uses_authored_order_without_presentation_or_fallback_duplicates(
    tmp_path,
):
    path = tmp_path / "list.pptx"
    path.write_bytes(_deck())
    stats = OoxmlDiagnostics()
    blocks = list(iter_pptx_blocks(path, diagnostics=stats))
    assert [(b.text, b.part) for b in blocks] == [
        ("Title", 1),
        ("First item", 1),
        ("Detail", 1),
        ("Second item", 1),
        ("Later", 2),
    ]
    assert stats.smartart_texts == 3
    report = diceo.Diagnostics()
    chunks = list(diceo.chunk(path, diagnostics=report))
    assert "First item" in chunks[0].text
    assert chunks[0].locator.page == 0
    assert "smartart_texts=3" in " ".join(report.notes)
    assert "visual relationships/unsupported text order" in " ".join(report.notes)


@pytest.mark.parametrize(
    "model",
    [
        _MODEL.replace('srcOrd="1"', 'srcOrd="0"'),  # ambiguous sibling order
        _MODEL.replace('srcId="root" destId="second"', 'srcId="first" destId="second"'),
        _MODEL.replace('destId="first"', 'destId="missing"'),
    ],
)
def test_unsupported_smartart_does_not_invent_order(tmp_path, model):
    path = tmp_path / "list.pptx"
    path.write_bytes(_deck(model))
    report = diceo.Diagnostics()
    chunks = list(diceo.chunk(path, diagnostics=report))
    assert "Title" in chunks[0].text and "Later" in " ".join(c.text for c in chunks)
    assert "First item" not in " ".join(c.text for c in chunks)
    assert "smartart=1 smartart_texts=0" in " ".join(report.notes)


@pytest.mark.parametrize(
    "options",
    [
        {"model": "<broken"},
        {"target": "../diagrams/absent.xml"},
        {"target": "https://example.invalid/diagram.xml", "mode": 'TargetMode="External"'},
    ],
)
def test_bad_diagram_keeps_later_slides_and_reports_loss(tmp_path, options):
    path = tmp_path / "list.pptx"
    path.write_bytes(_deck(**options))
    report = diceo.Diagnostics()
    chunks = list(diceo.chunk(path, diagnostics=report))
    assert "Later" in " ".join(c.text for c in chunks)
    assert report.lost_data
    assert "parts_missing" in " ".join(report.truncated)


@pytest.mark.parametrize(
    "model, ceiling",
    [
        ('<!DOCTYPE dataModel [<!ENTITY x "expansion">]>' + _MODEL, None),
        (_MODEL, "_MAX_DEPTH"),
        (_MODEL, "_MAX_OPEN_ELEMENTS"),
    ],
)
def test_diagram_retains_entity_and_accumulated_tree_guards(monkeypatch, model, ceiling):
    from diceo import ooxml

    if ceiling:
        monkeypatch.setattr(ooxml, ceiling, 3)
    with (
        zipfile.ZipFile(io.BytesIO(_deck(model))) as archive,
        pytest.raises(diceo.CorruptDocument),
    ):
        list(
            ooxml._iter_smartart_text(
                archive, "ppt/slides/slide1.xml", "data", 1, OoxmlDiagnostics()
            )
        )
