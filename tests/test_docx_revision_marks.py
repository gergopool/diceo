"""Three ways a real Word document puts the same text in the file twice.

`ooxml.py`'s docstring already records that `w:delText` is excluded *for free*,
because deleted text uses a different tag from live text and a tag-keyed reader gets
it right without trying. That argument does not extend as far as it looks:

* **A tracked *move* uses plain `w:t`.** `<w:moveFrom>` holds the copy at the old
  location and `<w:moveTo>` the copy at the new one, both in ordinary runs, so a
  reader that only knows about `w:del` emits the sentence **twice** — measured, before
  this suite existed.
* **`mc:AlternateContent` holds two renderings of the same content.** Word writes it
  around every shape, textbox and SmartArt: an `mc:Choice` for consumers that
  understand a namespace and an `mc:Fallback` for those that do not. Reading both gave
  `'Diagram captionDiagram caption'` — not only duplicated but *glued*, which invents
  a token that is in no document.
* **Comments live in `word/comments.xml`.** That is the footnote situation exactly, and
  the docstring calls the footnote version "the rule-3 failure exactly". Microsoft's
  own SDK sample `openxml-sdk-comments.docx` carries `This is a comment.` and diceo
  reported `lost_data=False` — as did every one of the four rivals.

Comments are *reported, not extracted*: review chatter ("please rephrase") is noise in
a production index, while a reviewer's substantive note can be the answer to "why was
this changed". That is genuinely ambiguous, and the project's rule for ambiguous
content — hidden sheets, `display:none` — is to surface it and let the caller decide.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks

from .fixtures import _CONTENT_TYPES, _W_NS

_MC_NS = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def _docx(body: str, extra: dict[str, str] | None = None) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS} {_MC_NS}>'
        f"<w:body>{body}</w:body></w:document>",
    }
    parts.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _texts(path: Path) -> list[str]:
    return [b.text for b in iter_docx_blocks(path, diagnostics=OoxmlDiagnostics())]


# --------------------------------------------------------------------------- #
# tracked moves
# --------------------------------------------------------------------------- #

_MOVED = (
    '<w:p><w:moveFrom w:id="1"><w:r><w:t>The revenue was 1200 million.</w:t></w:r>'
    "</w:moveFrom></w:p>"
    '<w:p><w:moveTo w:id="2"><w:r><w:t>The revenue was 1200 million.</w:t></w:r>'
    "</w:moveTo></w:p>"
)


def test_a_moved_paragraph_is_not_emitted_twice(tmp_path):
    path = tmp_path / "moved.docx"
    path.write_bytes(_docx(_MOVED))

    texts = _texts(path)

    assert texts == ["The revenue was 1200 million."], texts


def test_the_destination_is_the_copy_that_survives(tmp_path):
    """`moveTo` is where the text *is*; `moveFrom` is where it used to be. Keeping
    the wrong one would put the sentence in the wrong section of the document."""
    path = tmp_path / "moved.docx"
    path.write_bytes(
        _docx(
            '<w:p><w:moveFrom w:id="1"><w:r><w:t>old place</w:t></w:r></w:moveFrom></w:p>'
            '<w:p><w:moveTo w:id="2"><w:r><w:t>new place</w:t></w:r></w:moveTo></w:p>'
        )
    )

    assert _texts(path) == ["new place"]


def test_a_move_inside_a_paragraph_leaves_the_rest_alone(tmp_path):
    path = tmp_path / "moved.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>Before. </w:t></w:r>"
            '<w:moveFrom w:id="1"><w:r><w:t>Moved away. </w:t></w:r></w:moveFrom>'
            "<w:r><w:t>After.</w:t></w:r></w:p>"
        )
    )

    assert _texts(path) == ["Before. After."]


def test_deleted_text_is_still_excluded(tmp_path):
    """The behaviour the docstring claims, kept under test now that the claim has
    been shown not to generalise."""
    path = tmp_path / "del.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:del><w:r><w:delText>deleted words </w:delText></w:r></w:del>"
            "<w:r><w:t>kept words</w:t></w:r></w:p>"
        )
    )

    assert _texts(path) == ["kept words"]


def test_an_insertion_is_content(tmp_path):
    """`w:ins` is text that *is* in the document. Suppressing revision marks
    wholesale would lose it."""
    path = tmp_path / "ins.docx"
    path.write_bytes(
        _docx('<w:p><w:ins w:id="1"><w:r><w:t>newly added</w:t></w:r></w:ins></w:p>')
    )

    assert _texts(path) == ["newly added"]


# --------------------------------------------------------------------------- #
# mc:AlternateContent
# --------------------------------------------------------------------------- #


def test_alternate_content_is_read_once(tmp_path):
    path = tmp_path / "alt.docx"
    path.write_bytes(
        _docx(
            "<w:p><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:r><w:t>Diagram caption</w:t></w:r></mc:Choice>'
            "<mc:Fallback><w:r><w:t>Diagram caption</w:t></w:r></mc:Fallback>"
            "</mc:AlternateContent></w:p>"
        )
    )

    assert _texts(path) == ["Diagram caption"]


def test_a_fallback_with_no_choice_is_still_read(tmp_path):
    """Skipping every `mc:Fallback` unconditionally would lose the text of an
    `AlternateContent` that only has one."""
    path = tmp_path / "alt.docx"
    path.write_bytes(
        _docx(
            "<w:p><mc:AlternateContent>"
            "<mc:Fallback><w:r><w:t>only rendering</w:t></w:r></mc:Fallback>"
            "</mc:AlternateContent></w:p>"
        )
    )

    assert _texts(path) == ["only rendering"]


def test_nested_alternate_content_does_not_unbalance(tmp_path):
    """Word nests these around a shape inside a shape. A depth counter that goes
    negative would suppress the rest of the document."""
    path = tmp_path / "alt.docx"
    path.write_bytes(
        _docx(
            "<w:p><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:r><w:t>outer</w:t></w:r>'
            "<mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:r><w:t> inner</w:t></w:r></mc:Choice>'
            "<mc:Fallback><w:r><w:t> inner</w:t></w:r></mc:Fallback>"
            "</mc:AlternateContent></mc:Choice>"
            "<mc:Fallback><w:r><w:t>outer inner</w:t></w:r></mc:Fallback>"
            "</mc:AlternateContent></w:p>"
            "<w:p><w:r><w:t>after</w:t></w:r></w:p>"
        )
    )

    assert _texts(path) == ["outer inner", "after"]


# --------------------------------------------------------------------------- #
# comments
# --------------------------------------------------------------------------- #

_COMMENTS = (
    f'<?xml version="1.0"?><w:comments {_W_NS}>'
    '<w:comment w:id="1" w:author="A"><w:p><w:r><w:t>Check this number.</w:t></w:r>'
    "</w:p></w:comment>"
    '<w:comment w:id="2" w:author="B"><w:p><w:r><w:t>Agreed.</w:t></w:r></w:p>'
    "</w:comment></w:comments>"
)


def test_comments_are_counted(tmp_path):
    path = tmp_path / "commented.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>The revenue was 1200 million.</w:t></w:r>"
            '<w:commentReference w:id="1"/></w:p>',
            {"word/comments.xml": _COMMENTS},
        )
    )

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.comments == 2


def test_comment_text_is_not_indexed(tmp_path):
    """Deliberate. Review chatter in a production index is noise, and the caller is
    told it exists rather than having it decided for them silently."""
    path = tmp_path / "commented.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>The revenue was 1200 million.</w:t></w:r></w:p>",
            {"word/comments.xml": _COMMENTS},
        )
    )

    joined = " ".join(_texts(path))

    assert "Check this number" not in joined


def test_a_document_with_no_comments_counts_none(tmp_path):
    path = tmp_path / "plain.docx"
    path.write_bytes(_docx("<w:p><w:r><w:t>plain</w:t></w:r></w:p>"))

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.comments == 0


def test_the_count_reaches_the_public_diagnostics(tmp_path):
    path = tmp_path / "commented.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>The revenue was 1200 million.</w:t></w:r></w:p>",
            {"word/comments.xml": _COMMENTS},
        )
    )

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any("comments=2" in note for note in report.notes), report.notes


def test_an_unreadable_comments_part_does_not_break_the_document(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>still here</w:t></w:r></w:p>",
            {"word/comments.xml": "<<<not xml at all"},
        )
    )

    assert _texts(path) == ["still here"]


# --------------------------------------------------------------------------- #
# an unbalanced field must not mute the document
# --------------------------------------------------------------------------- #


def test_an_unbalanced_field_costs_one_paragraph_not_the_document(tmp_path):
    """The worst failure this audit found.

    Field suppression was one counter with no bound, so a `w:fldChar` `begin` with
    no matching `separate` or `end` — a field Word left half-written, and a shape
    real documents have — suppressed **every run for the rest of the part**.
    Measured before the fix: a document with five paragraphs of content emitted
    **zero blocks**, with no exception and nothing in diagnostics.

    A field *instruction* lives inside one paragraph in every real document: for a
    complex field the `begin`, `instrText` and `separate` are together and only the
    cached *result* spans paragraphs, after suppression has already been lifted. So
    closing the field at the end of its paragraph is right for real files and caps
    the damage from a broken one at that paragraph.
    """
    path = tmp_path / "unbalanced.docx"
    body = (
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText>PAGEREF _Toc1</w:instrText></w:r></w:p>"
    ) + "".join(
        f"<w:p><w:r><w:t>Paragraph {i} of real content.</w:t></w:r></w:p>" for i in range(5)
    )
    path.write_bytes(_docx(body))

    texts = _texts(path)

    assert len(texts) == 5, texts
    assert "Paragraph 0 of real content." in texts


def test_the_abandoned_field_is_reported(tmp_path):
    path = tmp_path / "unbalanced.docx"
    path.write_bytes(
        _docx(
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            "<w:r><w:instrText>PAGEREF _Toc1</w:instrText></w:r></w:p>"
            "<w:p><w:r><w:t>content</w:t></w:r></w:p>"
        )
    )

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.fields_unclosed == 1


def test_a_well_formed_field_still_suppresses_its_instruction(tmp_path):
    """The behaviour being protected: `PAGE \\* MERGEFORMAT` must not reach the
    index, while the cached result after `separate` must."""
    path = tmp_path / "field.docx"
    path.write_bytes(
        _docx(
            "<w:p>"
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            "<w:r><w:instrText>PAGE \\* MERGEFORMAT</w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>17</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            "</w:p>"
        )
    )

    assert _texts(path) == ["17"]


def test_a_field_result_spanning_paragraphs_survives(tmp_path):
    """A TOC field's *result* is many paragraphs long. Suppression is already lifted
    at `separate`, so closing the field per paragraph must not disturb it."""
    path = tmp_path / "toc.docx"
    path.write_bytes(
        _docx(
            "<w:p>"
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText>TOC \\o "1-3"</w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>1 Introduction</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>2 Method</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        )
    )

    assert _texts(path) == ["1 Introduction", "2 Method"]
