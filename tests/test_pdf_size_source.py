"""Heading synthesis must survive a producer that scales type with the matrix.

`FPDFText_GetFontSize` returns the *nominal* size from the `Tf` operator. A
producer that writes `/F1 1 Tf` and then scales with `Tm` -- which is what both
real PDFs this repository holds actually do -- makes that number `1.0` for every
character in the document. The font-size census then has one bucket, `fit_styles`
returns `levels={}`, and **not a single heading is emitted for the whole
document**, silently. Measured 2026-07-31: 0 headings on 60 pages of
911-report.pdf and 0 on 30 pages of the IPBES assessment, against 83 and 7 once
the size signal is taken from the rendered glyph box instead.

Nothing in `docs/` could have caught this: all 14 synthetic corpus PDFs are
rendered by our own builder, which emits honest `Tf` sizes. This suite pins the
real-world shape so it cannot regress into invisibility again.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import diceo
from diceo.pdf import blocks
from diceo.pdf.extract import (
    DEGENERATE_SIZE,
    LEVEL_SIZE_TOL,
    MAX_LEVELS,
    Census,
    fit_model,
    fit_styles,
    lines,
)

from .fixtures import matrix_scaled_pdf, mixed_scale_pdf, tiny_pdf


def _tmpdir() -> str:
    return tempfile.mkdtemp()


# --------------------------------------------------------------------------- #
# the defect, pinned
# --------------------------------------------------------------------------- #


def test_the_fixture_really_is_degenerate(tmp_path):
    """Guard the guard: if PDFium ever starts reporting the effective size, this
    test fails and the rest of the module becomes meaningless."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    nominal = {round(line.size, 1) for page in lines(path) for line in page}

    assert nominal == {1.0}, f"fixture no longer reproduces the defect: {nominal}"


def test_nominal_census_alone_finds_no_headings(tmp_path):
    """The mechanism, stated as a test: this census cannot discriminate."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    census = Census()
    for page in lines(path):
        for line in page:
            census.add(round(line.size, 1), line.bold, len(line.text))

    assert fit_styles(census).levels == {}


# --------------------------------------------------------------------------- #
# the fix
# --------------------------------------------------------------------------- #


def test_headings_survive_a_matrix_scaled_producer(tmp_path):
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    found = [b for b in blocks(path) if b.kind == "heading"]
    texts = [b.text for b in found]

    assert "Annual Report 2026" in texts
    assert "Regional Breakdown" in texts


def test_the_two_heading_sizes_get_different_levels(tmp_path):
    """20 pt and 14 pt are two levels, not one. A flat hierarchy would put the
    chunker's boundaries in the wrong place."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    levels = {b.text: b.level for b in blocks(path) if b.kind == "heading"}

    assert levels["Annual Report 2026"] < levels["Regional Breakdown"]


def test_body_text_is_not_promoted(tmp_path):
    """The body size must stay body. Promoting it would make every paragraph a
    chunk boundary."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    headings = [b.text for b in blocks(path) if b.kind == "heading"]

    assert not any("1200 million" in t for t in headings)
    assert not any("EMEA contributed" in t for t in headings)


def test_the_source_switch_is_reported(tmp_path):
    """Rule 3. A silent fallback is how this bug survived in the first place."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    diagnostics: dict = {}
    list(blocks(path, diagnostics=diagnostics))

    assert diagnostics["size_source"] == "em"
    assert diagnostics["heading_levels"] >= 2


def test_heading_levels_counts_levels_and_heading_styles_counts_styles(tmp_path):
    """Two numbers, because they are two different things and one was reported as the
    other. `StyleModel.levels` is keyed by ``(size, bold)``, so its length counts
    *styles*; a document that refits as it streams accumulates many styles mapping onto
    the same few levels. On the real Federal Register that is 31 styles across 49
    refits, and the diagnostic read `heading_levels=31` about a document whose deepest
    emitted heading is level 4 -- an implausible number in the one field that exists so
    a caller can tell whether structure was recovered."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    diagnostics: dict = {}
    list(blocks(path, diagnostics=diagnostics))

    assert diagnostics["heading_levels"] <= MAX_LEVELS
    assert diagnostics["heading_styles"] >= diagnostics["heading_levels"]


def test_no_block_carries_a_level_past_the_reported_count(tmp_path):
    """The reported number has to bound the observable one, or it is decoration."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    diagnostics: dict = {}
    levels = {b.level for b in blocks(path, diagnostics=diagnostics) if b.kind == "heading"}

    assert levels, "fixture is meant to produce headings"
    assert max(levels) <= diagnostics["heading_levels"]


def test_a_normal_pdf_keeps_the_nominal_source(tmp_path):
    path = tmp_path / "plain.pdf"
    path.write_bytes(tiny_pdf(["Quarterly report", "Revenue was 1200 million."]))

    diagnostics: dict = {}
    list(blocks(path, diagnostics=diagnostics))

    assert diagnostics["size_source"] == "nominal"


def test_a_normal_pdf_is_never_probed_for_glyph_boxes(tmp_path):
    """The loose-box probe is one extra FFI call per line. PDF throughput is the
    headline number (47.67 doc/s), so the probe must not fire on documents that
    do not need it -- `em` stays 0.0 and the cost stays zero."""
    path = tmp_path / "plain.pdf"
    path.write_bytes(tiny_pdf(["Quarterly report", "Revenue was 1200 million."]))

    ems = {line.em for page in lines(path) for line in page}

    assert ems == {0.0}


def test_the_probe_fires_on_a_degenerate_pdf(tmp_path):
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    ems = sorted({round(line.em, 1) for page in lines(path) for line in page})

    assert len(ems) == 3, ems  # 20, 14 and 10 pt, rendered
    assert all(value > 0 for value in ems)


def test_it_holds_across_the_calibration_boundary(tmp_path):
    """Calibration buffers four pages. The decision is made once, from that
    window, and must still apply on page 9."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf(pages=9))

    pages_with_headings = {b.page for b in blocks(path) if b.kind == "heading"}

    assert pages_with_headings == set(range(9))


def test_matrix_scaled_figure_labels_do_not_hijack_the_hierarchy(tmp_path):
    """The trap the earlier structure prototype documented, and the reason the probe is
    decided per page rather than per document.

    On `paper-tables.pdf` pages 13-15 the attention-heatmap figures are typeset at
    a unit font size with a scaled matrix, so their labels measure a glyph box of
    27 against a body of 8.9. Anything that switches to glyph boxes because it saw
    *some* unit sizes promotes `'Input-Input Layer5 The Law will never beperfect'`
    to h1. Here the page also holds honest `Tf` sizes, so it is not degenerate, the
    probe never fires, and the nominal signal -- which correctly sees 1.0 and
    rejects the label -- decides."""
    path = tmp_path / "mixed.pdf"
    path.write_bytes(mixed_scale_pdf())

    diagnostics: dict = {}
    found = [b for b in blocks(path, diagnostics=diagnostics) if b.kind == "heading"]

    assert diagnostics["size_source"] == "nominal"
    assert not any("Input-Input" in b.text for b in found), [b.text for b in found]
    assert any("Attention Is All You Need" in b.text for b in found)


def test_the_probe_is_decided_per_page_not_per_document(tmp_path):
    """The mechanism behind the test above, asserted directly: a page carrying any
    honestly-sized text is never probed, so its lines cannot enter the em census."""
    path = tmp_path / "mixed.pdf"
    path.write_bytes(mixed_scale_pdf())

    ems = {line.em for page in lines(path) for line in page}

    assert ems == {0.0}


def test_rotated_text_is_still_excluded(tmp_path):
    """`page_lines` negates the size of rotated text so it can never be a
    heading. That sentinel lives on `size`, and the em source must not reopen
    the hole -- a 20 pt stamp down the side of a page would otherwise take
    level 1 and push every real heading one level deeper."""
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf())

    for page in lines(path):
        for line in page:
            assert line.size > 0  # nothing rotated in this fixture
    # and the guard itself, directly
    from dataclasses import replace

    page_one = next(iter(lines(path)))
    rotated = replace(page_one[0], size=-1.0)
    from diceo.pdf.extract import StyleModel, _is_heading

    model = StyleModel(body_size=11.7, size_source="em", levels={(23.4, False): 1})
    assert _is_heading(rotated, model) is None


# --------------------------------------------------------------------------- #
# fitting a *measured* signal is not the same as fitting an authored one
# --------------------------------------------------------------------------- #


def _degenerate_nominal() -> Census:
    """What a matrix-scaling producer's nominal census actually looks like."""
    census = Census()
    census.add(1.0, False, 5000)
    return census


def test_neighbouring_sizes_cluster_into_one_level():
    """Nominal sizes are a handful of authored values; glyph-box heights are a
    measurement, and two font families at the same point size measure a little
    differently. Without clustering each becomes its own heading level."""
    census = Census()
    census.add(23.4, False, 18)
    census.add(23.3, False, 20)
    census.add(16.4, False, 18)
    for _ in range(40):
        census.add(11.7, False, 60)  # body

    model = fit_model(_degenerate_nominal(), census)

    assert model.size_source == "em"
    assert model.levels[(23.4, False)] == model.levels[(23.3, False)] == 1
    assert model.levels[(16.4, False)] == 2


def test_the_clustering_tolerance_is_relative():
    """0.5 pt apart is the same level at 24 pt and two levels at 6 pt."""
    assert LEVEL_SIZE_TOL * 23.4 > 0.5
    assert LEVEL_SIZE_TOL * 6.0 < 0.5


def test_styles_past_the_cap_are_dropped_not_flattened():
    """`fit_styles` clamps every extra style to level 6. For a measured signal
    that turns glyph-box noise a hair above body size into headings. Dropping
    them is the conservative choice: a false heading puts a chunk boundary in
    the middle of a paragraph."""
    census = Census()
    for size in (30.0, 26.0, 22.0, 18.0, 15.0, 13.0):
        census.add(size, False, 18)
    for _ in range(40):
        census.add(11.7, False, 60)

    model = fit_model(_degenerate_nominal(), census)

    assert len(model.levels) == MAX_LEVELS
    assert max(model.levels.values()) == MAX_LEVELS
    assert (13.0, False) not in model.levels


def test_an_honest_census_is_left_completely_alone():
    """The nominal path must be bit-for-bit what it was, or every published
    retrieval number moves."""
    census = Census()
    census.add(20.0, False, 18)
    census.add(14.0, False, 18)
    for _ in range(40):
        census.add(10.0, False, 60)
    em = Census()
    em.add(23.4, False, 18)

    assert fit_model(census, em).levels == fit_styles(census).levels
    assert fit_model(census, em).size_source == "nominal"


def test_an_empty_em_census_cannot_trigger_the_switch():
    """Only switch when the alternative signal actually discriminates."""
    degenerate = Census()
    degenerate.add(1.0, False, 500)

    model = fit_model(degenerate, Census())

    assert model.size_source == "nominal"


def test_a_flat_em_census_still_switches():
    """Deliberately, and it took a failing test to see why.

    A flat measured census looks like no improvement, so the first version of this
    refused to switch on one. But a degenerate nominal census is *proof* that the
    nominal signal carries nothing, so a flat measured census is no worse -- and
    the window may just be front matter with one type size, which is exactly the
    document that needs the refit. Refusing here is refusing to ever learn."""
    degenerate = Census()
    degenerate.add(1.0, False, 500)
    flat = Census()
    flat.add(11.7, False, 500)

    model = fit_model(degenerate, flat)

    assert model.size_source == "em"
    assert model.levels == {}, "no headings yet -- but the door is open"


def test_degeneracy_threshold_is_documented_where_it_is_used():
    assert DEGENERATE_SIZE == 2.0


def test_a_size_between_census_buckets_still_classifies():
    """The lookup has to be a *band*, not an exact key.

    `Tf` sizes are a handful of authored values, so `levels[(size, bold)]` finds
    them. Glyph-box heights are a measurement: a heading two pages later measures
    21.6 where the calibration window saw 21.7, and an exact lookup returns None.
    Measured on 911-report.pdf: 13 of 17 distinct em heights across 60 pages were
    absent from the census, including the one carrying 105,117 characters."""
    census = Census()
    census.add(21.7, False, 18)
    for _ in range(40):
        census.add(9.3, False, 60)

    model = fit_model(_degenerate_nominal(), census)

    assert model.level_for(21.7, False) == 1
    assert model.level_for(21.6, False) == 1, "a hair off must still be a heading"
    assert model.level_for(9.3, False) is None, "body text must not be promoted"
    assert model.level_for(9.5, False) is None, "nor a hair above body"


def test_the_band_does_not_swallow_the_gap_between_two_levels():
    census = Census()
    census.add(21.7, False, 18)
    census.add(14.5, False, 18)
    for _ in range(40):
        census.add(9.3, False, 60)

    model = fit_model(_degenerate_nominal(), census)

    assert model.level_for(21.7, False) == 1
    assert model.level_for(14.5, False) == 2
    assert model.level_for(18.0, False) is None, "halfway between is neither"


def test_the_nominal_lookup_stays_an_exact_match():
    """Bands are for measured signals only. Widening the nominal lookup would
    reclassify lines in every document the corpus measures."""
    census = Census()
    census.add(20.0, False, 18)
    for _ in range(40):
        census.add(10.0, False, 60)

    model = fit_styles(census)

    assert model.level_for(20.0, False) == 1
    assert model.level_for(19.9, False) is None


def test_the_model_is_refitted_as_a_degenerate_document_streams():
    """A report whose front matter is unrepresentative.

    This is 911-report.pdf's shape, reduced: the calibration window is a cover
    and a contents list, so the body size fitted from it (6.4) is not the body
    size of the document (9.3, and 105,117 of its characters). Pages 0-31 of that
    report are *all* front matter, which is why widening the window fixes
    nothing. The model has to be revised once the body actually starts."""
    front = [(6, "official government edition"), (6, "printed by the authority")]
    body = [
        (16, "Chapter One: The System Was Blinking Red"),
        (10, "The threat reporting in the summer of 2001 was unprecedented."),
        (10, "Analysts warned of a coming attack on American interests abroad."),
    ]
    path = Path(_tmpdir()) / "report.pdf"
    path.write_bytes(matrix_scaled_pdf([front] * 4 + [body] * 12))

    found = [b.text for b in blocks(path) if b.kind == "heading"]

    assert any("Chapter One" in t for t in found), (
        "the model never revised itself: headings after the calibration window "
        f"are invisible. Got {found}"
    )
    assert not any("threat reporting" in t for t in found)


def test_a_one_line_display_style_does_not_consume_a_heading_level():
    """The cover page is not the document's hierarchy.

    Measured on 911-report.pdf pages 30-130: the working section-heading style is
    em 10.9 with 699 characters over 20-odd lines, and the chapter style is 14.5.
    Both were pushed out of MAX_LEVELS by 21.7, 18.1 and 12.7 -- cover and
    half-title styles carrying **24, 24 and 27 characters on one line each**."""
    census = Census()
    census.add(21.7, False, 24)  # cover
    census.add(18.1, False, 24)  # half title
    census.add(12.7, False, 27)  # imprint
    for _ in range(20):
        census.add(10.9, False, 35)  # the real section headings
    for _ in range(24):
        census.add(14.5, False, 22)  # chapter titles
    for _ in range(500):
        census.add(9.3, False, 530)  # body -- takes the census past the threshold

    model = fit_model(_degenerate_nominal(), census)

    assert model.level_for(10.9, False) is not None, (
        f"the working heading style was pruned out: {model.levels}"
    )
    assert model.level_for(14.5, False) is not None
    assert model.level_for(14.5, False) < model.level_for(10.9, False)


def test_rare_styles_are_kept_while_the_census_is_still_small():
    """The same pruning applied to a calibration window is destructive: a style
    that has been seen twice in four pages is not rare, the *window* is small.
    The structure-synthesis experiment measured heading F1 collapsing
    0.568 -> 0.246 that way."""
    census = Census()
    census.add(21.7, False, 24)
    census.add(14.5, False, 22)
    for _ in range(6):
        census.add(9.3, False, 60)

    model = fit_model(_degenerate_nominal(), census)

    assert model.level_for(21.7, False) is not None
    assert model.level_for(14.5, False) is not None


def test_the_largest_style_is_never_pruned():
    """A document whose only heading is its title must still get that title."""
    census = Census()
    census.add(30.0, False, 20)
    for _ in range(500):
        census.add(9.3, False, 530)

    model = fit_model(_degenerate_nominal(), census)

    assert model.level_for(30.0, False) == 1


def test_refitting_is_confined_to_the_measured_path(tmp_path):
    """An honest document must be classified once, from its calibration window,
    exactly as it was before this fallback existed."""
    path = tmp_path / "plain.pdf"
    path.write_bytes(tiny_pdf(["Quarterly report", "Revenue was 1200 million."], pages=20))

    diagnostics: dict = {}
    list(blocks(path, diagnostics=diagnostics))

    assert diagnostics["size_source"] == "nominal"
    assert diagnostics.get("model_refits", 0) == 0


# --------------------------------------------------------------------------- #
# through the public API
# --------------------------------------------------------------------------- #


def test_the_public_api_recovers_the_structure(tmp_path):
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf(pages=4))

    joined = "\n".join(piece.text for piece in diceo.chunk(path))

    assert "Annual Report 2026" in joined
    assert "Regional Breakdown" in joined


def test_the_public_api_reports_the_recovered_signal(tmp_path):
    path = tmp_path / "matrix.pdf"
    path.write_bytes(matrix_scaled_pdf(pages=4))

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any("glyph boxes" in note for note in report.notes), report.notes


def test_a_document_with_no_headings_at_all_says_so(tmp_path):
    """The observable that would have caught this bug in one glance: 0 heading
    levels on a real report is a defect, and the caller can now see it."""
    path = tmp_path / "flat.pdf"
    path.write_bytes(tiny_pdf(["one line of prose that is quite long indeed"]))

    report = diceo.Diagnostics()
    list(diceo.chunk(path, diagnostics=report))

    assert any("headings=0" in note for note in report.notes), report.notes
