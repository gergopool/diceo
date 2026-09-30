"""A source cell can exceed the complete retrieval window."""

import pytest

from diceo import Block, Diagnostics, Limits, Locator, chunk_blocks


@pytest.mark.parametrize("separator", [" | ", "\t"])
def test_long_cells_keep_labels_columns_row_context_and_every_value(separator):
    header = separator.join(["Request", "Modification", "Reason", "Empty"])
    values = ["95. A", "alpha " * 150, "beta " * 90, ""]
    locator = Locator(page=7, row=116)
    extra = {"source_url": "https://example.org/original.docx"}
    chunks = list(
        chunk_blocks(
            [
                Block("table_row", header, locator=Locator(page=7, row=0)),
                Block("table_row", separator.join(values), locator=locator),
                Block("table_row", separator.join(["96. B", "ordinary", "other", ""])),
            ],
            doc_id="original",
            extra=extra,
            limits=Limits(target_chars=180),
        )
    )
    split = [chunk for chunk in chunks if chunk.locator == locator]
    assert len(split) > 2
    recovered = [[], []]
    for chunk in split:
        assert len(chunk) <= 180
        assert chunk.text.startswith(header + "\n")
        assert chunk.doc_id == "original" and chunk.extra is extra
        cells = chunk.text[len(header) + 1 :].split(separator)
        assert len(cells) == 4 and cells[0] == "95. A" and cells[3] == ""
        assert bool(cells[1]) != bool(cells[2])
        for index in (1, 2):
            recovered[index - 1].append(cells[index])
    for index in (1, 2):
        assert "".join("".join(recovered[index - 1]).split()) == "".join(values[index].split())
    assert sum(chunk.text.count("96. B") for chunk in chunks) == 1


def test_single_and_unconfirmed_pdf_rows_split_without_repeated_data_as_labels():
    value = "unmapped " * 100
    locator = Locator(page=3, row=-1)
    for rows in (
        [Block("table_row", value, locator=locator)],
        [Block("table_row", value, locator=locator), Block("table_row", "body")],
    ):
        chunks = list(chunk_blocks(rows, limits=Limits(target_chars=100)))
        assert all(len(chunk) <= 100 for chunk in chunks)
        text = "".join(chunk.text for chunk in chunks)
        assert text.count("unmapped") == 100
        assert all(chunk.locator == locator for chunk in chunks if "unmapped" in chunk.text)


def test_empty_edge_columns_are_not_shifted_during_a_split():
    chunks = list(
        chunk_blocks(
            [
                Block("table_row", " | Description | Reference | "),
                Block("table_row", " | " + "authored " * 100 + " | Ref95 | "),
            ],
            limits=Limits(target_chars=100),
        )
    )
    assert len(chunks) > 2
    for chunk in chunks:
        cells = chunk.text.split("\n", 1)[1].split(" | ")
        assert len(cells) == 4 and cells[0] == cells[3] == ""
        assert cells[2] == "Ref95" and "authored" in cells[1]


def test_header_larger_than_target_is_preserved_and_reported_and_cap_stops_splitting(
    monkeypatch,
):
    import diceo.chunker as chunker

    split = chunker._iter_split_long
    consumed = []

    def counted(text, target):
        for piece in split(text, target):
            consumed.append(piece)
            yield piece

    monkeypatch.setattr(chunker, "_iter_split_long", counted)
    header = "Full authored column label " * 10
    row = "value " * 1000
    report = Diagnostics()
    chunks = list(
        chunk_blocks(
            [Block("table_row", header), Block("table_row", row)],
            limits=Limits(target_chars=100, max_chars=1),
            diagnostics=report,
        )
    )
    assert len(chunks) == report.chunks == 1
    assert report.chars == len(chunks[0])
    assert chunks[0].text.startswith(header.strip() + "\n")
    assert any("table_context_over_target" in note for note in report.notes)
    assert report.truncated == ["max_chars=1"]
    assert len(consumed) == 2  # delivered first, withheld next; the remaining cell stays lazy.


def test_normal_table_rows_keep_existing_chunks():
    chunks = list(
        chunk_blocks(
            [
                Block("table_row", "Name | Amount"),
                *(Block("table_row", f"item{i} | 123456") for i in range(4)),
            ],
            limits=Limits(target_chars=30),
        )
    )
    assert [chunk.text for chunk in chunks] == [
        "Name | Amount\nitem0 | 123456\nitem1 | 123456",
        "Name | Amount\nitem2 | 123456\nitem3 | 123456",
    ]


@pytest.mark.parametrize("row", ["a | b | c | d | e", " |  |  |  | "])
@pytest.mark.parametrize("table_chars", [None, 8])
def test_context_only_row_survives_when_labels_and_columns_exhaust_the_target(row, table_chars):
    header = "A | B | C | D | E"
    report = Diagnostics()
    chunks = list(
        chunk_blocks(
            [Block("table_row", header), Block("table_row", row)],
            limits=Limits(target_chars=16, table_chars=table_chars),
            diagnostics=report,
        )
    )
    assert len(chunks) == 1
    assert chunks[0].text.startswith(header + "\n")
    assert chunks[0].text.split("\n", 1)[1].strip() == row.strip()
    assert report.chunks == 1 and report.chars == len(chunks[0])
    if row.startswith("a") or table_chars == 8:
        assert any("table_context_over_target" in note for note in report.notes)


def test_wide_short_cells_remain_one_row_when_their_labels_cannot_fit():
    # Real NASA timeline geometry: 72 monthly columns, each source value is short.
    header = " | ".join("2026" for _ in range(72))
    row = " | ".join("Earth to Mars" for _ in range(72))
    report = Diagnostics()
    chunks = list(
        chunk_blocks(
            [Block("table_row", header), Block("table_row", row)],
            limits=Limits(target_chars=600),
            diagnostics=report,
        )
    )
    assert [chunk.text for chunk in chunks] == [header + "\n" + row]
    assert any("table_context_over_target" in note for note in report.notes)
