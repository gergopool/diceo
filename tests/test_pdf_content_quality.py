"""PDF quality regressions found on public UNESCO, ECB and statistics documents."""

from __future__ import annotations

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from diceo.pdf.extract import Line, StyleModel, _emit_page, blocks

from .fixtures import stamped_image_pdf, tiny_pdf


def _line(text, box, *, start=0, size=9.0, bold=False):
    return Line(text, 0, start, start + len(text), size, bold, box)


def _emit(page, *, tables=True):
    return list(_emit_page(page, StyleModel(11.0), tables=tables, median_advance=4.0))


def test_replacement_characters_are_counted_on_their_source_pages(tmp_path, monkeypatch):
    path = tmp_path / "mapping.pdf"
    path.write_bytes(
        tiny_pdf(["A broken mapping still occupies the same character offsets."], pages=3)
    )
    get_text = pdfium_c.FPDFText_GetText

    def broken_mapping(textpage, start, count, buffer):
        result = get_text(textpage, start, count, buffer)
        buffer[2] = buffer[4] = 0xFFFD
        return result

    monkeypatch.setattr(pdfium_c, "FPDFText_GetText", broken_mapping)
    stats = {}
    output = list(blocks(path, page_range=range(1, 3), diagnostics=stats))
    assert sum(block.text.count("\ufffd") for block in output) == 4
    assert stats["replacement_chars"] == 4
    assert stats["pages_with_replacement_chars"] == {1: 2, 2: 2}


def test_a_sparse_mixed_page_finds_a_large_image_after_many_text_objects(tmp_path):
    path = tmp_path / "chart.pdf"
    path.write_bytes(
        stamped_image_pdf("Chart 18: Financial caption with more than forty characters")
    )
    doc = pdfium.PdfDocument(path)
    page = doc[0]
    try:
        image = pdfium_c.FPDFPage_GetObject(page.raw, 0)
        # Move the raster above the caption, then past the old 24-object scan cap.
        pdfium_c.FPDFPageObj_Transform(image, 0.5, 0, 0, 0.5, 150, 200)
        assert pdfium_c.FPDFPage_RemoveObject(page.raw, image)
        for _ in range(32):
            rect = pdfium_c.FPDFPageObj_CreateNewRect(10, 10, 1, 1)
            pdfium_c.FPDFPage_InsertObject(page.raw, rect)
        pdfium_c.FPDFPage_InsertObject(page.raw, image)
        assert pdfium_c.FPDFPage_GenerateContent(page.raw)
        doc.save(tmp_path / "mixed.pdf")
    finally:
        page.close()
        doc.close()
    stats = {}
    output = list(blocks(tmp_path / "mixed.pdf", diagnostics=stats))
    assert output and "caption" in output[0].text.lower()
    assert stats["pages_image_mixed"] == [0]
    assert stats["pages_image_only"] == []


def test_large_images_with_an_overlapping_text_layer_are_not_routed_as_unread(tmp_path):
    path = tmp_path / "ocr.pdf"
    path.write_bytes(
        stamped_image_pdf("Chart 18: The image has an existing text layer that is readable.")
    )
    stats = {}
    list(blocks(path, diagnostics=stats))
    assert stats["pages_image_mixed"] == []
    assert stats["pages_image_only"] == []


def test_ordinary_short_text_pages_do_not_inspect_image_objects(tmp_path, monkeypatch):
    from diceo.pdf import extract

    path = tmp_path / "short.pdf"
    path.write_bytes(tiny_pdf(pages=3))

    def unexpected(*args):
        raise AssertionError("ordinary text should not walk PDF page objects")

    monkeypatch.setattr(extract, "_page_has_unread_image", unexpected)
    assert list(blocks(path))


def test_detached_column_labels_move_before_their_data_and_keep_their_words():
    page = [
        _line("Under 19 20-24 25-29 30-34 35-39 40 and", (186, 705, 441, 721)),
        _line("over", (416, 698, 436, 704)),
        _line("1970 1934 1.0 26.5 49.2 18.5 4.2 0.5 25.6", (95, 675, 492, 683), start=100),
        _line("2023 727 0.6 6.5 26.0 36.5 23.9 6.6 31.0", (95, 567, 492, 575), start=200),
        _line("Year", (95, 714, 117, 722), start=300),
        _line("Number", (139, 730, 177, 738), start=310),
        _line("of births", (139, 715, 177, 721), start=320),
        _line("Mean age", (460, 736, 504, 744), start=330),
        _line("bearing first", (454, 721, 510, 730), start=340),
        _line("child", (471, 707, 494, 715), start=360),
    ]
    output = _emit(page)
    assert output[0].kind == "table_row" and output[0].row == 0
    assert "Number of births" in output[0].text
    assert "Mean age bearing first child" in output[0].text
    assert "40 and over" in output[0].text
    assert [block.row for block in output[1:]] == [1, 2]
    assert output[1].start == 100 and output[2].start == 200


def test_numeric_data_without_a_label_band_is_never_marked_as_a_header():
    page = [
        _line("Participants 9844 312 641 400 569 724", (95, 675, 492, 683)),
        _line("Strawberry 39 0.2 2.2 0.0 1.1 7.5 0.0", (95, 567, 492, 575)),
    ]
    output = _emit(page)
    assert [block.row for block in output] == [-1, -1]
    assert [block.text for block in output] == [line.text for line in page]


def test_year_columns_are_retained_as_headers_of_a_narrow_geometric_table():
    page = [
        _line("Site Q4 2023 Q1 2024 Q2 2024", (57, 716, 478, 724)),
        _line("Ashcombe Depot n/a 59.0137 16.2383", (57, 700, 476, 708)),
        _line("Ashcombe South Depot n/a 22.1876 51.3884", (57, 684, 476, 692)),
    ]
    output = _emit(page)
    assert [block.row for block in output] == [0, 1, 2]
    assert output[0].text == page[0].text


def test_earlier_narrow_data_rows_cannot_be_adopted_as_column_labels():
    page = [
        _line("Ashcombe Depot n/a 59.0137 16.2383", (57, 716, 476, 724)),
        _line("Ashcombe South Depot n/a 22.1876 51.3884", (57, 700, 476, 708)),
        _line("Ashcombe Rail Terminal 93.7784 81.6586 33.2124", (57, 684, 476, 692)),
    ]
    assert [block.row for block in _emit(page)] == [-1, -1, -1]


def test_wide_headers_cover_rows_with_different_label_indents():
    page = [
        _line(
            "Generator Test Domain All domain Baike QA RuATD Bulgarian News",
            (76, 731, 519, 736),
        ),
        _line("Train Domain Acc F1 Acc F1 Acc F1 Acc F1", (104, 727, 504, 731)),
        _line("All domains 95.9 96.1 79.7 83.0 70.4 76.2 72.4 77.1", (104, 718, 518, 722)),
        _line("Baike QA 66.8 75.3 98.0 98.0 62.0 72.4 57.1 69.5", (76, 712, 518, 717)),
    ]
    output = list(_emit_page(page, StyleModel(11.0), tables=True, median_advance=10.0))
    assert output[0].row == 0
    assert "Generator Test Domain" in output[0].text
    assert "Train Domain Acc F1" in output[0].text
    assert [block.row for block in output[1:]] == [1, 2]


def test_wrapped_caption_ends_at_the_body_font_and_column_boundary():
    page = [
        _line("Figure 1: Collection workflow.", (71, 451, 525, 459), size=9.9),
        _line("Industry research and academic papers.", (71, 440, 525, 448), size=9.9),
        _line("Standard benchmarks continue here.", (71, 425, 290, 432), size=10.9),
    ]
    output = _emit(page, tables=False)
    assert [block.kind for block in output] == ["caption", "para"]
    assert output[0].text.endswith("academic papers.")
    assert output[1].text == "Standard benchmarks continue here."


def test_an_inline_table_reference_does_not_consume_its_following_paragraph():
    page = [
        _line("Table 12 for details) depicts performance.", (71, 451, 290, 459)),
        _line("The results remain part of the discussion.", (71, 440, 290, 448)),
    ]
    assert [block.kind for block in _emit(page, tables=False)] == ["para"]
