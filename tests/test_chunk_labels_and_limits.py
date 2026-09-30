"""Chunk labels, limits and line joins that were quietly wrong.

Each test is one confirmed defect: a title taken from a sibling section, a
breadcrumb naming the next chunk's heading, a truncation reported for a document
that fit, and text changed on its way through a reader.
"""

from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage

import diceo
from diceo.chunker import _split_long, chunk_blocks
from diceo.pdf.extract import _Paragraph
from diceo.sheets import _is_date_code
from diceo.types import Block, Diagnostics, Limits

from .fixtures import tiny_xlsx
from .test_pdf_dehyphenation import _line


def _heading(text: str, level: int) -> Block:
    return Block("heading", text, level)


def test_the_trail_is_kept_by_level_not_position():
    """A document that starts at H2: Alpha is Beta's sibling, never its title."""
    body = "word " * 150
    blocks = [
        _heading("Alpha", 2),
        Block("paragraph", body),
        _heading("Beta", 2),
        Block("paragraph", body),
    ]
    chunks = list(chunk_blocks(blocks, limits=Limits(target_chars=600)))
    labels = [(c.title, c.breadcrumb) for c in chunks]
    assert labels == [("Alpha", "")] * 2 + [("Beta", "")] * 2, labels


def test_a_chunk_is_not_labelled_by_the_heading_carried_out_of_it():
    blocks = [
        _heading("A", 1),
        Block("paragraph", "word " * 100),
        _heading("B", 2),
        Block("paragraph", "text " * 300),
    ]
    chunks = list(chunk_blocks(blocks, limits=Limits(target_chars=1800)))
    assert "B" not in chunks[0].text.split("\n")
    assert chunks[0].breadcrumb == ""
    assert chunks[1].breadcrumb == "B"


def test_a_document_that_fits_max_chars_exactly_is_not_truncated():
    report = Diagnostics()
    blocks = [Block("paragraph", "hello world")]
    out = list(chunk_blocks(blocks, limits=Limits(max_chars=11), diagnostics=report))
    assert [c.text for c in out] == ["hello world"]
    assert report.truncated == []


def test_a_long_block_with_no_spaces_is_cut_at_a_newline():
    text = "\n".join(f"https://example.org/item{i:04d}" for i in range(20))
    pieces = _split_long(text, 100)
    assert all(piece.startswith("https://") for piece in pieces), pieces


def test_a_line_ending_in_a_hyphen_before_a_number_keeps_it():
    para = _Paragraph()
    para.add(_line("fiscal years 2019-", 700.0))
    para.add(_line("2020 and after", 688.0))
    block = para.flush()
    assert block is not None and block.text == "fiscal years 2019-2020 and after"


def test_a_colour_section_is_not_a_date_format():
    assert not _is_date_code("[Magenta]0.00")
    assert not _is_date_code("[Red]#,##0")
    assert _is_date_code("[h]:mm")
    assert _is_date_code("yyyy-mm-dd")


def test_every_uncached_formula_is_counted(tmp_path):
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    sheet = (
        f'<?xml version="1.0"?><worksheet {ns}><sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>k</t></is></c>'
        '<c r="B1"><f>1+1</f></c><c r="C1" t="str"><f>"a"&amp;"b"</f></c>'
        '<c r="D1" t="b"><f>TRUE()</f></c></row></sheetData></worksheet>'
    )
    source = zipfile.ZipFile(io.BytesIO(tiny_xlsx()))
    path = tmp_path / "f.xlsx"
    with zipfile.ZipFile(path, "w") as out:
        for item in source.infolist():
            data = source.read(item.filename)
            out.writestr(item, sheet if item.filename.endswith("sheet1.xml") else data)
    report = Diagnostics()
    list(diceo.chunk(path, diagnostics=report))
    assert any("formula" in note and "3" in note for note in report.notes + report.truncated), (
        report.notes,
        report.truncated,
    )


def test_a_large_html_email_body_keeps_its_characters(tmp_path):
    """Past 64 KiB of ASCII the re-encoded UTF-8 body was sniffed again, and a
    stale `<meta charset=windows-1252>` turned `Café` into `CafÃ©`."""
    message = EmailMessage()
    message["Subject"] = "Menu"
    message["From"] = "a@b.c"
    style = "p{color:red}" * 7000
    message.set_content(
        f'<html><head><meta charset="windows-1252"><style>{style}</style></head>'
        "<body><p>Café crème – naïve</p></body></html>",
        subtype="html",
        charset="utf-8",
    )
    path = tmp_path / "m.eml"
    path.write_bytes(message.as_bytes())
    text = "\n".join(c.text for c in diceo.chunk(path))
    assert "Café crème – naïve" in text, text


def test_to_text_marks_headings_by_level_and_passes_the_rest_through():
    blocks = [_heading("Title", 1), Block("paragraph", "Body."), _heading("Sub", 2)]
    blocks += [Block("table_row", "a | b"), Block("table_row", "1 | 2")]
    assert "".join(diceo.to_text(blocks)) == "# Title\n\nBody.\n\n## Sub\n\na | b\n1 | 2\n"
