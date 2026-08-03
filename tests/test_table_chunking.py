"""A wide table's chunks must each say what the table is about.

The one format diceo loses on is DOCX, and it loses to both remaining rivals by
almost exactly the same margin (experiment 026, docling). The
diagnosis is in experiment 030 (docx mechanism): recovering the
per-query outcomes from the embedding cache showed that of the 52 held-out
``wide_table_cell`` queries, the 14 whose answer row happened to land in the chunk
that kept the table's caption score **14/14**, while the 38 whose row landed in a
later chunk score **8/38** (Fisher p = 1.8e-7).

The caption is usually the only place the *metric* is named -- "energy intensity",
"kWh/pallet" -- so a chunk without it has nothing for such a query to match, however
many data rows it holds.

    uv run pytest tests/test_table_chunking.py -q
"""

from __future__ import annotations

import pytest

from diceo.chunker import chunk_blocks
from diceo.types import Block, Limits, Locator

_CAPTION = "Table 2: energy intensity by site, all reporting bases and quarters, in kWh/pallet"
_HEADER = " | ".join(
    ["Site", "Site code"]
    + [
        f"Q{q} 2023 {basis}"
        for q in (1, 2, 3)
        for basis in ("Actual", "Planned", "Forecast", "Audited")
    ]
)
_SITES = [
    "Ashcombe Depot",
    "Ashcombe South Depot",
    "Calderhythe Terminal",
    "Barrowfield Hub",
    "Elsmere Energy Plant",
    "Granby Yard",
]
_ROWS = [
    " | ".join([site, f"X{i:02d}"] + [f"{10 * i + j}.5" for j in range(12)])
    for i, site in enumerate(_SITES)
]
_PROSE = "Reporting for solar self-consumption follows PRO-112, revised before Q3."


def _wide_table_blocks(
    caption: str | None = _CAPTION, prose: str | None = _PROSE
) -> list[Block]:
    blocks: list[Block] = []
    if prose is not None:
        blocks.append(Block("paragraph", prose, 0, Locator()))
    if caption is not None:
        blocks.append(Block("paragraph", caption, 0, Locator()))
    blocks.append(Block("table_row", _HEADER, 0, Locator()))
    blocks.extend(Block("table_row", row, 0, Locator()) for row in _ROWS)
    return blocks


def _chunks(blocks: list[Block], target: int = 600):
    return list(chunk_blocks(blocks, limits=Limits(target_chars=target)))


def test_every_chunk_holding_a_wide_table_row_names_the_table():
    """The fix. Before it, 96 of 122 held-out wide-table chunks (79%) had no caption."""
    chunks = _chunks(_wide_table_blocks())
    stranded = [
        chunk.index
        for chunk in chunks
        if any(site in chunk.text for site in _SITES) and _CAPTION not in chunk.text
    ]
    assert not stranded, f"chunks {stranded} hold data rows but never name the table"


def test_the_caption_is_not_duplicated_within_one_chunk():
    """Carrying it must not mean carrying it twice."""
    for chunk in _chunks(_wide_table_blocks()):
        assert chunk.text.count(_CAPTION) <= 1, chunk.text


def test_a_table_with_no_caption_still_chunks():
    """Real documents put the caption below the table, in another style, or nowhere.
    The absent case has to be a clean no-op rather than a crash or a stray prefix."""
    chunks = _chunks(_wide_table_blocks(caption=None))
    assert chunks
    body = "\n".join(chunk.text for chunk in chunks)
    for site in _SITES:
        assert site in body


def test_prose_before_a_table_is_not_mistaken_for_its_caption():
    """A caption is a short label. Replicating a *sentence* into every row group would
    put unrelated prose in every chunk -- the contamination 030 measured at a 0.10
    cosine cost. Sentence-final punctuation is the signal, and it is language-neutral."""
    chunks = _chunks(_wide_table_blocks(caption=None, prose=_PROSE))
    carrying = [chunk.index for chunk in chunks if _PROSE in chunk.text]
    assert len(carrying) <= 1, f"prose replicated into chunks {carrying}"


def test_a_narrow_table_is_unchanged():
    """The gate is `_wants_one_row_per_group`. A narrow table must not start carrying
    a repeated caption -- its rows already fit in one chunk with the caption present."""
    header = "Site | Q1 2023 | Q2 2023 | Q3 2023"
    rows = [f"{site} | {i}.1 | {i}.2 | {i}.3" for i, site in enumerate(_SITES)]
    blocks = [
        Block(
            "paragraph", "Table 1: actual energy intensity by site and quarter", 0, Locator()
        ),
        Block("table_row", header, 0, Locator()),
        *[Block("table_row", row, 0, Locator()) for row in rows],
    ]
    chunks = _chunks(blocks)
    assert len(chunks) == 1
    assert chunks[0].text.count("Table 1:") == 1


@pytest.mark.parametrize("kind", ["heading", "slide_title"])
def test_a_heading_is_not_consumed_as_a_caption(kind: str):
    """Headings are carried by the trail machinery and must stay there."""
    blocks = [
        Block(kind, "3. Cold-chain integrity", 1, Locator()),
        Block("table_row", _HEADER, 0, Locator()),
        *[Block("table_row", row, 0, Locator()) for row in _ROWS],
    ]
    chunks = _chunks(blocks)
    assert sum(chunk.text.count("3. Cold-chain integrity") for chunk in chunks) == 1
