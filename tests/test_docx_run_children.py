"""Characters that live in the markup, and cells that cover more than one column.

Three defects out of experiment 038 (edge case mining) with one
shape between them: the reader met an element it had no branch for, dropped it, and
welded the words either side into a token no query contains.

* `w:noBreakHyphen` -- the hyphen an author types where the word must not break
  across lines. Measured over the 51 real DOCX in the research corpus: 2 occurrences,
  both in
  `ipbes-invasive-species-spm-fr.docx` and both mid-word, so that report's
  `sous-estimation` and `sous-régions` reached the index as `sousestimation` and
  `sousrégions`. It is now U+002D and not the U+2011 it literally is, because rule 4
  asks which token a query matches and nobody types a non-breaking hyphen.
  `w:softHyphen` is still dropped -- that one is a line-break hint with no character
  behind it -- and the pin for that is here too.
* `w:sym` -- a glyph out of a symbol-encoded font, dropped with no character *and* no
  separator, so `80<w:sym/>C` came out `80C` and `See<w:sym/>Appendix B` came out
  `SeeAppendix B`.
* `w:gridSpan` -- a merged cell. Emitted as a single field, every value after it in
  the row sits under the wrong header.

`w:sym` and `w:gridSpan` occur **0** times in those 51 files, so their fixtures are
necessarily hand-built and their effect on real documents is unmeasured here. Across
the 24 held-out DOCX the fingerprint hashes, all three constructs occur 0 times
against 12,446 `w:p`, which is why the docx digest cannot move.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks

from .fixtures import _CONTENT_TYPES, _W_NS

_MC_NS = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def _docx(body: str) -> bytes:
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS} {_MC_NS}>'
        f"<w:body>{body}</w:body></w:document>",
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _read(tmp_path: Path, body: str, name: str = "runs.docx"):
    path = tmp_path / name
    path.write_bytes(_docx(body))
    report = OoxmlDiagnostics()
    return [b.text for b in iter_docx_blocks(path, diagnostics=report)], report


def _cells(row: str) -> list[str]:
    return row.split(" | ")


# --------------------------------------------------------------------------- #
# w:noBreakHyphen -- and w:softHyphen, which must stay dropped
# --------------------------------------------------------------------------- #

_HYPHENATED = (
    "<w:p><w:r><w:t>state</w:t><w:noBreakHyphen/><w:t>of</w:t><w:noBreakHyphen/>"
    "<w:t>the</w:t><w:noBreakHyphen/><w:t>art</w:t></w:r></w:p>"
)


def test_a_non_breaking_hyphen_is_a_character(tmp_path: Path) -> None:
    """The assertion that was failing: `stateoftheart`, one token, matching nothing."""
    texts, _ = _read(tmp_path, _HYPHENATED)
    assert texts == ["state-of-the-art"]


def test_the_corpus_sentence_that_found_it(tmp_path: Path) -> None:
    """The IPBES French report's own construction, spelled the way Word spells it:
    the hyphen sits in a run of its own between two text runs."""
    texts, _ = _read(
        tmp_path,
        "<w:p><w:r><w:t>18 sous</w:t></w:r><w:r><w:noBreakHyphen/></w:r>"
        "<w:r><w:t>régions de l’IPBES</w:t></w:r></w:p>",
    )
    assert texts == ["18 sous-régions de l’IPBES"]


def test_a_soft_hyphen_is_still_dropped(tmp_path: Path) -> None:
    """The regression pin. `w:softHyphen` is a *hint* about where a renderer may
    break the word; there is no character in the document, and putting one there
    would split `hyphenation` into two tokens that are not words."""
    texts, _ = _read(
        tmp_path, "<w:p><w:r><w:t>hyphen</w:t><w:softHyphen/><w:t>ation</w:t></w:r></w:p>"
    )
    assert texts == ["hyphenation"]


def test_a_hyphen_in_a_discarded_fallback_does_not_become_a_block(tmp_path: Path) -> None:
    """The reason the new branches are guarded the way `w:t` is and `w:br` is not.

    Word writes a text box twice, `mc:Choice` and `mc:Fallback`, and the fallback's
    runs are suppressed. A hyphen appended there survives `.strip()` where a tab or a
    newline does not, so an unguarded branch emits a paragraph reading `-` for every
    floating box in the document.
    """
    box = (
        "<w:p><w:r><mc:AlternateContent>"
        '<mc:Choice Requires="wps"><w:txbxContent><w:p><w:r><w:t>Sous</w:t>'
        "<w:noBreakHyphen/><w:t>titre</w:t></w:r></w:p></w:txbxContent></mc:Choice>"
        "<mc:Fallback><w:txbxContent><w:p><w:r><w:t>Sous</w:t><w:noBreakHyphen/>"
        "<w:t>titre</w:t></w:r></w:p></w:txbxContent></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>"
    )
    texts, _ = _read(tmp_path, box)
    assert texts == ["Sous-titre"]


def test_a_hyphen_inside_a_field_instruction_is_not_content(tmp_path: Path) -> None:
    """`PAGEREF _Toc1 \\h` is not text, and neither is a hyphen inside it."""
    texts, _ = _read(
        tmp_path,
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        "<w:r><w:instrText>PAGEREF _Toc1</w:instrText><w:noBreakHyphen/></w:r>"
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>12</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>',
    )
    assert texts == ["12"]


# --------------------------------------------------------------------------- #
# w:sym
# --------------------------------------------------------------------------- #


def test_a_symbol_no_longer_welds_the_runs_either_side(tmp_path: Path) -> None:
    """The minimum the fix owes: two words stay two tokens. Wingdings is not mapped
    -- a dingbat is nobody's query term -- so this is the space fallback."""
    texts, report = _read(
        tmp_path,
        '<w:p><w:r><w:t>See</w:t><w:sym w:font="Wingdings" w:char="F0E0"/>'
        "<w:t>Appendix B</w:t></w:r></w:p>",
    )
    assert texts == ["See Appendix B"]
    assert report.symbols_flattened == 1, "the glyph is gone; rule 3 wants that counted"


def test_the_symbol_font_glyphs_that_sit_inside_a_token(tmp_path: Path) -> None:
    """`80°C`, `±5%` and `µg/mL` are single tokens, and a space would split each of
    them in two. Read off the Symbol encoding vector: B0 degree, B1 plusminus, 6D mu."""
    texts, report = _read(
        tmp_path,
        '<w:p><w:r><w:t>Heated to 80</w:t><w:sym w:font="Symbol" w:char="F0B0"/>'
        "<w:t>C</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>Tolerance </w:t><w:sym w:font="SymbolMT" w:char="F0B1"/>'
        "<w:t>5 percent</w:t></w:r></w:p>"
        '<w:p><w:r><w:t>Dosed at 10 </w:t><w:sym w:font="Symbol" w:char="F06D"/>'
        "<w:t>g/mL</w:t></w:r></w:p>",
    )
    assert texts == ["Heated to 80°C", "Tolerance ±5 percent", "Dosed at 10 μg/mL"]
    assert report.symbols_flattened == 0, "nothing was lost, so nothing to report"


def test_a_bare_font_byte_is_the_same_glyph(tmp_path: Path) -> None:
    """Word writes the private-use code point its cmap maps into (`F0B0`); other
    producers write the font's own byte (`B0`). Same glyph, same font."""
    texts, _ = _read(
        tmp_path,
        '<w:p><w:r><w:t>20</w:t><w:sym w:font="Symbol" w:char="B0"/><w:t>C</w:t></w:r></w:p>',
    )
    assert texts == ["20°C"]


def test_an_unreadable_symbol_is_a_space_and_not_an_exception(tmp_path: Path) -> None:
    """A caller sees a DiceoError or they see chunks. `w:char="oops"` -- and a
    `w:sym` with no attributes at all -- must not be an uncaught ValueError."""
    texts, report = _read(
        tmp_path,
        '<w:p><w:r><w:t>alpha</w:t><w:sym w:font="Symbol" w:char="oops"/>'
        "<w:t>beta</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>gamma</w:t><w:sym/><w:t>delta</w:t></w:r></w:p>",
    )
    assert texts == ["alpha beta", "gamma delta"]
    assert report.symbols_flattened == 2


def test_a_symbol_alone_in_a_paragraph_emits_no_block(tmp_path: Path) -> None:
    """The space is a separator, not content: a paragraph holding one unmapped
    symbol is still an empty paragraph and must not become a block of whitespace."""
    texts, _ = _read(
        tmp_path, '<w:p><w:r><w:sym w:font="Wingdings" w:char="F06E"/></w:r></w:p>'
    )
    assert texts == []


# --------------------------------------------------------------------------- #
# w:gridSpan
# --------------------------------------------------------------------------- #

_SPANNING_HEADER = (
    "<w:tbl>"
    '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
    "<w:p><w:r><w:t>2024 results</w:t></w:r></w:p></w:tc></w:tr>"
    "<w:tr><w:tc><w:p><w:r><w:t>Revenue</w:t></w:r></w:p></w:tc>"
    "<w:tc><w:p><w:r><w:t>12.4</w:t></w:r></w:p></w:tc></w:tr>"
    "</w:tbl>"
)


def test_a_merged_header_cell_covers_the_columns_it_spans(tmp_path: Path) -> None:
    """The header row had one field against the data row's two, so the chunker
    prefixed `2024 results` over a row whose second column it does not name."""
    texts, report = _read(tmp_path, _SPANNING_HEADER)
    assert [_cells(row) for row in texts] == [
        ["2024 results", "2024 results"],
        ["Revenue", "12.4"],
    ]
    assert report.merged_cells_expanded == 1


def test_a_mid_row_span_keeps_every_later_value_under_its_own_header(tmp_path: Path) -> None:
    """The one that silently misattributes: before the fix the header read
    `Region | 2024` and the data row `North | 12.4 | 13.9`, so 13.9 had no header at
    all and 12.4 answered for both years."""
    texts, _ = _read(
        tmp_path,
        "<w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc>"
        '<w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        "<w:p><w:r><w:t>2024</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>12.4</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>13.9</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl>",
    )
    header, data = (_cells(row) for row in texts)
    assert header == ["Region", "2024", "2024"]
    assert len(data) == len(header), "the row and its header must line up"


def test_an_ordinary_table_is_untouched(tmp_path: Path) -> None:
    """The control. A table with no `w:gridSpan` anywhere -- which is every table in
    the 51 real DOCX in the research corpus -- and the counter must stay at zero. The
    two-paragraph cell is one field: it used to be two, and 12.4 had no header. A
    diagnostic that fires on healthy input is one a caller learns to ignore inside a
    day."""
    texts, report = _read(
        tmp_path,
        "<w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>2024</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>and islands</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>12.4</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl>",
    )
    assert texts == ["Region | 2024", "North and islands | 12.4"]
    assert report.merged_cells_expanded == 0
    assert report.symbols_flattened == 0


def test_an_explicit_span_of_one_changes_nothing(tmp_path: Path) -> None:
    """`w:gridSpan w:val="1"` is a cell that covers its own column, which is what an
    unmarked cell does, so it is read exactly like one and counted as no merge."""
    texts, report = _read(
        tmp_path,
        "<w:tbl><w:tr>"
        '<w:tc><w:tcPr><w:gridSpan w:val="1"/></w:tcPr>'
        "<w:p><w:r><w:t>North</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>and islands</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>12.4</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>",
    )
    assert texts == ["North and islands | 12.4"]
    assert report.merged_cells_expanded == 0


def test_a_hostile_span_cannot_blow_the_row_up(tmp_path: Path) -> None:
    """Rule 2 is a memory bound, not a style. `w:val="1000000"` would otherwise
    materialise a million copies of one cell for one row."""
    texts, _ = _read(
        tmp_path,
        "<w:tbl><w:tr>"
        '<w:tc><w:tcPr><w:gridSpan w:val="1000000"/></w:tcPr>'
        "<w:p><w:r><w:t>Everything</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>",
    )
    assert len(_cells(texts[0])) == 64


def test_an_unparseable_span_is_one_column(tmp_path: Path) -> None:
    """Typed-error contract: a garbage attribute is not an uncaught ValueError."""
    texts, report = _read(
        tmp_path,
        "<w:tbl><w:tr>"
        '<w:tc><w:tcPr><w:gridSpan w:val="many"/></w:tcPr>'
        "<w:p><w:r><w:t>Everything</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>12.4</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>",
    )
    assert texts == ["Everything | 12.4"]
    assert report.merged_cells_expanded == 0


def test_a_tracked_change_cannot_restore_the_old_span(tmp_path: Path) -> None:
    """`w:tcPrChange` carries the cell's *previous* geometry, later in the same
    `w:tcPr`, so first wins -- the same rule `w:pStyle` already follows."""
    texts, _ = _read(
        tmp_path,
        "<w:tbl><w:tr>"
        '<w:tc><w:tcPr><w:gridSpan w:val="2"/>'
        '<w:tcPrChange w:id="1"><w:tcPr><w:gridSpan w:val="3"/></w:tcPr></w:tcPrChange>'
        "</w:tcPr><w:p><w:r><w:t>2024 results</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>",
    )
    assert len(_cells(texts[0])) == 2


def test_a_merged_cell_holding_a_nested_table_still_spans(tmp_path: Path) -> None:
    """Word nests tables for layout constantly. The inner table's cells write over
    the same scalars, so the outer cell's span is saved with the rest of its frame."""
    texts, report = _read(
        tmp_path,
        "<w:tbl><w:tr>"
        '<w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        "<w:p><w:r><w:t>Summary</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>inner</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "</w:tc></w:tr></w:tbl>",
    )
    assert "inner" in texts
    assert report.nested_tables == 1
    assert _cells(texts[-1]) == ["Summary", "Summary"]


# --------------------------------------------------------------------------- #
# the public path
# --------------------------------------------------------------------------- #


def test_the_repaired_token_reaches_the_chunk(tmp_path: Path) -> None:
    """All three, through `diceo.chunk`, because that is what a caller indexes --
    and a control that none of it raises."""
    path = tmp_path / "report.docx"
    path.write_bytes(
        _docx(
            _HYPHENATED
            + '<w:p><w:r><w:t>Heated to 80</w:t><w:sym w:font="Symbol" w:char="F0B0"/>'
            "<w:t>C</w:t></w:r></w:p>" + _SPANNING_HEADER
        )
    )
    body = "\n".join(chunk.text for chunk in diceo.chunk(path))
    assert "state-of-the-art" in body
    assert "80°C" in body
    assert "2024 results | 2024 results" in body
