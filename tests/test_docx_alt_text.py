"""A figure's alt text was discarded while the figure itself was counted.

`count_media` reports `media_parts=7`, so the caller knows the report had charts. The
one machine-readable sentence saying *what* those charts show —
`wp:docPr/@descr`, the "Alt Text" pane in Word — was read by nothing. For a chart
that is the whole message: the pixels are unindexable without OCR, and the author
already wrote the caption.

The half of this that is not obvious, and that
experiment 038 (edge case mining) insisted on, is the **filter**.
Word writes alt text by itself and has done so by default since 2019. It describes
pixels rather than meaning:

    A close-up of a chart Description automatically generated
    A white sheet with black text Description automatically generated with medium confidence

Measured here, on the 51 real DOCX files in the research corpus: **18 of 18** non-empty
`descr` values are exactly this. On a real NASA deck, MarkItDown indexed
`A helicopter flying over a city Description automatically generated` as document
content. That is a sentence the document does not say, and it retrieves. So the
generated forms are rejected and counted, and only authored text is emitted.

Fingerprint consequence, measured before the change: of the 24 held-out DOCX files
the fingerprint hashes, **150** `wp:docPr` elements carry no `descr` or `title` at
all, so nothing this module adds can reach those digests.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks

from .fixtures import _CONTENT_TYPES, _W_NS

_MC_NS = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'
_WP_NS = 'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'


def _docx(body: str) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS} {_MC_NS} {_WP_NS}>'
        f"<w:body>{body}</w:body></w:document>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _figure(attributes: str, name: str = "Picture 1") -> str:
    """An inline drawing, with the `wp:docPr` Word puts on every one of them."""
    return (
        f"<w:p><w:r><w:drawing><wp:inline>"
        f'<wp:docPr id="1" name="{name}" {attributes}/>'
        f"</wp:inline></w:drawing></w:r></w:p>"
    )


def _blocks(path: Path):
    return list(iter_docx_blocks(path, diagnostics=OoxmlDiagnostics()))


def _captions(path: Path) -> list[str]:
    return [b.text for b in _blocks(path) if b.kind == "caption"]


# --------------------------------------------------------------------------- #
# authored alt text is content
# --------------------------------------------------------------------------- #

_AUTHORED = "Global spread of the zebra mussel, 1988 to 2020"


def test_authored_alt_text_is_emitted(tmp_path):
    """The defect: this produced **no blocks at all**, with `media_parts` counting
    the image it described."""
    path = tmp_path / "figure.docx"
    path.write_bytes(_docx(_figure(f'descr="{_AUTHORED}"')))

    assert _captions(path) == [_AUTHORED]


def test_it_is_a_caption_not_a_paragraph(tmp_path):
    """`caption` is the kind the chunker carries onto a table's row groups; a figure
    description is the same sort of thing and must not be indexed as body prose."""
    path = tmp_path / "figure.docx"
    path.write_bytes(_docx(_figure(f'descr="{_AUTHORED}"')))

    assert [b.kind for b in _blocks(path)] == ["caption"]


def test_title_is_used_when_descr_is_absent(tmp_path):
    """`descr` first — it is the field the Alt Text pane writes and the one a screen
    reader announces — but `title` is authored too and is all some producers set."""
    path = tmp_path / "titled.docx"
    path.write_bytes(_docx(_figure('title="Zebra mussel range map"')))

    assert _captions(path) == ["Zebra mussel range map"]


def test_descr_wins_over_title(tmp_path):
    path = tmp_path / "both.docx"
    path.write_bytes(_docx(_figure(f'descr="{_AUTHORED}" title="Map"')))

    assert _captions(path) == [_AUTHORED]


def test_the_caption_sits_beside_the_paragraph_text(tmp_path):
    """An inline figure lives in a run of a real paragraph. Emitting the caption must
    not disturb that paragraph."""
    path = tmp_path / "inline.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>See the map. </w:t></w:r>"
            "<w:r><w:drawing><wp:inline>"
            f'<wp:docPr id="1" name="Picture 1" descr="{_AUTHORED}"/>'
            "</wp:inline></w:drawing></w:r>"
            "<w:r><w:t>It is unambiguous.</w:t></w:r></w:p>"
        )
    )

    assert [b.text for b in _blocks(path)] == [
        _AUTHORED,
        "See the map. It is unambiguous.",
    ]


def test_whitespace_is_collapsed(tmp_path):
    """Word writes `\\n\\n` inside `descr`. A caption is one line."""
    path = tmp_path / "wrapped.docx"
    path.write_bytes(_docx(_figure('descr="Range&#10;&#10;map   of   1988"')))

    assert _captions(path) == ["Range map of 1988"]


# --------------------------------------------------------------------------- #
# machine-written alt text is not
# --------------------------------------------------------------------------- #

#: Real values, taken verbatim from the public-office corpus, plus the NASA
#: deck string 038 recorded MarkItDown emitting as content.
_MACHINE = [
    "A helicopter flying over a city Description automatically generated",
    "A close-up of a chart\n\nDescription automatically generated",
    "A white sheet with black text\n\nDescription automatically generated with "
    "medium confidence",
    "A picture containing outdoor, rock, stone\n\nDescription automatically generated",
    "Chart, bar chart\n\nDescription Automatically Generated",
]


def test_machine_written_alt_text_is_never_emitted(tmp_path):
    for index, value in enumerate(_MACHINE):
        path = tmp_path / f"machine{index}.docx"
        path.write_bytes(_docx(_figure(f'descr="{value.replace(chr(10), "&#10;")}"')))

        assert _captions(path) == [], f"{value!r} reached the index"


def test_a_bare_generator_name_is_not_a_caption(tmp_path):
    """Some producers copy the shape's `name` into `descr`. 'Picture 1' is the id
    spelled out; it describes nothing."""
    for value in ("Picture 1", "Text Box 520001818", "Chart", "image_3", "Rectangle 4"):
        path = tmp_path / "generated.docx"
        path.write_bytes(_docx(_figure(f'descr="{value}"')))

        assert _captions(path) == [], f"{value!r} reached the index"


def test_a_real_caption_that_merely_starts_with_a_noun_survives(tmp_path):
    """The generator filter is anchored and bounded by a trailing number, so it
    cannot eat an authored sentence that begins with the same word."""
    for value in (
        "Chart of invasive species arrivals since 1500",
        "Picture 1 shows the ballast water exchange zone",
        "Diagram: pathways of introduction",
    ):
        path = tmp_path / "real.docx"
        path.write_bytes(_docx(_figure(f'descr="{value}"')))

        assert _captions(path) == [value], value


def test_an_empty_descr_produces_nothing(tmp_path):
    path = tmp_path / "empty.docx"
    path.write_bytes(_docx(_figure('descr="" title="   "')))

    assert _blocks(path) == []


def test_a_docpr_with_no_alt_attributes_produces_nothing(tmp_path):
    path = tmp_path / "bare.docx"
    path.write_bytes(
        _docx(
            '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" '
            'name="Picture 1"/></wp:inline></w:drawing></w:r></w:p>'
        )
    )

    assert _blocks(path) == []


# --------------------------------------------------------------------------- #
# it must not be read twice
# --------------------------------------------------------------------------- #


def test_a_fallback_copy_of_the_figure_is_not_a_second_caption(tmp_path):
    """Word writes the shape under both `mc:` branches. The alt text has to obey the
    same one-branch rule the run text already obeys, or every floating figure gets
    its caption indexed twice."""
    path = tmp_path / "alt.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><mc:AlternateContent>"
            '<mc:Choice Requires="wps"><w:drawing><wp:anchor>'
            f'<wp:docPr id="1" name="Picture 1" descr="{_AUTHORED}"/>'
            "</wp:anchor></w:drawing></mc:Choice>"
            "<mc:Fallback><w:drawing><wp:anchor>"
            f'<wp:docPr id="1" name="Picture 1" descr="{_AUTHORED}"/>'
            "</wp:anchor></w:drawing></mc:Fallback>"
            "</mc:AlternateContent></w:r></w:p>"
        )
    )

    assert _captions(path) == [_AUTHORED]


def test_alt_text_inside_a_field_instruction_is_not_content(tmp_path):
    """The same rule `w:instrText` obeys: between `begin` and `separate` nothing is
    document text."""
    path = tmp_path / "field.docx"
    path.write_bytes(
        _docx(
            "<w:p>"
            '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            "<w:r><w:drawing><wp:inline>"
            f'<wp:docPr id="1" name="Picture 1" descr="{_AUTHORED}"/>'
            "</wp:inline></w:drawing></w:r>"
            "<w:r><w:instrText>INCLUDEPICTURE \\d</w:instrText></w:r>"
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            "<w:r><w:t>result</w:t></w:r>"
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            "</w:p>"
        )
    )

    assert _captions(path) == []


# --------------------------------------------------------------------------- #
# rule 3: both outcomes are counted
# --------------------------------------------------------------------------- #


def test_both_kinds_are_counted(tmp_path):
    path = tmp_path / "mixed.docx"
    path.write_bytes(
        _docx(
            _figure(f'descr="{_AUTHORED}"')
            + _figure('descr="A close-up of a chart Description automatically generated"')
            + _figure('descr="Picture 1"')
        )
    )

    report = OoxmlDiagnostics()
    list(iter_docx_blocks(path, diagnostics=report))

    assert report.figure_alt_texts == 1
    assert report.figure_alt_texts_machine == 2


def test_the_rejection_reaches_the_public_diagnostics(tmp_path):
    """A figure whose only description was Word's is a figure with no authored
    description — which is the answer to 'why is that chart not findable'."""
    path = tmp_path / "machine.docx"
    path.write_bytes(
        _docx(_figure('descr="A close-up of a chart Description automatically generated"'))
    )

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any("figure_alt_texts_machine=1" in note for note in report.notes), report.notes


def test_an_authored_caption_reaches_the_chunks(tmp_path):
    path = tmp_path / "figure.docx"
    path.write_bytes(
        _docx(
            "<w:p><w:r><w:t>The report covers 1988 to 2020.</w:t></w:r></w:p>"
            + _figure(f'descr="{_AUTHORED}"')
        )
    )

    joined = " ".join(chunk.text for chunk in diceo.chunk(path))

    assert _AUTHORED in joined


# --------------------------------------------------------------------------- #
# the real corpus
# --------------------------------------------------------------------------- #

_IPBES = Path("data/fixtures/public-office/ipbes-invasive-species-spm-fr.docx")


def test_the_real_file_contributes_no_machine_captions():
    """Every one of the IPBES report's 18 alt texts is Word's own. The measurement
    that makes the filter load-bearing rather than defensive: without it this one
    document would add 18 sentences of 'A close-up of a chart' to the index."""
    if not _IPBES.exists():
        import pytest

        pytest.skip("corpus fixture not present")

    report = OoxmlDiagnostics()
    blocks = list(iter_docx_blocks(_IPBES, diagnostics=report))

    assert report.figure_alt_texts_machine == 18
    assert report.figure_alt_texts == 0
    assert [b for b in blocks if b.kind == "caption"] == []
