"""The sheet summary chunk dropped its trailing column names, and said "15" anyway.

`_summary_text` paired labels with dtypes through
`zip(profile.header, profile.dtypes, strict=False)`. The two lists are built from
different widths — the header from the header row, the dtypes from the *modal* body
width — so a header row wider than its body silently truncated the list of columns
to the shorter of the two. `strict=False` there was not tolerating a mismatch, it
was hiding one.

Measured on `worldbank-pink-sheet-monthly.xlsx` (public-office corpus), sheet
*Index Weights*: header 15 labels, dtypes 12, so the summary printed
`Columns (15):` and then listed **twelve**, dropping `Share of\\nfood index` and two
unnamed columns. The sheet summary is the chunk that answers "what is in this
file", so a column name absent from it is a column nobody can find.

The fix is upstream of the rendering: the dtype vector is now computed over
`len(header)` columns rather than over the modal body width, so the two are the
same length by construction (columns past the body get `empty`, or a real dtype if
some row does reach that far), and the `zip` is `strict=True` — an invariant
violation should be loud, not silently truncating.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from diceo.sheets import SheetProfile, _summary_text, iter_sheet_chunks

from .fixtures import _CONTENT_TYPES

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _book(sheet_body: str) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        f'Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook xmlns="{_NS}" '
        f'xmlns:r="{_REL}"><sheets><sheet name="S" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>",
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL}/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet xmlns="{_NS}">'
        f"<sheetData>{sheet_body}</sheetData></worksheet>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _text(reference: str, value: str) -> str:
    return f'<c r="{reference}" t="inlineStr"><is><t>{value}</t></is></c>'


def _number(reference: str, value: str) -> str:
    return f'<c r="{reference}"><v>{value}</v></c>'


def _wide_header_book(labels: list[str], body_columns: int, body_rows: int = 7) -> bytes:
    """A header row wider than the table under it -- the shape that breaks the pairing.

    The trailing columns are genuinely empty in the body, which is exactly how a
    real sheet reaches this state: a column that is named and reserved but not yet
    filled in, or (the World Bank case) header labels that overhang the numbers.
    """
    rows = [
        '<row r="1">'
        + "".join(_text(f"{chr(65 + i)}1", label) for i, label in enumerate(labels))
        + "</row>"
    ]
    for number in range(2, 2 + body_rows):
        cells = _text(f"A{number}", "EMEA") + "".join(
            _number(f"{chr(65 + i)}{number}", str(100 + number + i))
            for i in range(1, body_columns)
        )
        rows.append(f'<row r="{number}">{cells}</row>')
    return _book("".join(rows))


def _summary(path: Path) -> str:
    return next(
        chunk.text for chunk in iter_sheet_chunks(path) if chunk.kind == "sheet_summary"
    )


# --------------------------------------------------------------------------- #
# the loss
# --------------------------------------------------------------------------- #


def test_a_header_wider_than_its_body_keeps_every_column_name(tmp_path):
    """Observed before the fix: `Columns (5):` followed by three entries, with
    `owner` and `compliance_status` absent from the summary chunk entirely."""
    path = tmp_path / "wide-header.xlsx"
    path.write_bytes(
        _wide_header_book(["region", "revenue", "units", "owner", "compliance_status"], 3)
    )

    summary = _summary(path)

    assert "owner" in summary, summary
    assert "compliance_status" in summary, summary


def test_the_declared_column_count_matches_the_columns_listed(tmp_path):
    """The count line and the list came from different lists, so they disagreed --
    the summary asserted five columns and then named three."""
    path = tmp_path / "wide-header.xlsx"
    path.write_bytes(
        _wide_header_book(["region", "revenue", "units", "owner", "compliance_status"], 3)
    )

    summary = _summary(path)

    assert "Columns (5):" in summary, summary
    assert sum(1 for line in summary.splitlines() if line.startswith("  - ")) == 5, summary


def test_a_column_with_no_body_values_is_typed_empty(tmp_path):
    """Not `text`, and not absent: `empty` is the honest answer for a named column
    the sample never saw a value in, and it is what tells a caller the column is a
    label rather than data."""
    path = tmp_path / "wide-header.xlsx"
    path.write_bytes(_wide_header_book(["region", "revenue", "reserved"], 2))

    summary = _summary(path)

    assert "  - reserved [empty]" in summary, summary


def test_a_header_narrower_than_its_body_is_unaffected(tmp_path):
    """The other direction was never broken and must stay that way: unnamed trailing
    columns still get a dtype and an `(unnamed)` label."""
    path = tmp_path / "narrow-header.xlsx"
    path.write_bytes(_wide_header_book(["region", "revenue"], 4))

    summary = _summary(path)

    assert "Columns (4):" in summary, summary
    assert summary.count("(unnamed)") == 2, summary


def test_an_ordinary_sheet_is_byte_identical(tmp_path):
    """The fix must not move the summary of a rectangular sheet -- every published
    xlsx retrieval number was measured on that stream."""
    path = tmp_path / "clean.xlsx"
    path.write_bytes(_wide_header_book(["region", "revenue", "units"], 3))

    summary = _summary(path)

    assert summary.splitlines()[:5] == [
        "Sheet: S",
        "Columns (3):",
        "  - region [text]",
        "  - revenue [number]",
        "  - units [number]",
    ], summary


# --------------------------------------------------------------------------- #
# the invariant itself
# --------------------------------------------------------------------------- #


def test_the_pairing_refuses_a_mismatched_profile():
    """`strict=False` made a broken profile render as a shorter, plausible one. With
    `strict=True` the same profile raises, which is the point: nothing downstream
    can tell a truncated column list from a genuinely short one."""
    profile = SheetProfile(
        sheet="S",
        title=None,
        header=["a", "b", "c"],
        header_rows=1,
        first_data_row=2,
        confidence="clean",
        dtypes=["text"],
    )

    try:
        _summary_text(profile, 10)
    except ValueError:
        return
    raise AssertionError("a header/dtype length mismatch was rendered instead of raising")


def test_every_profile_the_reader_builds_satisfies_it(tmp_path):
    """The guard above is only safe because `_profile` pads by construction. This
    walks the shapes that used to disagree and asserts the invariant directly."""
    from diceo.sheets import SAMPLE_ROWS, Row, _profile

    shapes = [
        [Row("S", 1, ["a", "b", "c", "d"]), *(Row("S", n, ["x", "1"]) for n in range(2, 9))],
        [Row("S", 1, ["a", "b"]), *(Row("S", n, ["x", "1", "2", "3"]) for n in range(2, 9))],
        [Row("S", n, ["only text here"]) for n in range(1, 9)],
        [Row("S", 1, ["a", "b"]), Row("S", 2, ["c", "d"])],
        [],
    ]
    for sample in shapes:
        profile = _profile("S", sample[:SAMPLE_ROWS], (None, None))
        if profile.header:
            assert len(profile.header) == len(profile.dtypes), profile
