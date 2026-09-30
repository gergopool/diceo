"""Counters describe delivered content, including capped and explicitly closed iterators."""

import diceo
from diceo import Block, Diagnostics, Limits, Locator, chunk_blocks


def test_additive_counters_preserve_positional_diagnostics():
    report = diceo.Diagnostics("pdf", 1, 0, 0, 42)
    assert report.chars == 42
    assert report.pages_image_mixed == report.pages_unreadable_text == 0
    assert not report.needs_ocr


def test_capped_chunk_counter_does_not_include_withheld_chunk():
    for cap, expected in ((0, 0), (1, 1)):
        report = Diagnostics()
        chunks = list(
            chunk_blocks(
                [Block("paragraph", "one two three " * 10)],
                limits=Limits(target_chars=20, max_chars=cap),
                diagnostics=report,
            )
        )
        assert report.chunks == len(chunks) == expected
        assert report.chars == sum(len(c.text) for c in chunks)


def test_closed_sheet_stream_counts_the_chunk_already_delivered():
    report = Diagnostics()
    stream = diceo.chunk(b"name,value\nA,42\nB,17\n", name="rows.csv", diagnostics=report)
    first = next(stream)
    stream.close()
    assert report.chunks == 1 and report.chars == len(first.text)


def test_public_character_counters_count_text_not_html_markup():
    source = b"<main><h1>Title</h1><p>Actual content.</p></main>"
    report = Diagnostics()
    chunks = list(diceo.chunk(source, name="page.html", diagnostics=report))
    assert chunks[0].text == "Title\nActual content."
    assert report.chars == sum(len(c.text) for c in chunks)
    report = Diagnostics()
    blocks = list(diceo.extract(source, name="page.html", diagnostics=report))
    assert [b.text for b in blocks] == ["Title", "Actual content."]
    assert report.chars == sum(len(b.text) for b in blocks)


def test_pdf_data_row_is_not_repeated_as_header_without_confidence():
    blocks = [
        Block("table_row", text, locator=Locator(page=0))
        for text in ("Alpha 10 20", "Beta 30 40", "Gamma 50 60", "Delta 70 80")
    ]
    text = "\n".join(c.text for c in chunk_blocks(blocks, limits=Limits(target_chars=20)))
    assert text.count("Alpha 10 20") == 1


def test_to_text_preserves_an_adjacent_table_boundary():
    blocks = [
        Block("table_row", "A | B", locator=Locator(row=0)),
        Block("table_row", "1 | 2", locator=Locator(row=1)),
        Block("table_row", "C | D", locator=Locator(row=0)),
    ]
    assert "1 | 2\n\nC | D" in "".join(diceo.to_text(blocks))


def test_zero_page_budget_reports_unread_pages_without_calling_them_scans(tmp_path):
    import pytest

    canvas = pytest.importorskip("reportlab.pdfgen.canvas")
    path = tmp_path / "one.pdf"
    writer = canvas.Canvas(str(path))
    writer.drawString(40, 700, "A readable page.")
    writer.save()
    for reader in (diceo.extract, diceo.chunk):
        report = Diagnostics()
        assert list(reader(path, limits=Limits(max_pages=0), diagnostics=report)) == []
        assert report.truncated == ["max_pages=0"] and report.lost_data
        assert report.pages_without_text == 0 and not report.needs_ocr
