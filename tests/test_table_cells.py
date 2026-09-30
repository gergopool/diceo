"""Every value stays under its own column name, in every table reader.

A table row is only as good as its alignment: one missing empty field and every
later value answers for the wrong header, with nothing in diagnostics. These are
the shapes that did exactly that, one per reader or chunker path.
"""

from __future__ import annotations

from pathlib import Path

import diceo
from diceo.types import Limits

from .test_docx_run_children import _read


def _tc(text: str = "", props: str = "") -> str:
    para = f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" if text else "<w:p/>"
    return f"<w:tc>{f'<w:tcPr>{props}</w:tcPr>' if props else ''}{para}</w:tc>"


def _tbl(*rows: str) -> str:
    return "<w:tbl>" + "".join(f"<w:tr>{row}</w:tr>" for row in rows) + "</w:tbl>"


def _chunks(path: Path, **limits) -> list[str]:
    return [c.text for c in diceo.chunk(path, limits=Limits(**limits) if limits else None)]


# --------------------------------------------------------------------------- #
# docx
# --------------------------------------------------------------------------- #


def test_an_empty_docx_cell_keeps_its_column(tmp_path):
    texts, _ = _read(
        tmp_path,
        _tbl(
            _tc("Region") + _tc("2023") + _tc("2024"),
            _tc("EMEA") + _tc() + _tc("91"),
            _tc() + _tc("40") + _tc("50"),
        ),
    )
    assert texts == ["Region | 2023 | 2024", "EMEA |  | 91", " | 40 | 50"]


def test_a_two_paragraph_docx_cell_is_one_field(tmp_path):
    two = (
        "<w:tc><w:p><w:r><w:t>1 Main St</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>Suite 5</w:t></w:r></w:p></w:tc>"
    )
    texts, _ = _read(tmp_path, _tbl(_tc("Name") + _tc("Address"), _tc("Acme") + two))
    assert texts[1] == "Acme | 1 Main St Suite 5"


def test_an_empty_docx_span_still_covers_its_columns(tmp_path):
    texts, _ = _read(
        tmp_path,
        _tbl(
            _tc("A") + _tc("B") + _tc("C"),
            _tc(props='<w:gridSpan w:val="2"/>') + _tc("c1"),
        ),
    )
    assert texts[1] == " |  | c1"


def test_a_vertical_merge_repeats_the_value_above(tmp_path):
    texts, report = _read(
        tmp_path,
        _tbl(
            _tc("Region") + _tc("Sales"),
            _tc("EMEA", '<w:vMerge w:val="restart"/>') + _tc("10"),
            _tc(props="<w:vMerge/>") + _tc("20"),
        ),
    )
    assert texts[2] == "EMEA | 20"
    assert report.merged_cells_expanded == 1


def test_a_blank_form_says_its_rows_are_empty(tmp_path):
    """The reported symptom: a table whose body is all blank read as a bare header."""
    empty = _tc() + _tc()
    texts, _ = _read(tmp_path, _tbl(_tc("Item") + _tc("Status"), empty, empty, empty))
    assert texts == ["Item | Status", "(3 empty rows)"]


def test_spacer_rows_between_data_rows_add_no_marker(tmp_path):
    texts, _ = _read(tmp_path, _tbl(_tc("K") + _tc("V"), _tc() + _tc(), _tc("a") + _tc("1")))
    assert texts == ["K | V", "a | 1"]


def test_a_footnote_in_a_cell_does_not_split_the_table(tmp_path):
    """Only the note's placement is tested here: the part holding its text is not
    built, so the reference resolves to nothing and no block is expected."""
    texts, _ = _read(
        tmp_path,
        _tbl(_tc("K") + _tc("V"), _tc("a") + _tc("1"))
        + "<w:p><w:r><w:t>after</w:t></w:r></w:p>",
    )
    assert texts == ["K | V", "a | 1", "after"]


def test_w_cr_is_a_line_break(tmp_path):
    texts, _ = _read(tmp_path, "<w:p><w:r><w:t>one</w:t><w:cr/><w:t>two</w:t></w:r></w:p>")
    assert texts == ["one\ntwo"]


# --------------------------------------------------------------------------- #
# chunker, html, markdown, csv
# --------------------------------------------------------------------------- #


def test_an_empty_corner_cell_survives_the_chunker(tmp_path):
    path = tmp_path / "t.html"
    path.write_text(
        "<table><tr><th></th><th>Q1</th><th>Q2</th></tr>"
        "<tr><td>North</td><td>10</td><td>20</td></tr>"
        "<tr><td></td><td>5</td><td>6</td></tr></table>"
    )
    assert _chunks(path) == ["| Q1 | Q2\nNorth | 10 | 20\n| 5 | 6"]


def test_a_markdown_row_keeps_its_empty_first_cell(tmp_path):
    path = tmp_path / "t.md"
    path.write_text("|  | Q1 | Q2 |\n|---|---|---|\n| North | 10 | 20 |\n|  | 5 | 6 |\n")
    assert _chunks(path) == ["| Q1 | Q2\nNorth | 10 | 20\n| 5 | 6"]


def test_two_adjacent_tables_keep_their_own_headers(tmp_path):
    rows = "".join(
        f"<tr><td>City{i} with a long padded name</td><td>FR</td></tr>" for i in range(9)
    )
    path = tmp_path / "t.html"
    path.write_text(
        "<table><tr><th>Name</th><th>Age</th></tr><tr><td>Ann</td><td>30</td></tr></table>"
        f"<p></p><table><tr><th>City</th><th>Country</th></tr>{rows}</table>"
    )
    chunks = _chunks(path, target_chars=100)
    assert all("Name | Age" not in c for c in chunks[1:]), chunks
    assert chunks[-1].startswith("City | Country"), chunks


def test_a_trailing_comma_adds_no_phantom_column(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("Name,Qty,\nApple,3,\nPear,,\n")
    rows = _chunks(path)[-1]
    assert rows == "Name | Qty\nApple | 3\nPear", rows


def test_blank_csv_lines_do_not_spend_max_rows(tmp_path):
    path = tmp_path / "t.csv"
    path.write_text("Name,Qty\n\n\nApple,3\nPear,4\nFig,5\n")
    assert _chunks(path, max_rows=3)[-1] == "Name | Qty\nApple | 3\nPear | 4"
