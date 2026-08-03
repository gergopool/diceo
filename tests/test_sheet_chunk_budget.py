"""A wide sheet's header prefix silently blows the caller's chunk target.

`iter_sheet_chunks` sizes a row group by the length of its *rows* and then prefixes
the header line for free. That is the only sensible arithmetic -- on the real World
Bank commodity sheet the header is 1,781 characters against a 600-character target,
so counting it would leave room for no rows at all -- but it means a caller who
asked for 600 gets **2,344**, and nothing said so.

Measured 2026-07-31 on `worldbank-pink-sheet-monthly.xlsx` (72 columns, 786 monthly
rows): at target 600 the header line is repeated **780 times**, which is **56.6% of
every character diceo emits for that workbook** and **68.5% of the row-group text
specifically** (the two denominators differ because the first includes the per-sheet
summary chunks; the diagnostic reports the second). Docling and markitdown emit
0.1-2% duplicated text. It is not removable -- experiment 016
measured a group without its header at `wide_table_cell` 0.065 against 0.538, and a
`column: value` rendering was simulated and is *more* expensive on every sheet that
is not both wide and sparse. So the fix is rule 3's fix: say it.

This matters to a caller with a token budget. 2,344 characters is past a 512-token
embedding window; silently exceeding it truncates their vectors, not ours.
"""

from __future__ import annotations

import diceo
from diceo.sheets import SheetDiagnostics, iter_sheet_chunks

from .fixtures import tiny_xlsx


def _wide_xlsx(columns: int = 60, rows: int = 8) -> bytes:
    """A sheet whose header line alone is longer than a small chunk target."""
    header = [f"Commodity {i} measured in dollars per metric ton" for i in range(columns)]
    body = [[f"{i}.{j}" for j in range(columns)] for i in range(rows)]
    return tiny_xlsx([header, *body])


def test_a_wide_header_pushes_groups_past_the_target(tmp_path):
    """The behaviour itself, pinned. Not a bug to fix by shrinking the group: with
    a header this wide there is no group size that both fits and carries a row."""
    path = tmp_path / "wide.xlsx"
    path.write_bytes(_wide_xlsx())

    groups = [
        c
        for c in iter_sheet_chunks(path, target=600, diagnostics=SheetDiagnostics())
        if c.kind == "row_group"
    ]

    assert groups
    assert max(len(c.text) for c in groups) > 600 * 2


def test_the_overshoot_is_reported(tmp_path):
    """Rule 3. A caller with a 512-token embedder needs to know before indexing."""
    path = tmp_path / "wide.xlsx"
    path.write_bytes(_wide_xlsx())

    report = SheetDiagnostics()
    list(iter_sheet_chunks(path, target=600, diagnostics=report))

    over = [n for n in report.notes if "over_target" in n]
    assert over, report.notes
    assert "header" in over[0]


def test_the_note_carries_the_numbers_not_just_a_warning(tmp_path):
    path = tmp_path / "wide.xlsx"
    path.write_bytes(_wide_xlsx())

    report = SheetDiagnostics()
    chunks = [
        c
        for c in iter_sheet_chunks(path, target=600, diagnostics=report)
        if c.kind == "row_group"
    ]
    note = next(n for n in report.notes if "over_target" in n)
    biggest = max(len(c.text) for c in chunks)

    # The worst case is what decides whether a caller's window is blown.
    assert str(biggest) in note, note
    assert "600" in note, note


def test_a_narrow_sheet_says_nothing(tmp_path):
    """The note has to be rare, or it is noise that gets filtered out and then the
    one time it matters nobody reads it."""
    path = tmp_path / "narrow.xlsx"
    path.write_bytes(tiny_xlsx())

    report = SheetDiagnostics()
    list(iter_sheet_chunks(path, target=1800, diagnostics=report))

    assert not [n for n in report.notes if "over_target" in n]


def test_it_reaches_the_public_diagnostics(tmp_path):
    path = tmp_path / "wide.xlsx"
    path.write_bytes(_wide_xlsx())

    report = diceo.Diagnostics()
    list(diceo.chunk(path, limits=diceo.Limits(target_chars=600), diagnostics=report))

    assert any("over_target" in n for n in report.notes), report.notes


def test_the_header_repetition_cost_is_reported(tmp_path):
    """The other half of the same fact: how much of the output is the repeated
    header. On the real World Bank sheet this is 56.6%, and a caller paying per
    embedded token is entitled to see it."""
    path = tmp_path / "wide.xlsx"
    path.write_bytes(_wide_xlsx())

    report = SheetDiagnostics()
    list(iter_sheet_chunks(path, target=600, diagnostics=report))

    note = next((n for n in report.notes if "header_repeated" in n), None)
    assert note is not None, report.notes
    assert "%" in note


# --------------------------------------------------------------------------- #
# the summary chunk, which must never be proportional to the document
# --------------------------------------------------------------------------- #
#
# The header prefix above is a cost that buys retrieval. This one bought nothing:
# `_summary_text` inlined its sample rows verbatim, so a sheet whose rows are large
# was copied into its own preamble. Measured 2026-08-01 on a 200 MB single-line CSV:
# **1.19 GB peak RSS** and two chunks totalling **400,000,152 characters** -- twice
# the file -- with `lost_data` False. `Limits` could not stop it either: the oversized
# chunk is emitted first and only the *next* one is suppressed, so `max_chars` fires
# after the memory has been spent, and `max_rows=1` never applied to the summary at
# all. A summary is a preamble; the cap is what makes it one.


#: The longest sample cell and the longest rendered sample row in the corpus (48 xlsx
#: files, 253 sheets, measured 2026-08-01). The budget sits above both on purpose, so
#: that no real spreadsheet's summary moves; these numbers are here so that a future
#: tightening has to face them.
_CORPUS_LONGEST_SAMPLE_CELL = 579
_CORPUS_LONGEST_SAMPLE_ROW = 5_253


def test_the_summary_does_not_copy_a_pathological_row(tmp_path):
    """The defect: output proportional to input, from the *summary* chunk.

    A single-cell CSV is the minimal shape of it -- one row, no header, so the row
    lands in the sample and is inlined whole. Asserted as a ratio rather than a
    constant because the failure is unboundedness: before the cap this file came back
    at 2.00x, and a 200 MB one at 2.00x of 200 MB.
    """
    payload = b"a" * 200_000
    report = diceo.Diagnostics()

    pieces = list(diceo.chunk(payload, name="one-long-row.csv", diagnostics=report))

    summary = next(piece for piece in pieces if "sheet_summary" in piece.kinds)
    assert len(summary.text) < 2_000, f"the summary grew with the file: {len(summary.text)}"
    emitted = sum(len(piece.text) for piece in pieces)
    assert emitted < len(payload) * 1.05, f"{emitted} chars out of {len(payload)} bytes"


def test_the_row_itself_still_reaches_the_index_in_full(tmp_path):
    """The cap is not allowed to be a loss. What the summary drops is a *copy*."""
    payload = b"a" * 200_000

    pieces = list(diceo.chunk(payload, name="one-long-row.csv"))

    rows = [piece for piece in pieces if "sheet_row" in piece.kinds]
    assert sum(len(piece.text) for piece in rows) == 200_000


def test_the_shortened_sample_is_reported_without_claiming_a_loss(tmp_path):
    """Rule 3 for a cap that bites, and rule 3 pointed the other way.

    The note has to be there -- a budget applied in silence is the thing this whole
    module exists about. `lost_data` must *not* be there: it is the boolean the API
    tells callers to branch on, and nothing is missing from their index.
    """
    report = diceo.Diagnostics()

    list(diceo.chunk(b"a" * 200_000, name="one-long-row.csv", diagnostics=report))

    note = next((n for n in report.notes if "summary_samples_shortened" in n), None)
    assert note is not None, report.notes
    assert "row group" in note, "the note must say where the full row still is"
    assert not report.lost_data, "a shortened *duplicate* is not a loss"


def test_a_sample_cell_the_size_of_the_corpus_worst_case_is_untouched(tmp_path):
    """The budget is sized from the corpus, so the corpus has to fit under it.

    The longest sample cell in 253 real sheets is 579 characters and the longest
    rendered sample row is 5,253. Both go through unchanged, and nothing is reported
    -- that is what "no real spreadsheet's summary moves" means, pinned.
    """
    cell = "z" * _CORPUS_LONGEST_SAMPLE_CELL
    # 26 columns, because `tiny_xlsx` numbers them `chr(65 + i)` and runs out of
    # letters after Z. 579 + 25*186 + 25 separators renders to 5,304 characters, just
    # past the widest sample row in the corpus.
    filler = ["w" * 186] * 25
    path = tmp_path / "worst-real.xlsx"
    path.write_bytes(
        tiny_xlsx([["label", *filler], [cell, *filler], ["1", *filler], ["2", *filler]])
    )
    report = diceo.Diagnostics()

    pieces = list(diceo.chunk(path, diagnostics=report))

    summary = next(piece for piece in pieces if "sheet_summary" in piece.kinds)
    assert cell in summary.text, "a real sheet's longest sample cell was cut"
    assert not any("summary_samples_shortened" in n for n in report.notes), report.notes


def test_a_row_of_many_tiny_cells_is_bounded_too(tmp_path):
    """The per-cell cap alone is not enough, which is why there are two.

    A row of 200,000 one-character cells has no long cell in it and renders to 800,000
    characters, so the line budget is the one that has to catch it -- and it has to
    catch it *while building the line*, not by joining it first (rule 2).
    """
    payload = b",".join([b"x"] * 200_000)
    report = diceo.Diagnostics()

    pieces = list(diceo.chunk(payload, name="many-cells.csv", diagnostics=report))

    summary = next(piece for piece in pieces if "sheet_summary" in piece.kinds)
    assert len(summary.text) < 20_000, len(summary.text)
    assert any("summary_samples_shortened" in n for n in report.notes), report.notes
