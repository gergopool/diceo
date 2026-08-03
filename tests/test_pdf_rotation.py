"""A page with ``/Rotate`` must read in the order its author wrote it.

The shape that fails is the landscape appendix -- how every wide table in every
government report is typeset -- and it fails twice over. Measured on the fixtures
below against pypdfium2 152.0.7947.0:

    >>> [ln.text[:22] for ln in page_lines(doc[0], 0)]   # /Rotate 90
    ['the survey team during', 'study, together with t',
     'The table below lists ', 'Appendix C Regional To']

The heading comes back **last**. And because `_emit_page` measures the paragraph
gap as ``previous.bbox[1] - line.bbox[3]`` -- previous bottom minus this top -- a
page whose lines arrive bottom-to-top makes that quantity negative on every pair,
so ``gap < -height`` fires on every pair and each line of the paragraph becomes its
own block. Mis-ordered *and* shredded into one-line chunks.

What PDFium is actually doing (measured, all four rotations, both layouts):

    ==========  ====================  ========================================
    /Rotate     text drawn ...        order `FPDFText_GetText` returns
    ==========  ====================  ========================================
    0           horizontally          content-stream order
    90          horizontally          **sorted by page-space y ascending**
    180, 270    horizontally          content-stream order
    90,180,270  rotated to match      content-stream order
    ==========  ====================  ========================================

Two facts carry the fix. First, ``FPDFText_GetCharBox`` reports **unrotated page
space** at every rotation -- the same fixture's boxes are identical whatever
``/Rotate`` says -- so text that is horizontal in page space is read top-to-bottom
by ``(-bbox[3], bbox[0])`` no matter how the page is displayed. Second, PDFium
orders in the *display* frame, which is why only the 90 case scrambles: there, and
only there, does a page-space line become a display-space column.

The second layout is the control that a naive "rot != 0 -> sort" would break.
``pdflscape`` and friends emit ``/Rotate 90`` *and* draw the text sideways, so its
lines are vertical in page space, PDFium's display-frame order is already right,
and sorting those boxes by page-space top scrambles a page that arrived correct.
So the sort is gated on the page's text being horizontal in page space, which is a
property of the boxes we already have and costs no extra FFI call.
"""

from __future__ import annotations

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

import diceo
from diceo.pdf.extract import blocks, page_lines

#: 18 pt against an 11 pt body: enough for `fit_styles` to rank it as level 1.
HEADING = (18.0, "Appendix C Regional Totals")
#: One paragraph, four visual lines. It has to be more than two so that "shredded"
#: and "merged" cannot be confused with an off-by-one.
PARAGRAPH = [
    (11.0, "The table below lists the totals for every region in the"),
    (11.0, "study, together with the confidence interval reported by"),
    (11.0, "the survey team during the review period of the whole"),
    (11.0, "programme, which the committee agreed to in March."),
]
PAGE = [HEADING, *PARAGRAPH]


# --------------------------------------------------------------------------- #
# fixtures -- built here, from the standard library, like `tests/fixtures.py`
# --------------------------------------------------------------------------- #


def rotated_pdf(
    lines: list[tuple[float, str]] | None = None,
    *,
    rotate: int = 0,
    sideways: bool = False,
    reverse_stream: bool = False,
) -> bytes:
    """One page, ``/Rotate rotate``, with a real xref whose offsets are computed.

    ``sideways=False`` is the page somebody rotated *after* it was typeset -- a
    sideways scan straightened in Acrobat, a `pdftk rotate`, a wide table dropped in
    by an imposition tool. The text is ordinary horizontal page-space text and only
    the page dictionary says otherwise.

    ``sideways=True`` is the other real shape: the typesetter drew the text rotated
    so that it reads upright once the viewer applies ``/Rotate``. That is what
    `pdflscape` emits, and its lines are *vertical* in page space.

    ``reverse_stream`` writes the same page bottom-up, which is how the fixture
    proves an ordering claim is about geometry rather than about content order.
    """
    lines = PAGE if lines is None else lines
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    placed: list[tuple[float, str, float, float]] = []
    # Lead by the point size, not by a constant: four lines 1.6 points apart get
    # merged into one by PDFium, which would make every assertion below vacuous.
    offset = 72.0
    for size, text in lines:
        if sideways:
            placed.append((size, text, offset, 72.0))
        else:
            placed.append((size, text, 72.0, 792.0 - offset))
        offset += size * 1.6
    if reverse_stream:
        placed.reverse()

    content = ["BT"]
    for size, text, x, y in placed:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content.append(f"/F1 {size:g} Tf")
        # `0 1 -1 0` turns the glyph advance into +y and the glyph up-vector into
        # -x, which is exactly the frame /Rotate 90 undoes for the reader.
        content.append(f"0 1 -1 0 {x:g} {y:g} Tm" if sideways else f"1 0 0 1 {x:g} {y:g} Tm")
        content.append(f"({escaped}) Tj")
    content.append("ET")
    stream = "\n".join(content).encode("latin-1", "replace")

    spin = f" /Rotate {rotate}" if rotate else ""
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
        + spin.encode()
        + b" /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
    )
    objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset_ in offsets:
        out += f"{offset_:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def write(tmp_path, name: str, **kwargs) -> object:
    path = tmp_path / f"{name}.pdf"
    path.write_bytes(rotated_pdf(**kwargs))
    return path


def shape(path) -> list[tuple[str, str]]:
    """``(kind, text)`` per block -- what a caller's index actually receives."""
    return [(block.kind, block.text) for block in blocks(path)]


def texts(path) -> list[str]:
    doc = pdfium.PdfDocument(path)
    page = doc[0]
    try:
        return [line.text for line in page_lines(page, 0)]
    finally:
        page.close()
        doc.close()


# --------------------------------------------------------------------------- #
# guard the guard -- if PDFium stops doing this, the fix below is theatre
# --------------------------------------------------------------------------- #


def test_pdfium_reports_the_rotation_we_key_the_fix_on(tmp_path):
    """``FPDFPage_GetRotation`` returns quarter turns, not degrees."""
    seen = []
    for degrees in (0, 90, 180, 270):
        path = write(tmp_path, f"spin{degrees}", rotate=degrees)
        doc = pdfium.PdfDocument(path)
        page = doc[0]
        seen.append(pdfium_c.FPDFPage_GetRotation(page.raw))
        page.close()
        doc.close()

    assert seen == [0, 1, 2, 3]


def test_char_boxes_are_in_unrotated_page_space(tmp_path):
    """The fact the sort key rests on.

    If ``FPDFText_GetCharBox`` ever started reporting display space, sorting by
    page-space top would be the wrong axis and this module would be sorting noise.
    """
    boxes = {}
    for degrees in (0, 90, 180, 270):
        path = write(tmp_path, f"box{degrees}", rotate=degrees)
        doc = pdfium.PdfDocument(path)
        page = doc[0]
        boxes[degrees] = sorted(round(line.bbox[3], 1) for line in page_lines(page, 0))
        page.close()
        doc.close()

    assert boxes[90] == boxes[0], boxes
    assert boxes[180] == boxes[0], boxes
    assert boxes[270] == boxes[0], boxes


# --------------------------------------------------------------------------- #
# the defect
# --------------------------------------------------------------------------- #


def test_a_rotated_page_reads_top_to_bottom(tmp_path):
    """Order, not presence. Every line was always *there*; it was in the wrong place,
    which is the failure a `chars` count cannot see."""
    path = write(tmp_path, "landscape", rotate=90)

    assert texts(path) == [text for _size, text in PAGE]


def test_the_heading_is_the_first_block_on_a_rotated_page(tmp_path):
    path = write(tmp_path, "landscape", rotate=90)

    found = shape(path)

    assert found[0][0] == "heading", found
    assert found[0][1] == HEADING[1], found


def test_a_paragraph_on_a_rotated_page_is_one_block(tmp_path):
    """The shredding half. Four body lines arriving bottom-to-top made
    ``gap = previous.bbox[1] - line.bbox[3]`` negative on every pair, so every line
    became its own chunk -- a landscape appendix indexed as one-line fragments."""
    path = write(tmp_path, "landscape", rotate=90)

    paragraphs = [text for kind, text in shape(path) if kind == "para"]

    assert len(paragraphs) == 1, paragraphs
    assert paragraphs[0] == " ".join(text for _size, text in PARAGRAPH)


def test_rotation_does_not_change_what_the_document_says(tmp_path):
    """The whole claim in one assertion: the same page, spun four ways, is one
    document. ``/Rotate`` is a viewing instruction and must not reach the index."""
    upright = shape(write(tmp_path, "upright", rotate=0))

    for degrees in (90, 180, 270):
        assert shape(write(tmp_path, f"spun{degrees}", rotate=degrees)) == upright, degrees


def test_a_rotated_page_survives_the_public_api(tmp_path):
    """`page_lines` is where the fix lives; a caller sees `diceo.chunk`."""
    path = write(tmp_path, "landscape", rotate=90)

    text = "".join(piece.text for piece in diceo.chunk(path))

    assert text.index(HEADING[1]) < text.index(PARAGRAPH[0][1]), text


# --------------------------------------------------------------------------- #
# controls -- the half that says the fix does not fire where it must not
# --------------------------------------------------------------------------- #


def test_an_unrotated_page_is_never_reordered(tmp_path):
    """The control that matters most, and it has to be built out of a page whose
    content stream is *not* in visual order -- otherwise "sorted" and "untouched"
    look identical and the test proves nothing.

    At ``/Rotate 0`` diceo hands back exactly what PDFium hands it, bottom-up page
    and all. That is the behaviour every published number was measured on, and no
    rotation fix is allowed to quietly improve it.
    """
    path = write(tmp_path, "upside_stream", rotate=0, reverse_stream=True)

    assert texts(path) == [text for _size, text in reversed(PAGE)]


def test_a_page_typeset_sideways_keeps_pdfiums_order(tmp_path):
    """`pdflscape`'s output: ``/Rotate 90`` *and* the text drawn rotated to match.

    Its lines are vertical in page space, so PDFium's display-frame order is already
    the author's, and the page-space sort key is meaningless there -- applied anyway
    it ranks these four lines 3, 2, 4, 1. Gating on the text's own orientation is the
    only thing standing between this fix and a new bug in the same place.
    """
    path = write(tmp_path, "sideways", rotate=90, sideways=True)

    assert texts(path) == [text for _size, text in PAGE]


def test_a_sideways_page_written_bottom_up_is_still_not_reordered(tmp_path):
    """The same control, stated so it cannot pass by accident: whatever order PDFium
    chooses for a sideways page, we leave it alone."""
    path = write(tmp_path, "sideways_rev", rotate=90, sideways=True, reverse_stream=True)

    assert texts(path) == [text for _size, text in reversed(PAGE)]


def test_an_empty_rotated_page_is_still_empty(tmp_path):
    path = tmp_path / "blank.pdf"
    path.write_bytes(rotated_pdf([], rotate=90))

    stats: dict = {}
    assert list(blocks(path, diagnostics=stats)) == []
    assert stats["pages_without_text"] == 1


# --------------------------------------------------------------------------- #
# the invariant the reorder must not break
# --------------------------------------------------------------------------- #


def test_font_metrics_still_land_on_the_right_character_after_the_sort(tmp_path):
    """``Line.start``/``end`` are indices into PDFium's character array and the size,
    weight, angle and box probes are all made at one of them. The sort moves whole
    `Line` records and never touches an index, so the 18 pt heading must still
    measure 18 pt -- a sort that renumbered anything would report body size here."""
    path = write(tmp_path, "landscape", rotate=90)
    doc = pdfium.PdfDocument(path)
    page = doc[0]
    try:
        found = page_lines(page, 0)
    finally:
        page.close()
        doc.close()

    assert [round(line.size, 1) for line in found] == [size for size, _text in PAGE]


def test_the_spans_still_locate_the_text_on_the_rotated_page(tmp_path):
    """D9: ``start``/``end`` stay a correct locator. They are no longer *ascending*
    across the page after a reorder -- PDFium's array is in its own order and we
    reordered the records, not the array -- so a consumer that assumed monotonic
    offsets is what this test documents.
    """
    path = write(tmp_path, "landscape", rotate=90)
    doc = pdfium.PdfDocument(path)
    page = doc[0]
    try:
        textpage = page.get_textpage()
        found = page_lines(page, 0)
        for line in found:
            assert textpage.get_text_range(line.start, line.end - line.start) == line.text
    finally:
        page.close()
        doc.close()


def test_diagnostics_count_the_pages_that_were_reordered(tmp_path):
    """Rule 3. A repair is not a secret: a caller comparing our output against another
    extractor's needs to know which pages we put back in order and how many."""
    path = write(tmp_path, "landscape", rotate=90)

    stats: dict = {}
    list(blocks(path, diagnostics=stats))

    assert stats["pages_reordered"] == 1


def test_the_repair_reaches_the_public_diagnostics(tmp_path):
    """A count nobody can read is a count nobody acts on: it has to survive the trip
    from `blocks()`'s stats dict to `Diagnostics.notes`."""
    report = diceo.Diagnostics()
    list(diceo.chunk(write(tmp_path, "landscape", rotate=90), diagnostics=report))

    assert any("pages_reordered=1" in note for note in report.notes), report.notes


def test_an_ordinary_document_reports_no_reordering(tmp_path):
    """The other half of rule 3: a diagnostic that fires on every ordinary page is
    noise a caller learns to ignore inside a day."""
    for name, kwargs in (
        ("upright", {"rotate": 0}),
        ("upright_rev", {"rotate": 0, "reverse_stream": True}),
        ("sideways", {"rotate": 90, "sideways": True}),
    ):
        stats: dict = {}
        list(blocks(write(tmp_path, name, **kwargs), diagnostics=stats))

        assert stats["pages_reordered"] == 0, name
