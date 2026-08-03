"""Three defects in the slide reader, all reproduced before this suite existed.

* **`a:tc` boundaries were not tracked.** A table row's fields came from its
  *paragraphs*, so a cell containing two paragraphs became two columns and an empty
  cell became none. `Region | Q1 | Q2` over `EMEA`+`note` / (empty) / `91` came out as
  `EMEA | note | 91` — the right *number* of fields by coincidence, with `note` sitting
  under Q1 and `91` under Q2.
* **`mc:AlternateContent` emitted both renderings.** Word and PowerPoint wrap every
  shape, chart and SmartArt in an `mc:Choice` plus an `mc:Fallback` carrying the same
  text; the slide path read both, so `Chart title` was indexed twice.
* **Hidden slides were indexed with nothing said.** `<p:sld show="0">` is a slide the
  presenter chose not to show — routinely a superseded draft. Still indexed, because
  rule 3 forbids dropping content, but now counted, the way hidden *sheets* already are.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_pptx_blocks

from .fixtures import _CONTENT_TYPES

_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
_MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def _pptx(slides: list[str], hidden: set[int] | None = None) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="ppt/presentation.xml"/></Relationships>',
    }
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i + 1}"/>' for i in range(len(slides)))
    parts["ppt/presentation.xml"] = (
        f'<?xml version="1.0"?><p:presentation {_P} {_R}>'
        f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>"
    )
    parts["ppt/_rels/presentation.xml.rels"] = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships">'
        + "".join(
            f'<Relationship Id="rId{i + 1}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
            for i in range(len(slides))
        )
        + "</Relationships>"
    )
    for index, body in enumerate(slides, start=1):
        show = ' show="0"' if hidden and index in hidden else ""
        parts[f"ppt/slides/slide{index}.xml"] = (
            f'<?xml version="1.0"?><p:sld {_A} {_P} {_MC}{show}>'
            f"<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _cell(*paragraphs: str) -> str:
    body = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs)
    return f"<a:tc><a:txBody>{body}</a:txBody></a:tc>"


def _table(*rows: str) -> str:
    inner = "".join(f"<a:tr>{row}</a:tr>" for row in rows)
    return (
        "<p:graphicFrame><a:graphic><a:graphicData><a:tbl>"
        f"{inner}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def _shape(text: str) -> str:
    return f"<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"


def _blocks(path: Path, report: OoxmlDiagnostics | None = None):
    return list(iter_pptx_blocks(path, diagnostics=report or OoxmlDiagnostics()))


# --------------------------------------------------------------------------- #
# a:tc
# --------------------------------------------------------------------------- #


def test_a_cell_with_two_paragraphs_is_one_column(tmp_path):
    path = tmp_path / "table.pptx"
    path.write_bytes(
        _pptx(
            [
                _table(
                    _cell("Region") + _cell("Q1") + _cell("Q2"),
                    _cell("EMEA", "note") + _cell("46") + _cell("91"),
                )
            ]
        )
    )

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]

    assert rows[0] == "Region | Q1 | Q2"
    assert rows[1].split(" | ")[0] == "EMEA note", rows
    assert len(rows[1].split(" | ")) == 3, rows


def test_an_empty_cell_keeps_its_column(tmp_path):
    path = tmp_path / "table.pptx"
    path.write_bytes(
        _pptx(
            [
                _table(
                    _cell("Region") + _cell("Q1") + _cell("Q2"),
                    _cell("EMEA") + _cell() + _cell("91"),
                )
            ]
        )
    )

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]
    fields = rows[1].split(" | ")

    assert len(fields) == 3, rows
    assert fields[2] == "91", "91 must stay under Q2"


def test_a_row_of_empty_cells_is_not_a_row(tmp_path):
    path = tmp_path / "table.pptx"
    path.write_bytes(_pptx([_table(_cell() + _cell(), _cell("real") + _cell("row"))]))

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]

    assert rows == ["real | row"], rows


def test_text_outside_the_table_is_still_a_paragraph(tmp_path):
    path = tmp_path / "mixed.pptx"
    path.write_bytes(_pptx([_shape("Slide body") + _table(_cell("a") + _cell("b"))]))

    kinds = {b.kind for b in _blocks(path)}

    assert "paragraph" in kinds
    assert "table_row" in kinds


# --------------------------------------------------------------------------- #
# mc:AlternateContent
# --------------------------------------------------------------------------- #


def test_alternate_content_is_read_once(tmp_path):
    path = tmp_path / "alt.pptx"
    path.write_bytes(
        _pptx(
            [
                "<mc:AlternateContent>"
                f'<mc:Choice Requires="a14">{_shape("Chart title")}</mc:Choice>'
                f"<mc:Fallback>{_shape('Chart title')}</mc:Fallback>"
                "</mc:AlternateContent>"
            ]
        )
    )

    texts = [b.text for b in _blocks(path)]

    assert texts == ["Chart title"], texts


def test_a_fallback_with_no_choice_is_still_read(tmp_path):
    path = tmp_path / "alt.pptx"
    path.write_bytes(
        _pptx(
            [
                "<mc:AlternateContent>"
                f"<mc:Fallback>{_shape('only rendering')}</mc:Fallback>"
                "</mc:AlternateContent>"
            ]
        )
    )

    assert [b.text for b in _blocks(path)] == ["only rendering"]


def test_a_table_inside_alternate_content_is_not_doubled(tmp_path):
    """The expensive case: a whole table emitted twice."""
    path = tmp_path / "alt.pptx"
    table = _table(_cell("a") + _cell("b"))
    path.write_bytes(
        _pptx(
            [
                "<mc:AlternateContent>"
                f'<mc:Choice Requires="a14">{table}</mc:Choice>'
                f"<mc:Fallback>{table}</mc:Fallback>"
                "</mc:AlternateContent>"
            ]
        )
    )

    rows = [b.text for b in _blocks(path) if b.kind == "table_row"]

    assert rows == ["a | b"], rows


# --------------------------------------------------------------------------- #
# hidden slides
# --------------------------------------------------------------------------- #


def test_a_hidden_slide_is_counted(tmp_path):
    path = tmp_path / "hidden.pptx"
    path.write_bytes(_pptx([_shape("visible"), _shape("HIDDEN DRAFT")], hidden={2}))

    report = OoxmlDiagnostics()
    _blocks(path, report)

    assert report.slides_hidden == [2]


def test_a_hidden_slide_is_still_indexed(tmp_path):
    """Rule 3: it is content, and dropping it silently is the failure mode. The
    caller is told, the way `sheets_hidden` already tells them."""
    path = tmp_path / "hidden.pptx"
    path.write_bytes(_pptx([_shape("visible"), _shape("HIDDEN DRAFT")], hidden={2}))

    assert "HIDDEN DRAFT" in " ".join(b.text for b in _blocks(path))


def test_no_hidden_slides_means_no_note(tmp_path):
    path = tmp_path / "plain.pptx"
    path.write_bytes(_pptx([_shape("one"), _shape("two")]))

    report = OoxmlDiagnostics()
    _blocks(path, report)

    assert report.slides_hidden == []


def test_the_count_reaches_the_public_diagnostics(tmp_path):
    path = tmp_path / "hidden.pptx"
    path.write_bytes(_pptx([_shape("visible"), _shape("HIDDEN DRAFT")], hidden={2}))

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any("slides_hidden" in note for note in report.notes), report.notes
