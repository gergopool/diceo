"""No control character may reach `chunk.text`, from any format.

`chunk.text` is what a caller writes to a database and sends to an embedder. A NUL
byte is rejected outright by PostgreSQL's `text` type, has to be escaped in JSON, and
tokenises to noise. Experiment 028 closed this for the plain-text reader; the PDF path
was never covered, and measured here before the fix a PDF whose text layer contains
`\\x00`, `\\x01` and `\\x1b` handed all three straight through to the caller:

    'BeforeNUL and \\x01SOH and \\x1bESC after.\\x00'

Tab, newline and carriage return are kept — they are layout the readers emit on
purpose (`w:tab`, `w:br`, line joins).

The cross-format test is the point of this module: it is a *contract*, so it is
asserted over every format rather than only over the one that was caught breaking it.
"""

from __future__ import annotations

import diceo
from diceo.pdf import blocks as pdf_blocks

from .fixtures import (
    _CONTENT_TYPES,
    _zip,
    every_format,
    tiny_pdf,
    tiny_xlsx,
    write_every_format,
)

#: Everything a caller cannot store or embed. Tab, LF and CR are deliberate layout.
FORBIDDEN = frozenset(
    {chr(code) for code in range(0x20) if code not in (0x09, 0x0A, 0x0D)} | {chr(0x7F)}
)

_DIRTY = "Before\x00NUL and \x01SOH and \x1bESC after."


def _offenders(text: str) -> set[str]:
    return {ch for ch in text if ch in FORBIDDEN}


def test_a_pdf_text_layer_full_of_control_bytes_is_cleaned(tmp_path):
    path = tmp_path / "control.pdf"
    path.write_bytes(tiny_pdf([_DIRTY]))

    joined = "".join(piece.text for piece in diceo.chunk(path))

    assert not _offenders(joined), sorted(hex(ord(c)) for c in _offenders(joined))


def test_the_surrounding_words_survive(tmp_path):
    """Stripping must not take the text with it."""
    path = tmp_path / "control.pdf"
    path.write_bytes(tiny_pdf([_DIRTY]))

    joined = "".join(piece.text for piece in diceo.chunk(path))

    for word in ("Before", "NUL", "SOH", "ESC", "after"):
        assert word in joined, joined


def test_the_removal_is_counted(tmp_path):
    """Rule 3: characters that were in the document and are not in the output."""
    path = tmp_path / "control.pdf"
    path.write_bytes(tiny_pdf([_DIRTY]))

    diagnostics: dict = {}
    list(pdf_blocks(path, diagnostics=diagnostics))

    assert diagnostics["control_chars_removed"] == 3, diagnostics


def test_a_clean_pdf_counts_none(tmp_path):
    path = tmp_path / "clean.pdf"
    path.write_bytes(tiny_pdf(["Nothing unusual here."]))

    diagnostics: dict = {}
    list(pdf_blocks(path, diagnostics=diagnostics))

    assert diagnostics["control_chars_removed"] == 0


def test_tabs_and_newlines_are_not_control_characters(tmp_path):
    """DOCX emits `\\t` for `w:tab` and `\\n` for `w:br`, and HTML rows are
    tab-separated. A blanket strip of C0 would quietly destroy every table row."""
    paths = write_every_format(tmp_path)

    joined = "".join(piece.text for piece in diceo.chunk(paths[".html"]))

    assert "\t" in joined, "HTML table rows are tab-separated"


def test_no_format_leaks_a_control_character(tmp_path):
    """The contract, over every format diceo reads. A new reader that leaks one
    fails here rather than in a caller's database."""
    paths = write_every_format(tmp_path)

    leaked = {}
    for suffix in every_format():
        joined = "".join(piece.text for piece in diceo.chunk(paths[suffix]))
        bad = _offenders(joined)
        if bad:
            leaked[suffix] = sorted(hex(ord(c)) for c in bad)

    assert not leaked, leaked


# --------------------------------------------------------------------------- #
# when the strip takes the whole document with it
# --------------------------------------------------------------------------- #


def test_a_document_that_was_only_control_characters_says_so(tmp_path):
    """Stripping is right; stripping everything and reporting a clean run is not.

    4,096 NUL bytes named ``.csv`` or ``.txt`` came back as **zero chunks**, a
    ``control_chars_removed=4096`` *note*, and ``lost_data`` False. A caller running
    the documented handler -- ``if report.lost_data: warn()`` -- saw nothing, and the
    document is simply not in their index. Same shape as an empty file, one layer
    further in.
    """
    for suffix in (".txt", ".csv"):
        path = tmp_path / f"nul{suffix}"
        path.write_bytes(b"\x00" * 4096)
        report = diceo.Diagnostics()

        pieces = list(diceo.chunk(path, diagnostics=report))

        assert not pieces, f"{suffix}: expected no chunks from a file of NULs"
        assert report.lost_data, f"{suffix}: a document vanished and nothing said so"
        assert any("control_chars_removed" in entry for entry in report.truncated), (
            f"{suffix}: {report.truncated}"
        )


def test_a_stray_control_character_is_still_only_a_note(tmp_path):
    """The other half, and the reason the case above is narrow. 93 files in the
    fixture corpus carry a stray control character in otherwise healthy text; a form
    feed is not a loss, and raising `lost_data` for one would make the flag useless."""
    path = tmp_path / "stray.txt"
    path.write_bytes(b"Revenue was 1200 million.\x00 Next year, more.\n")
    report = diceo.Diagnostics()

    pieces = list(diceo.chunk(path, diagnostics=report))

    assert pieces
    assert not report.lost_data, report.as_dict()
    assert any("control_chars_removed" in note for note in report.notes), report.notes


# --------------------------------------------------------------------------- #
# the escape that is allowed to carry one
# --------------------------------------------------------------------------- #


def _xlsx_with_shared_string(value: str) -> bytes:
    """A workbook whose one cell resolves through ``sharedStrings.xml``.

    `tiny_xlsx` writes ``inlineStr``, and the two paths decode in different
    functions, so a fixture that exercises only one of them proves half the
    contract.
    """
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/{kind}" Target="{target}"/>'
        "</Relationships>"
    )
    return _zip(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": rels.format(kind="officeDocument", target="xl/workbook.xml"),
            "xl/workbook.xml": f'<?xml version="1.0"?><workbook {ns} {rel_ns}><sheets>'
            f'<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": rels.format(
                kind="worksheet", target="worksheets/sheet1.xml"
            ).replace(
                "</Relationships>",
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/sharedStrings" '
                'Target="sharedStrings.xml"/></Relationships>',
            ),
            "xl/sharedStrings.xml": f'<?xml version="1.0"?><sst {ns} count="2" '
            f'uniqueCount="2"><si><t>region</t></si><si><t>{value}</t></si></sst>',
            "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet {ns}>'
            f'<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row>'
            f'<row r="2"><c r="A2" t="s"><v>1</v></c></row></sheetData></worksheet>',
        }
    )


def test_a_sheet_escape_cannot_smuggle_a_control_character(tmp_path):
    """The gap the cross-format sweep above could not see.

    ``every_format()`` builds *clean* fixtures, so the sweep only catches a reader
    that invents a control character -- not one that faithfully decodes the caller's.
    A spreadsheet is the one format that can carry a NUL **legally**: XML cannot
    hold one, so ECMA-376 18.4.12 defines ``_x0000_`` to smuggle it, and
    `unescape_cell` turned it straight back into `chr(0)`.

    Measured before the fix, on both cell paths: ``lost_data`` False, no note, and
    ``'evil\\x00NUL\\x1b[31mRED'`` in `Chunk.embed_text` -- which is an exception
    inside the caller's `INSERT`, not inside diceo. The ESC is the same defect
    wearing a different hat: ``diceo sheet.xlsx --text`` wrote a raw terminal
    escape to stdout.
    """
    dirty = "evil_x0000_NUL_x001B_[31mRED"

    for label, payload in (
        ("shared", _xlsx_with_shared_string(dirty)),
        ("inline", tiny_xlsx([["region"], [dirty]])),
    ):
        path = tmp_path / f"{label}.xlsx"
        path.write_bytes(payload)
        report = diceo.Diagnostics()

        joined = "".join(piece.text for piece in diceo.chunk(path, diagnostics=report))

        assert not _offenders(joined), (label, sorted(hex(ord(c)) for c in _offenders(joined)))
        # Removed, and said so -- rule 3 applies to characters as much as to pages.
        assert any("control_chars_removed=2" in note for note in report.notes), report.notes
        assert "RED" in joined, "the surrounding text must survive the strip"


def test_a_sheet_keeps_the_escape_that_separates_two_lines(tmp_path):
    """The control on the test above. ``_x000D_`` is how a cell holding two lines is
    stored, and CR is *not* in the stripped set -- a blanket strip would weld the two
    lines into one token that neither line's query can find."""
    path = tmp_path / "twoline.xlsx"
    path.write_bytes(tiny_xlsx([["note"], ["line1_x000D_line2"]]))

    joined = "".join(piece.text for piece in diceo.chunk(path))

    assert "line1" in joined and "line2" in joined
    assert "_x000D_" not in joined, "the escape must still be decoded, not passed through"
