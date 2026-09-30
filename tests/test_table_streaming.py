"""Table packing keeps only the current group without changing its boundaries."""

import pytest

from diceo import Block, Diagnostics, Limits, Locator, chunk_blocks


@pytest.mark.parametrize("page,row", [(-1, -1), (3, 0), (3, -1)])
def test_table_groups_preserve_header_confidence_and_location(page, row):
    header = Block("table_row", "Name | Amount", locator=Locator(page=page, row=row))
    blocks = [header, *(Block("table_row", f"item{i} | 123456") for i in range(4))]
    chunks = list(chunk_blocks(blocks, limits=Limits(target_chars=30)))
    text = "\n".join(chunk.text for chunk in chunks)
    assert text.count(header.text) == (1 if page >= 0 and row < 0 else len(chunks))
    assert all(chunk.locator == header.locator for chunk in chunks)
    assert [chunk.char_start for chunk in chunks] == [
        sum(len(previous.text) for previous in chunks[:index]) for index in range(len(chunks))
    ]
    assert all(text.count(f"item{i} | 123456") == 1 for i in range(4))


def test_single_rows_adjacent_tables_and_following_headings_keep_the_old_cuts():
    blocks = [
        Block("table_row", "single", locator=Locator(row=0)),
        Block("table_row", "Key | Value", locator=Locator(row=0)),
        Block("table_row", "second | 20", locator=Locator(row=1)),
        Block("heading", "After", 1),
        Block("paragraph", "Body"),
    ]
    chunks = list(chunk_blocks(blocks, limits=Limits(target_chars=20)))
    assert [chunk.text for chunk in chunks] == [
        "single\nKey | Value\nsecond | 20",
        "After\nBody",
    ]
    assert chunks[0].title == ""
    assert chunks[1].title == "After"


def test_wide_groups_keep_caption_once_when_groups_merge():
    header = " | ".join(["First column", "Second column", "C", "D", "E", "F"])
    blocks = [Block("caption", "Measurements"), Block("table_row", header)]
    blocks.extend(Block("table_row", f"value{i} | 1 | 2 | 3 | 4 | 5") for i in range(8))
    chunks = list(chunk_blocks(blocks, limits=Limits(target_chars=110)))
    assert len(chunks) > 1
    assert all(chunk.text.count("Measurements") == 1 for chunk in chunks)
    assert all(chunk.text.count(header) == 1 for chunk in chunks)
    assert all("value" in chunk.text for chunk in chunks)


def test_first_chunk_and_zero_cap_do_not_consume_a_whole_table():
    read = []

    def rows():
        yield Block("table_row", "Name | Amount", locator=Locator(row=0))
        for index in range(10_000):
            read.append(index)
            yield Block("table_row", f"item{index} | 123456", locator=Locator(row=index + 1))

    chunks = chunk_blocks(rows(), limits=Limits(target_chars=100))
    first = next(chunks)
    chunks.close()
    assert first.text.startswith("Name | Amount\nitem0")
    assert len(read) < 20

    read.clear()
    report = Diagnostics()
    assert not list(
        chunk_blocks(rows(), limits=Limits(target_chars=100, max_chars=0), diagnostics=report)
    )
    assert len(read) < 20
    assert report.chunks == report.chars == 0
    assert report.truncated
