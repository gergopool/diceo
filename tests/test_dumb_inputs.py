"""Inputs a user will pass by accident, and the three bugs that audit found.

Written after surveying the issue trackers of pypdf, pdfminer.six, pdfplumber,
unstructured, MarkItDown, docling and Apache Tika for what actually goes wrong in
production (experiment 028, hostile input audit). Every case below
is a real reported failure class somewhere in that set, and three of them were live
bugs here:

* a **named pipe hung the library forever** -- `open("rb")` on a FIFO blocks until a
  writer appears, so one bad path stalled a whole indexing worker with no exception
  and no timeout;
* **UTF-16 with no BOM decoded to twice as many characters** with a NUL between each
  one, silently doubling every chunk offset;
* **NUL bytes reached the output**, which turns our success into an exception inside
  the caller's store (PostgreSQL rejects U+0000 in `text` and `jsonb`).

    uv run pytest tests/test_dumb_inputs.py -q
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

import pytest

import diceo
from diceo.errors import DiceoError, DocumentNotFound
from diceo.plaintext import sniff_utf16
from diceo.types import Diagnostics

# --------------------------------------------------------------------------- #
# things that are not regular files
#
# The four cases below are POSIX-only by construction -- a FIFO, an `AF_UNIX`
# socket, a character device and a symlink an unprivileged Windows account may not
# even create. The guard is on the file kind, not on the reader: what it protects
# is the *test*, and skipping it says so in the log rather than reporting a bug in
# a code path Windows cannot reach.
# --------------------------------------------------------------------------- #

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file kind: no FIFO, AF_UNIX or /dev/null on Windows"
)


@posix_only
def test_a_named_pipe_is_refused_immediately_and_does_not_hang(tmp_path: Path):
    """The bug this file exists for. A FIFO must never block the caller.

    The timing assertion is the real test: before the fix this call never returned
    at all, and a "raises" assertion alone would hang the suite rather than fail it.
    """
    fifo = tmp_path / "pipe.txt"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(DocumentNotFound, match="named pipe"):
        list(diceo.extract(fifo))
    assert time.monotonic() - started < 5.0, "refusing a FIFO must be instant"


@posix_only
def test_a_unix_socket_is_refused():
    """Not on `tmp_path`: a socket path is capped at 104 bytes on macOS and 108 on
    Linux, and pytest's per-test directory under macOS's `/var/folders/...` TMPDIR
    already spends more than that, so `bind` fails there and the test only ever ran
    on CI."""
    directory = Path(tempfile.mkdtemp(dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        path = directory / "sock"
        server.bind(str(path))
        with pytest.raises(DocumentNotFound, match="socket"):
            list(diceo.extract(path))
    finally:
        server.close()
        shutil.rmtree(directory, ignore_errors=True)


@posix_only
def test_a_character_device_is_refused_by_kind_not_by_emptiness():
    """`/dev/null` used to report "file is empty", which sends the reader looking in
    the wrong place. Naming the kind points at the path they passed."""
    with pytest.raises(DocumentNotFound, match="character device"):
        list(diceo.extract(Path("/dev/null")))


def test_a_directory_says_so(tmp_path: Path):
    with pytest.raises(DocumentNotFound, match="directory"):
        list(diceo.extract(tmp_path))


@posix_only
def test_a_dangling_symlink_is_not_found(tmp_path: Path):
    link = tmp_path / "dead.txt"
    link.symlink_to(tmp_path / "nothing-here")
    with pytest.raises(DocumentNotFound):
        list(diceo.extract(link))


# --------------------------------------------------------------------------- #
# BOM-less UTF-16: the silent offset doubler
# --------------------------------------------------------------------------- #

_SENTENCE = "The quick brown fox jumps over the lazy dog."


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_utf16_without_a_bom_decodes_to_the_right_length(tmp_path: Path, encoding: str):
    path = tmp_path / "unicode.txt"
    path.write_bytes(_SENTENCE.encode(encoding))
    text = "\n".join(block.text for block in diceo.extract(path))
    assert text.strip() == _SENTENCE
    assert "\x00" not in text


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_sniff_finds_bomless_utf16(encoding: str):
    assert sniff_utf16(_SENTENCE.encode(encoding)) == encoding


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"short",
        _SENTENCE.encode("utf-8"),
        _SENTENCE.encode("cp1252"),
        "een héél gewone Nederlandse zin met accenten".encode(),
        "これは日本語のテキストです".encode(),
        b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4,
        bytes(200),
        os.urandom(512),
    ],
)
def test_sniff_does_not_claim_utf16_for_things_that_are_not(raw: bytes):
    """False positives here would corrupt ordinary documents, which is far worse
    than missing an unusual one -- so the negative cases are the important ones."""
    assert sniff_utf16(raw) is None


def test_a_bom_still_wins_over_the_sniff(tmp_path: Path):
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xff\xfe" + _SENTENCE.encode("utf-16-le"))
    text = "\n".join(block.text for block in diceo.extract(path))
    assert text.strip() == _SENTENCE


# --------------------------------------------------------------------------- #
# control characters
# --------------------------------------------------------------------------- #


def test_nul_bytes_never_reach_the_output(tmp_path: Path):
    path = tmp_path / "nul.txt"
    path.write_bytes(b"hel\x00lo wor\x00ld")
    text = "\n".join(block.text for block in diceo.extract(path))
    assert "\x00" not in text
    assert text.strip() == "hello world"


def test_control_characters_are_stripped_but_tab_and_newline_survive(tmp_path: Path):
    path = tmp_path / "ctrl.txt"
    path.write_bytes(b"a\x01b\x0bc\td\x1fe\x7ff")
    text = "\n".join(block.text for block in diceo.extract(path))
    assert text.strip() == "abc\tdef"


def test_stripping_is_counted_in_diagnostics():
    """Rule 3: a document we quietly altered must say so."""
    from diceo.plaintext import strip_controls

    report = Diagnostics()
    assert strip_controls("a\x00b\x01c", report) == "abc"
    assert any("control_chars_removed=2" in note for note in report.notes)


def test_a_clean_string_is_returned_unchanged_and_reports_nothing():
    from diceo.plaintext import strip_controls

    report = Diagnostics()
    assert strip_controls(_SENTENCE, report) == _SENTENCE
    assert not report.notes


# --------------------------------------------------------------------------- #
# OOXML part names are not fixed
# --------------------------------------------------------------------------- #

_DOCX_BODY = (
    '<?xml version="1.0"?><w:document '
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p><w:r><w:t>SharePoint exported this document</w:t></w:r></w:p>"
    "</w:body></w:document>"
)
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)


def _docx_bytes(part: str, *, rels: bool = True) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="rels" ContentType='
            '"application/vnd.openxmlformats-package.relationships+xml"/></Types>',
        )
        if rels:
            archive.writestr(
                "_rels/.rels",
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                'openxmlformats.org/package/2006/relationships"><Relationship '
                f'Id="rId1" Type="{_OFFICE_DOCUMENT_REL}" Target="{part}"/>'
                "</Relationships>",
            )
        archive.writestr(part, _DOCX_BODY)
    return buf.getvalue()


@pytest.mark.parametrize(
    ("part", "rels"),
    [
        ("word/document.xml", True),  # the conventional name
        ("word/document2.xml", True),  # what Word Online and SharePoint write
        ("word/document2.xml", False),  # ...with unreadable relationships too
        ("Word/Document.xml", True),  # OPC part names are case-insensitive
        ("word/document17.xml", False),  # any digit suffix, no rels
    ],
)
def test_a_docx_is_read_whatever_its_main_part_is_called(part: str, rels: bool):
    """Hardcoding `word/document.xml` rejected real Microsoft output as "a plain ZIP
    archive, not a document" -- a valid document refused outright."""
    text = "\n".join(block.text for block in diceo.extract(_docx_bytes(part, rels=rels)))
    assert "SharePoint exported this document" in text


def test_a_zip_with_no_document_part_is_still_refused():
    """The tolerant match must not turn every ZIP into a document."""
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("readme.txt", "just an archive")
        archive.writestr("word/notdocument.xml", "<a/>")
    with pytest.raises(DiceoError, match="ZIP"):
        list(diceo.extract(buf.getvalue()))


# --------------------------------------------------------------------------- #
# invisible characters: blank to a reader, truthy to Python
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "invisible",
    [
        "​",  # zero-width space
        "﻿",  # zero-width no-break space (a BOM landing mid-file)
        "­",  # soft hyphen
        "‍",  # zero-width joiner
        "‌",  # zero-width non-joiner
        "⁠",  # word joiner
        "‏",  # right-to-left mark
        " ",  # no-break space (this one `strip()` already handled)
        "　",  # ideographic space (ditto)
        "​﻿­",  # several at once
    ],
)
def test_a_line_of_only_invisible_characters_is_blank(invisible: str):
    """`str.strip()` removes NBSP and U+3000 but not the zero-width family, so such
    a line used to become its own block: a chunk of pure invisible characters that
    gets embedded, stored, searched, and can be returned as a hit."""
    doc = f"Real paragraph one.\n\n{invisible}\n\nReal paragraph two.".encode()
    blocks = [block.text for block in diceo.extract(io.BytesIO(doc))]
    assert len(blocks) == 2, f"invisible-only line became a block: {blocks!r}"
    assert not [t for t in blocks if not any(ch.isalnum() for ch in t)]


@pytest.mark.parametrize(
    "text",
    [
        "क्‌ष uses a zero-width non-joiner",  # Indic conjunct control
        "\U0001f468‍\U0001f469 is a ZWJ sequence",  # emoji family
        "hy­phen­ation hints",  # soft hyphen mid-word
    ],
)
def test_invisible_characters_inside_words_are_preserved(text: str):
    """The blank test must never become a rewrite. ZWJ and ZWNJ are load-bearing
    inside words and a soft hyphen is a real hyphenation hint -- only their
    appearance *alone on a line* means nothing."""
    out = "\n".join(block.text for block in diceo.extract(io.BytesIO(text.encode())))
    assert out.strip() == text


# --------------------------------------------------------------------------- #
# lying extensions and odd names -- these already worked; pinning them
# --------------------------------------------------------------------------- #

_HTML = b"<html><head><title>T</title></head><body><p>hello world</p></body></html>"


@pytest.mark.parametrize(
    "name",
    [
        "actually-html.pdf",  # textract parses these as the extension claims
        "actually-html.docx",
        "UPPERCASE.HTML",
        "no-extension",
        "spaced name.html",
        "emoji-\U0001f600.html",
    ],
)
def test_content_beats_a_lying_extension(tmp_path: Path, name: str):
    path = tmp_path / name
    path.write_bytes(_HTML)
    text = "\n".join(block.text for block in diceo.extract(path))
    assert "hello world" in text


def test_a_whitespace_only_file_is_empty_rather_than_an_error(tmp_path: Path):
    """unstructured raised IndexError on this; docling's CSV backend still does."""
    path = tmp_path / "blank.txt"
    path.write_bytes(b"   \n\t\n  \n")
    assert list(diceo.extract(path)) == []


def test_bytes_and_stream_inputs_are_not_confused_with_paths():
    assert any(b.text for b in diceo.extract(_HTML))
    assert any(b.text for b in diceo.extract(io.BytesIO(_HTML)))
    with pytest.raises(DiceoError):
        list(diceo.extract("this is a string, not a path"))
