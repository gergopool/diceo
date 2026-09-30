"""Numbering is authored text, including empty form cells and legal headings."""

from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks

from .fixtures import _W_NS, _zip


def _paragraph(text="", *, depth=0, num="1", style="", properties=""):
    reference = (
        f'<w:numPr><w:ilvl w:val="{depth}"/><w:numId w:val="{num}"/></w:numPr>'
        if num is not None
        else ""
    )
    return (
        f'<w:p><w:pPr><w:pStyle w:val="{style}"/>{reference}{properties}</w:pPr>'
        f"<w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def _level(depth, fmt="decimal", text=None, start=1, extra=""):
    marker = f"%{depth + 1}." if text is None else text
    return (
        f'<w:lvl w:ilvl="{depth}"><w:start w:val="{start}"/>'
        f'<w:numFmt w:val="{fmt}"/><w:lvlText w:val="{marker}"/>{extra}</w:lvl>'
    )


def _read(tmp_path, body, levels, *, styles="", overrides="", other_nums=""):
    path = tmp_path / "numbering.docx"
    path.write_bytes(
        _zip(
            {
                "word/document.xml": f"<w:document {_W_NS}><w:body>{body}</w:body>"
                "</w:document>",
                "word/styles.xml": f"<w:styles {_W_NS}>{styles}</w:styles>",
                "word/numbering.xml": f"<w:numbering {_W_NS}>"
                f'<w:abstractNum w:abstractNumId="7">{levels}</w:abstractNum>'
                f'<w:num w:numId="1"><w:abstractNumId w:val="7"/>{overrides}</w:num>'
                f"{other_nums}</w:numbering>",
            }
        )
    )
    report = OoxmlDiagnostics()
    return list(iter_docx_blocks(path, diagnostics=report)), report


def test_numbered_headings_and_empty_table_cells_share_the_counter(tmp_path):
    style = '<w:style w:styleId="Clause"><w:pPr><w:outlineLvl w:val="0"/>'
    style += '<w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
    body = _paragraph("Scope", num=None, style="Clause")
    body += "<w:tbl><w:tr><w:tc>" + _paragraph() + "</w:tc><w:tc>"
    body += _paragraph("Project title", num="0") + "</w:tc></w:tr></w:tbl>"
    body += _paragraph("Details", depth=1)
    blocks, report = _read(tmp_path, body, _level(0) + _level(1, text="%1.%2"), styles=style)
    assert [(b.kind, b.text) for b in blocks] == [
        ("heading", "1. Scope"),
        ("table_row", "2. | Project title"),
        ("list_item", "2.1 Details"),
    ]
    assert report.unresolved_list_markers == 0


def test_authored_hierarchy_restarts_when_the_parent_is_used(tmp_path):
    body = "".join(_paragraph("item", depth=depth) for depth in [0, 1, 2, 2, 1, 2, 0, 1, 2])
    levels = _level(0) + _level(1, text="%1.%2") + _level(2, text="(%1.%2.%3)")
    blocks, _ = _read(tmp_path, body, levels)
    assert [b.text for b in blocks] == [
        "1. item",
        "1.1 item",
        "(1.1.1) item",
        "(1.1.2) item",
        "1.2 item",
        "(1.2.1) item",
        "2. item",
        "2.1 item",
        "(2.1.1) item",
    ]


def test_never_restart_and_explicit_restart_are_respected(tmp_path):
    body = "".join(_paragraph("item", depth=depth) for depth in [0, 1, 2, 1, 2, 0, 1, 2])
    levels = (
        _level(0)
        + _level(1, extra='<w:lvlRestart w:val="0"/>')
        + _level(2, extra='<w:lvlRestart w:val="1"/>')
    )
    blocks, _ = _read(tmp_path, body, levels)
    assert [b.text for b in blocks] == [
        "1. item",
        "1. item",
        "1. item",
        "2. item",
        "2. item",
        "2. item",
        "3. item",
        "1. item",
    ]


def test_start_override_wins_over_level_override_and_each_num_is_independent(tmp_path):
    overrides = '<w:lvlOverride w:ilvl="0">' + _level(0, text="Clause %1)", start=4)
    overrides += '<w:startOverride w:val="7"/></w:lvlOverride>'
    second = '<w:num w:numId="2"><w:abstractNumId w:val="7"/></w:num>'
    blocks, _ = _read(
        tmp_path,
        _paragraph("one") + _paragraph("two", num="2") + _paragraph("three"),
        _level(0, start=2),
        overrides=overrides,
        other_nums=second,
    )
    assert [b.text for b in blocks] == ["Clause 7) one", "2. two", "Clause 8) three"]


def test_parent_formats_and_legal_decimal_override(tmp_path):
    body = _paragraph("parent") + _paragraph("child", depth=1)
    levels = _level(0, "upperRoman") + _level(1, "lowerLetter", "(%1-%2)")
    blocks, _ = _read(tmp_path, body, levels)
    assert [b.text for b in blocks] == ["I. parent", "(I-a) child"]
    levels += _level(2, "lowerRoman", "%1.%2.%3", extra="<w:isLgl/>")
    blocks, _ = _read(tmp_path, body + _paragraph("legal", depth=2), levels)
    assert blocks[-1].text == "1.1.1 legal"


def test_unknown_formats_are_diagnosed_instead_of_inventing_an_ordinal(tmp_path):
    blocks, report = _read(tmp_path, _paragraph("item"), _level(0, "cardinalText"))
    assert blocks[0].text == "- item"
    assert report.unresolved_list_markers == 1


def test_outline_nine_is_body_and_explicit_zero_removes_inherited_numbering(tmp_path):
    styles = '<w:style w:styleId="Heading1"><w:pPr><w:outlineLvl w:val="9"/>'
    styles += '<w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>'
    blocks, report = _read(
        tmp_path, _paragraph("Contents", num="0", style="Heading1"), _level(0), styles=styles
    )
    assert [(b.kind, b.level, b.text) for b in blocks] == [("paragraph", 0, "Contents")]
    assert report.unresolved_list_markers == 0


def test_later_level_placeholders_are_ignored_and_missing_starts_use_zero(tmp_path):
    level = '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/>'
    level += '<w:lvlText w:val="Article %1 / %2"/></w:lvl>'
    blocks, report = _read(tmp_path, _paragraph("scope"), level)
    assert blocks[0].text == "Article 0 /  scope"
    assert report.unresolved_list_markers == 0


def test_deleted_paragraph_marks_and_historical_properties_do_not_advance_lists(tmp_path):
    deleted = _paragraph(properties="<w:rPr><w:del/></w:rPr>")
    deleted = deleted.replace("<w:t></w:t>", "<w:delText>Removed clause</w:delText>")
    historical = '<w:pPrChange><w:pPr><w:numPr><w:ilvl w:val="1"/>'
    historical += '<w:numId w:val="1"/></w:numPr></w:pPr></w:pPrChange>'
    body = deleted + _paragraph("Plain", num=None, properties=historical)
    body += _paragraph("First visible", properties=historical) + _paragraph()
    body += _paragraph("Next visible")
    blocks, report = _read(tmp_path, body, _level(0) + _level(1, text="%1.%2"))
    assert [(b.kind, b.text) for b in blocks] == [
        ("paragraph", "Plain"),
        ("list_item", "1. First visible"),
        ("list_item", "2."),
        ("list_item", "3. Next visible"),
    ]
    assert report.unresolved_list_markers == 0


def test_alphabetic_markers_repeat_letters_after_z_and_bound_hostile_starts(tmp_path):
    blocks, report = _read(
        tmp_path,
        _paragraph("one") + _paragraph("two") + _paragraph("three"),
        _level(0, "lowerLetter", start=26),
    )
    assert [b.text for b in blocks] == ["z. one", "aa. two", "bb. three"]
    assert report.unresolved_list_markers == 0
    blocks, report = _read(
        tmp_path, _paragraph("bounded"), _level(0, "upperLetter", start=10**9)
    )
    assert blocks[0].text == "- bounded"
    assert report.unresolved_list_markers == 1
