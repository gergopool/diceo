"""A text box shreds the paragraph it is anchored to.

`w:txbxContent` holds ordinary `w:p` elements, and they sit **inside a run of the
host paragraph** — Word anchors a floating box to a paragraph, so the box's markup is
nested in it. With one paragraph accumulator for the whole part, the box's inner
`</w:p>` flushed the host's runs *so far* together with the box's text, and then
`para.reset()` threw away the host's style with them. Measured before this suite
existed, on the fixtures below:

* anchored mid-paragraph: one block reading
  `'Revenue grew 12 percent in 2024. Pull quote: the Arctic route opened in 2019.'`
  followed by the decapitated `'The board approved the plan.'` — a sentence the
  document does not contain, and a second one that has lost its subject.
* anchored in a heading: `'Chapter 3 FindingsSidebar: 3.2 million hectares.'` at
  heading level 1, glued with no separator. That string then becomes the heading
  *context* of every chunk in the section.
* anchored in a table cell: `'Cell AFloating note. | Cell B'`.

The fix is a stack of accumulator frames, so the box's paragraphs are their own
blocks and the host survives intact. They are **emitted**, not dropped: a pull quote,
a callout or a sidebar routinely carries a number that is nowhere else in the
document. They are also counted, because they are not in the host's reading order.

`mc:AlternateContent` is the other half of the same real-world markup — Word writes
the box twice, once under `mc:Choice` and once under `mc:Fallback` — and is already
resolved to one branch (`test_docx_revision_marks.py`). Every fixture here carries
both branches, the way Word does, so the two mechanisms are tested together.

The real corpus barely exercises this: 1 of its 51 DOCX files has a
`w:txbxContent` at all (`ipbes-invasive-species-spm-fr.docx`, whose two boxes carry
the figure panel labels `A` and `B` and whose host paragraph has no text of its own),
and **0** of the 24 held-out DOCX files the fingerprint hashes have one.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks

from .fixtures import _CONTENT_TYPES, _W_NS

_MC_NS = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
#: The namespaces Word's own text-box markup uses. `v:` and `wps:` are declared
#: because the fallback branch is genuine VML; an undeclared prefix is not
#: well-formed XML and would never reach the reader.
_DRAWING_NS = (
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
)

_STYLES = (
    f'<?xml version="1.0"?><w:styles {_W_NS}>'
    '<w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style></w:styles>'
)


def _docx(body: str, extra: dict[str, str] | None = None) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0"?>'
        f"<w:document {_W_NS} {_MC_NS} {_DRAWING_NS}>"
        f"<w:body>{body}</w:body></w:document>",
    }
    parts.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _blocks(path: Path):
    return list(iter_docx_blocks(path, diagnostics=OoxmlDiagnostics()))


def _texts(path: Path) -> list[str]:
    return [b.text for b in _blocks(path)]


def _textbox(*paragraphs: str) -> str:
    """A floating text box exactly as Word writes it: both `mc:` branches, the
    DrawingML one under `mc:Choice` and the VML one under `mc:Fallback`."""
    inner = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return (
        "<w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wps:txbx>'
        f"<w:txbxContent>{inner}</w:txbxContent>"
        "</wps:txbx></wp:anchor></w:drawing></mc:Choice>"
        f"<mc:Fallback><w:pict><v:shape><v:textbox><w:txbxContent>{inner}"
        "</w:txbxContent></v:textbox></v:shape></w:pict></mc:Fallback>"
        "</mc:AlternateContent></w:r>"
    )


# --------------------------------------------------------------------------- #
# the host paragraph
# --------------------------------------------------------------------------- #

_MID_PARAGRAPH = (
    "<w:p><w:r><w:t>Revenue grew 12 percent in 2024. </w:t></w:r>"
    + _textbox("Pull quote: the Arctic route opened in 2019.")
    + "<w:r><w:t>The board approved the plan.</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>Next paragraph.</w:t></w:r></w:p>"
)


def test_the_host_paragraph_survives_in_one_piece(tmp_path):
    """The assertion that was failing. The host's two runs belong to one sentence
    and nothing from the box may appear between them."""
    path = tmp_path / "textbox.docx"
    path.write_bytes(_docx(_MID_PARAGRAPH))

    texts = _texts(path)

    assert "Revenue grew 12 percent in 2024. The board approved the plan." in texts, texts


def test_the_spliced_sentence_is_gone(tmp_path):
    """The exact string diceo emitted before the fix. A sentence that is in no
    document is worse than a missing one: it retrieves, and it is wrong."""
    path = tmp_path / "textbox.docx"
    path.write_bytes(_docx(_MID_PARAGRAPH))

    for text in _texts(path):
        assert "2024. Pull quote" not in text, f"the box is still spliced in: {text!r}"


def test_the_textbox_is_its_own_block(tmp_path):
    """Emitted, not dropped. A pull quote carries facts that are nowhere else."""
    path = tmp_path / "textbox.docx"
    path.write_bytes(_docx(_MID_PARAGRAPH))

    assert "Pull quote: the Arctic route opened in 2019." in _texts(path)


def test_the_box_is_read_once_not_once_per_branch(tmp_path):
    """Both `mc:` branches carry the box. The existing `mc:AlternateContent`
    resolution has to keep working now that the box is a block of its own."""
    path = tmp_path / "textbox.docx"
    path.write_bytes(_docx(_MID_PARAGRAPH))

    texts = _texts(path)

    assert texts.count("Pull quote: the Arctic route opened in 2019.") == 1, texts


def test_nothing_after_the_host_paragraph_is_disturbed(tmp_path):
    path = tmp_path / "textbox.docx"
    path.write_bytes(_docx(_MID_PARAGRAPH))

    assert _texts(path)[-1] == "Next paragraph."


# --------------------------------------------------------------------------- #
# the host's *properties*, not only its runs
# --------------------------------------------------------------------------- #

_IN_HEADING = (
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
    "<w:r><w:t>Chapter 3 Findings</w:t></w:r>"
    + _textbox("Sidebar: 3.2 million hectares.")
    + "</w:p>"
)


def test_the_heading_keeps_its_text_and_its_level(tmp_path):
    """`para.reset()` inside the box cleared the host's `w:pStyle`, so the box text
    became the heading and the real heading was demoted to a paragraph."""
    path = tmp_path / "heading.docx"
    path.write_bytes(_docx(_IN_HEADING, {"word/styles.xml": _STYLES}))

    headings = [b for b in _blocks(path) if b.kind == "heading"]

    assert len(headings) == 1, [(b.kind, b.text) for b in _blocks(path)]
    assert headings[0].text == "Chapter 3 Findings"
    assert headings[0].level == 1


def test_the_sidebar_is_not_promoted_to_a_heading(tmp_path):
    """It inherited the host's style. A callout indexed as an h1 becomes the
    heading context of every chunk in the section."""
    path = tmp_path / "heading.docx"
    path.write_bytes(_docx(_IN_HEADING, {"word/styles.xml": _STYLES}))

    sidebar = next(b for b in _blocks(path) if "Sidebar" in b.text)

    assert sidebar.kind == "paragraph", sidebar
    assert sidebar.level == 0


def test_the_box_keeps_its_own_style(tmp_path):
    """The frame is a real accumulator, not a mute: a heading *inside* a text box
    is still a heading."""
    path = tmp_path / "styled-box.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>Host.</w:t></w:r>"
            "<w:r><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wps:txbx><w:txbxContent>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            "<w:r><w:t>Box title</w:t></w:r></w:p>"
            "</w:txbxContent></wps:txbx></wp:anchor></w:drawing></mc:Choice>"
            "</mc:AlternateContent></w:r></w:p>",
            {"word/styles.xml": _STYLES},
        )
    )

    blocks = _blocks(path)

    assert ("heading", "Box title", 1) in [(b.kind, b.text, b.level) for b in blocks]
    assert "Host." in [b.text for b in blocks]


# --------------------------------------------------------------------------- #
# shape: several paragraphs, nesting, tables
# --------------------------------------------------------------------------- #


def test_a_multi_paragraph_box_keeps_its_paragraphs(tmp_path):
    path = tmp_path / "multi.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>Host. </w:t></w:r>"
            + _textbox("Callout line one.", "Callout line two.")
            + "<w:r><w:t>Host tail.</w:t></w:r></w:p>"
        )
    )

    texts = _texts(path)

    assert "Callout line one." in texts
    assert "Callout line two." in texts
    assert "Host. Host tail." in texts


def test_a_box_inside_a_box_does_not_unbalance_the_stack(tmp_path):
    """A stack, not one saved frame. If the pop went wrong the host's runs would
    leak into the outer box -- or, worse, the frame list would never empty and the
    rest of the document would be attributed to a text box."""
    path = tmp_path / "nested.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>Host.</w:t></w:r>"
            "<w:r><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wps:txbx><w:txbxContent>'
            "<w:p><w:r><w:t>Outer box.</w:t></w:r>"
            "<w:r><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wps:txbx><w:txbxContent>'
            "<w:p><w:r><w:t>Inner box.</w:t></w:r></w:p>"
            "</w:txbxContent></wps:txbx></wp:anchor></w:drawing></mc:Choice>"
            "</mc:AlternateContent></w:r></w:p>"
            "</w:txbxContent></wps:txbx></wp:anchor></w:drawing></mc:Choice>"
            "</mc:AlternateContent></w:r></w:p>"
            "<w:p><w:r><w:t>After everything.</w:t></w:r></w:p>"
        )
    )

    texts = _texts(path)

    assert texts == ["Inner box.", "Outer box.", "Host.", "After everything."], texts


def test_a_box_anchored_in_a_cell_does_not_pollute_the_cell(tmp_path):
    """`'Cell AFloating note.'` welded a floating callout onto a column's value, so
    every downstream consumer read the note as data."""
    path = tmp_path / "cell.docx"
    path.write_bytes(
        _docx(
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell A</w:t></w:r>"
            + _textbox("Floating note.")
            + "</w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>Cell B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        )
    )

    blocks = _blocks(path)
    rows = [b.text for b in blocks if b.kind == "table_row"]

    assert rows == ["Cell A | Cell B"], rows
    assert "Floating note." in [b.text for b in blocks], "the note was lost entirely"


def test_a_table_inside_a_text_box_still_works(tmp_path):
    """The table frames and the paragraph frames are separate stacks; a box holding
    a table must not confuse either."""
    path = tmp_path / "boxtable.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>Host.</w:t></w:r>"
            "<w:r><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:drawing><wp:anchor><wps:txbx><w:txbxContent>'
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>x</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>y</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            "</w:txbxContent></wps:txbx></wp:anchor></w:drawing></mc:Choice>"
            "</mc:AlternateContent></w:r></w:p>"
        )
    )

    blocks = _blocks(path)

    assert [b.text for b in blocks if b.kind == "table_row"] == ["x | y"]
    assert "Host." in [b.text for b in blocks]


def test_a_document_without_a_text_box_is_untouched(tmp_path):
    path = tmp_path / "plain.docx"
    path.write_bytes(
        _docx("<w:p><w:r><w:t>One.</w:t></w:r></w:p><w:p><w:r><w:t>Two.</w:t></w:r></w:p>")
    )

    assert _texts(path) == ["One.", "Two."]


# --------------------------------------------------------------------------- #
# rule 3: the count
# --------------------------------------------------------------------------- #


def test_textbox_paragraphs_are_counted(tmp_path):
    path = tmp_path / "multi.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>Host.</w:t></w:r>"
            + _textbox("Callout line one.", "Callout line two.")
            + "</w:p>"
        )
    )

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.textbox_paragraphs == 2


def test_the_count_reaches_the_public_diagnostics(tmp_path):
    path = tmp_path / "textbox.docx"
    path.write_bytes(_docx(_MID_PARAGRAPH))

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any("textbox_paragraphs=1" in note for note in report.notes), report.notes


# --------------------------------------------------------------------------- #
# the real file
# --------------------------------------------------------------------------- #

_IPBES = Path("data/fixtures/public-office/ipbes-invasive-species-spm-fr.docx")


def test_the_ipbes_panel_labels_survive():
    """The only real DOCX in the corpus with a text box. Its two boxes carry the figure
    SPM.4 panel labels `A` and `B`, each written twice by `mc:AlternateContent`, and
    the host paragraph has no text of its own — so this file cannot show the splice,
    but it is the regression pin that the frame does not *lose* a box.
    """
    if not _IPBES.exists():
        import pytest

        pytest.skip("corpus fixture not present")

    report = OoxmlDiagnostics()
    texts = [b.text for b in iter_docx_blocks(_IPBES, diagnostics=report)]

    assert texts.count("A") == 1, "the panel label was duplicated or lost"
    assert texts.count("B") == 1
    assert report.textbox_paragraphs == 2
    caption = next(t for t in texts if t.startswith("Figure SPM.4."))
    assert "Distribution mondiale" in caption
