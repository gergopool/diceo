"""A document-sized table must not retain all its annotations in Python memory."""

from __future__ import annotations

import io
import tempfile
import zipfile
from xml.etree.ElementTree import ParseError

import pytest

from diceo.ooxml import OoxmlDiagnostics, iter_docx_blocks

from .fixtures import _W_NS
from .test_nested_tables import _docx


def _row(text: str, alt: str = "", note: str = "") -> str:
    drawing = (
        '<w:drawing><wp:docPr xmlns:wp="http://schemas.openxmlformats.org/'
        f'drawingml/2006/wordprocessingDrawing" descr="{alt}"/></w:drawing>'
        if alt
        else ""
    )
    reference = f'<w:footnoteReference w:id="{note}"/>' if note else ""
    return (
        f"<w:tr><w:tc><w:p><w:r><w:t>{text}</w:t>{drawing}{reference}</w:r></w:p></w:tc></w:tr>"
    )


def _spools(monkeypatch):
    opened = []
    original = tempfile.SpooledTemporaryFile

    def tracked(*args, **kwargs):
        assert kwargs["max_size"] == 1024**2
        handle = original(*args, **kwargs)
        opened.append(handle)
        return handle

    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", tracked)
    return opened


def test_large_table_annotations_spill_and_keep_order(monkeypatch):
    opened = _spools(monkeypatch)
    rows = 1500
    text = "x" * 1024
    body = "<w:tbl>" + "".join(_row(str(i), f"{i}: {text}") for i in range(rows))
    stream = iter_docx_blocks(io.BytesIO(_docx(body + "</w:tbl>")))
    for i in range(rows):
        block = next(stream)
        assert (block.kind, block.text, block.row) == ("table_row", str(i), i)
    assert len(opened) == 1 and opened[0]._rolled
    for i in range(rows):
        block = next(stream)
        assert (block.kind, block.text) == ("caption", f"{i}: {text}")
    with pytest.raises(StopIteration):
        next(stream)
    assert opened[0].closed


def test_notes_nested_tables_and_adjacent_tables_keep_annotation_order(monkeypatch):
    opened = _spools(monkeypatch)
    outer = _row("outer", "outer image", "1").replace(
        "</w:tc>", "<w:tbl>" + _row("inner", "inner image", "2") + "</w:tbl></w:tc>"
    )
    body = "<w:tbl>" + outer + "</w:tbl><w:tbl>" + _row("next", "next image") + "</w:tbl>"
    handle = io.BytesIO(_docx(body))
    with zipfile.ZipFile(handle, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "word/footnotes.xml",
            f"<w:footnotes {_W_NS}>"
            '<w:footnote w:id="1"><w:p><w:r><w:t>outer note</w:t></w:r></w:p></w:footnote>'
            '<w:footnote w:id="2"><w:p><w:r><w:t>inner note</w:t></w:r></w:p></w:footnote>'
            "</w:footnotes>",
        )
    handle.seek(0)
    report = OoxmlDiagnostics()
    blocks = [(b.kind, b.text) for b in iter_docx_blocks(handle, diagnostics=report)]
    assert blocks == [
        ("table_row", "inner"),
        ("table_row", "outer"),
        ("caption", "outer image"),
        ("footnote", "outer note"),
        ("caption", "inner image"),
        ("footnote", "inner note"),
        ("table_row", "next"),
        ("caption", "next image"),
    ]
    assert (report.figure_alt_texts, report.footnotes, report.nested_tables) == (3, 2, 1)
    assert len(opened) == 1 and opened[0].closed


@pytest.mark.parametrize("read", [1, 3])
def test_early_close_closes_annotation_spool(monkeypatch, read):
    opened = _spools(monkeypatch)
    body = "<w:tbl>" + _row("header", "first image") + _row("value", "second image")
    stream = iter_docx_blocks(io.BytesIO(_docx(body + "</w:tbl>")))
    for _ in range(read):
        next(stream)
    assert len(opened) == 1 and not opened[0].closed
    stream.close()
    assert opened[0].closed


def test_parser_failure_closes_annotation_spool(monkeypatch):
    opened = _spools(monkeypatch)
    stream = iter_docx_blocks(
        io.BytesIO(_docx("<w:tbl>" + _row("value", "authored image description")))
    )
    assert next(stream).text == "value"
    with pytest.raises(ParseError):
        next(stream)
    assert len(opened) == 1 and opened[0].closed


def test_tables_without_annotations_do_not_open_a_spool(monkeypatch):
    opened = _spools(monkeypatch)
    blocks = list(iter_docx_blocks(io.BytesIO(_docx("<w:tbl>" + _row("value") + "</w:tbl>"))))
    assert [block.text for block in blocks] == ["value"]
    assert opened == []
