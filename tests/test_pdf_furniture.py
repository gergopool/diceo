"""Running heads and feet must not reach the index.

experiment 033 (adjudication) measured the cost of not doing this:
**31.6% of PDF chunks** carried the document code and page number, 17.8% of `table_row`
blocks were the page footer rather than a table, and 120 prose blocks had the boilerplate
spliced *into the middle of a sentence*. Docling sits at 0.02 occurrences per page.

The suppression already existed in an earlier structure-synthesis prototype, which the
shipping API never imported. This is that logic, ported to the path that actually runs,
and improved in one
way: `blocks()` already buffers the first `calibration_pages` pages to fit the font model,
so furniture learned there can be suppressed on **page 1 too** rather than leaking until a
key has been seen twice.

    uv run pytest tests/test_pdf_furniture.py -q
"""

from __future__ import annotations

import pytest

from diceo.pdf.extract import Line, _Furniture, _furniture_key

_HEADER = " | ".join(f"Quarterly metric {i} in kWh per pallet" for i in range(14))
_ROWS = [" | ".join(f"r{r}c{c}" for c in range(14)) for r in range(6)]


def _line(text: str, page: int, top: float) -> Line:
    return Line(
        text=text,
        page=page,
        start=0,
        end=len(text),
        size=9.0,
        bold=False,
        bbox=(72.0, top - 10.0, 300.0, top),
    )


def _page(page: int, *texts: str) -> list[Line]:
    return [_line(t, page, 700.0 - 20.0 * i) for i, t in enumerate(texts)]


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("REP-EN-2000 Quarterly Review 3", "REP-EN-2000 Quarterly Review 47"),
        ("Internal 1", "Internal 12"),
        ("Page 3 of 44", "Page 41 of 44"),
    ],
)
def test_a_page_number_does_not_make_two_footers_different(a: str, b: str):
    """Keyed on digit-normalised text, so 'Internal 1' and 'Internal 12' are one key."""
    assert _furniture_key(a, "b") == _furniture_key(b, "b")


def test_distinct_footers_stay_distinct():
    assert _furniture_key("Internal 1", "b") != _furniture_key("Confidential 1", "b")


def test_a_repeated_head_and_foot_are_learned():
    pages = [
        _page(0, "REP-EN-2000 Quarterly Review", "Real prose on page one.", "Internal 1"),
        _page(1, "REP-EN-2000 Quarterly Review", "Real prose on page two.", "Internal 2"),
        _page(2, "REP-EN-2000 Quarterly Review", "Real prose on page three.", "Internal 3"),
    ]
    _learner = _Furniture()
    _learner.observe_all(pages)
    known = _learner.known
    assert _furniture_key("REP-EN-2000 Quarterly Review", "t") in known
    assert _furniture_key("Internal 1", "b") in known
    assert _furniture_key("Real prose on page one.", "t") not in known


def test_body_text_repeated_by_coincidence_is_not_furniture():
    """A sentence that happens to repeat is not in the margin, so it must survive."""
    pages = [
        _page(0, "Head", "The same sentence appears twice.", "Foot 1"),
        _page(1, "Head", "The same sentence appears twice.", "Foot 2"),
    ]
    _learner = _Furniture()
    _learner.observe_all(pages)
    known = _learner.known
    assert _furniture_key("The same sentence appears twice.", "t") not in known


def test_a_line_seen_on_only_one_page_is_not_furniture():
    pages = [
        _page(0, "Unique head", "Body.", "Foot 1"),
        _page(1, "Other head", "Body.", "Foot 2"),
    ]
    _learner = _Furniture()
    _learner.observe_all(pages)
    known = _learner.known
    assert _furniture_key("Unique head", "t") not in known
    assert _furniture_key("Other head", "t") not in known


def test_a_long_margin_line_is_not_furniture():
    """A running head is short. A full sentence at the top of a page is content."""
    long_line = (
        "This is a genuine opening sentence that runs on well past ninety "
        "characters and therefore cannot be a running header at all."
    )
    pages = [_page(0, long_line, "Body."), _page(1, long_line, "Body.")]
    _l = _Furniture()
    _l.observe_all(pages)
    assert _furniture_key(long_line, "t") not in _l.known


def test_a_single_page_document_learns_nothing():
    _l = _Furniture()
    _l.observe_all([_page(0, "Head", "Body.", "Foot 1")])
    assert _l.known == set()


def test_suppression_never_empties_a_page():
    """A document whose every page is the same two short lines is degenerate but real --
    a form, a slide export. Deleting all of it would be the worst possible outcome, so
    when every line on a page looks like furniture, none of it is removed."""
    pages = [_page(i, "Hello from diceo.", "The revenue was 1200 million.") for i in range(3)]
    learner = _Furniture()
    learner.observe_all(pages)
    assert learner.known, "both lines do look like furniture"
    kept, dropped = learner.strip(pages[0])
    assert dropped == 0
    assert len(kept) == 2


def test_a_head_and_foot_around_real_content_are_removed():
    pages = [
        _page(i, "REP-EN-2000 Review", "First body line.", "Second body line.", f"Internal {i}")
        for i in range(3)
    ]
    learner = _Furniture()
    learner.observe_all(pages)
    kept, dropped = learner.strip(pages[0])
    assert dropped == 2
    assert [line.text for line in kept] == ["First body line.", "Second body line."]


# --------------------------------------------------------------------------- #
# what review found in the first version
# --------------------------------------------------------------------------- #


def test_a_head_that_starts_after_the_cover_pages_is_still_learned():
    """The bug that mattered. Keys were learned **only** from the calibration buffer,
    so a document with a cover page and a table of contents -- which is every real
    report -- had its running head start on page 5 and got *zero* suppression. The
    synthetic corpus carries its header from page 1, which is exactly why the first
    measurement looked clean.
    """
    from diceo.pdf.extract import _Furniture

    learner = _Furniture()
    front = [_page(0, "Cover title only"), _page(1, "Contents", "1. Intro ... 3")]
    body = [
        _page(i, "REP-EN-2000 Review", f"Body of page {i}.", f"Internal {i}")
        for i in range(2, 7)
    ]
    kept_per_page = []
    for page in front + body:
        learner.observe(page)
        kept, _ = learner.strip(page)
        kept_per_page.append([line.text for line in kept])
    # Pages 2 and 3 teach the key; from page 4 on the head and foot are gone.
    assert "REP-EN-2000 Review" not in kept_per_page[4]
    assert "Body of page 4." in kept_per_page[4]
    # And the leak before the key was known is counted, not hidden.
    assert learner.leaked >= 1


def test_a_page_number_does_not_teach_a_key_that_deletes_a_year_heading():
    """`_furniture_key` maps every digit run to `#`, so a bare page number `1` and a
    genuine section title `2025` both key to `#`. Keying on the *edge side* as well
    keeps them apart: a footer teaches a bottom-key, a title at the top is a top-key."""
    from diceo.pdf.extract import _Furniture

    learner = _Furniture()
    for index in range(3):
        page = _page(index, "Real prose line.", "More prose.", str(index + 1))
        learner.observe(page)
    year_page = _page(9, "2025", "The year in review.", "9")
    kept, dropped = learner.strip(year_page)
    texts = [line.text for line in kept]
    assert "2025" in texts, "a section title was deleted by a page-number key"
    assert "9" not in texts, "the page number should still go"


def test_a_table_row_is_never_adopted_as_a_caption():
    """`_take_caption` excluded headings but not table rows, so a one-row table leaves a
    short `table_row` in `pending` and the next wide table would adopt it as its caption
    -- replicating another table's *data* into every row group, with the wrong locator."""
    from diceo.chunker import Limits as _Limits  # noqa: F401  (import shape check)
    from diceo.chunker import _Packer
    from diceo.types import Block, Diagnostics, Limits, Locator

    packer = _Packer("doc", Limits(target_chars=600), Diagnostics())
    packer.pending.append(Block("table_row", "Availability | N/A", 0, Locator()))
    assert packer._take_caption() is None
    assert len(packer.pending) == 1, "the row must stay where it was"

    packer.pending[:] = [Block("paragraph", "Table 2: energy intensity by site", 0, Locator())]
    assert packer._take_caption() == "Table 2: energy intensity by site"
    assert packer.pending == [], "a real caption is moved, not copied"


@pytest.mark.xfail(
    reason="Block carries no table identity, so two adjacent tables are one run and the "
    "first row of the first becomes the header for both. Needs a Block-level change.",
    strict=True,
)
def test_two_adjacent_tables_do_not_share_a_header():
    """Recorded as a known gap rather than left undiscovered. With no separating block,
    `close_table` sees one run of `table_row`s and takes `table[0]` as the header, so a
    one-row table immediately followed by a wide table donates its row as the header of
    both -- and it is then repeated into every group of the second table."""
    from diceo.chunker import chunk_blocks
    from diceo.types import Block, Limits, Locator

    blocks = [
        Block("table_row", "Availability | N/A", 0, Locator()),
        Block("table_row", _HEADER, 0, Locator()),
        *[Block("table_row", row, 0, Locator()) for row in _ROWS],
    ]
    chunks = list(chunk_blocks(blocks, limits=Limits(target_chars=600)))
    replicated = sum(chunk.text.count("Availability | N/A") for chunk in chunks)
    assert replicated == 1, f"the first table's row became a shared header ({replicated}x)"


def test_two_adjacent_tables_are_not_merged_by_a_shared_header_prefix():
    """The merge tested `last.text.startswith(prefix)`, so a second table whose header
    is a prefix of the first table's text was absorbed into it -- losing the second
    table's header and mislabelling its rows with the first table's locator."""
    from diceo.chunker import chunk_blocks
    from diceo.types import Block, Limits, Locator

    wide = " | ".join(f"Reporting column {i} in kWh per pallet" for i in range(14))
    blocks = [
        Block("paragraph", "Table A: first", 0, Locator()),
        Block("table_row", wide, 0, Locator()),
        Block("table_row", " | ".join(f"a{i}" for i in range(14)), 0, Locator()),
        Block("paragraph", "Some prose between the two tables.", 0, Locator()),
        Block("paragraph", "Table B: second", 0, Locator()),
        Block("table_row", wide, 0, Locator()),
        Block("table_row", " | ".join(f"b{i}" for i in range(14)), 0, Locator()),
    ]
    text = "\n".join(
        chunk.text for chunk in chunk_blocks(blocks, limits=Limits(target_chars=600))
    )
    assert "Table A: first" in text
    assert "Table B: second" in text, "the second table lost its caption to a merge"
    assert text.count(wide) >= 2, "the second table lost its header to a merge"


# --------------------------------------------------------------------------- #
# the default, which a measurement decided against
# --------------------------------------------------------------------------- #


def test_furniture_suppression_is_off_by_default(tmp_path):
    """Rule 4 decided this. Suppression is real noise removal -- 31.6% of chunks carried
    the running head -- but held-out PDF `gold@5` falls 0.845 -> 0.836, monotone in how
    much is removed, which takes PDF from -0.9pp against PyMuPDF4LLM (inside the 1pp
    equivalence threshold) to -1.8pp (outside it). So it ships as an option, not a
    default."""
    from tests import fixtures

    path = tmp_path / "with-furniture.pdf"
    path.write_bytes(
        fixtures.tiny_pdf(["REP-EN-2000 Review", "Real body text here.", "Internal 1"], pages=4)
    )

    default = "\n".join(b.text for b in diceo_extract(path))
    assert "REP-EN-2000" in default, "the default must not silently drop text"

    opted_in = "\n".join(b.text for b in diceo_extract(path, suppress_furniture=True))
    assert "Real body text here." in opted_in
    assert len(opted_in) <= len(default)


def diceo_extract(path, **limit_kwargs):
    import diceo
    from diceo.types import Limits

    return diceo.extract(path, limits=Limits(**limit_kwargs))
