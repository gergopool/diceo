"""A package that names one part twice has no single meaning, so we refuse it.

Found by the experiment 038 (edge case mining) audit and reproduced
before the fix: a ZIP may legally hold two members with the same name, OPC may not,
and `zipfile` resolves the ambiguity by handing back **the last one written**. Append
an empty `word/document.xml` to a real .docx and diceo extracted *nothing* — no
exception, no diagnostic, `lost_data` False. The caller's index silently does not
contain the document, which is the failure mode rule 3 exists for.

Refusing beats picking a copy: the two copies are two different documents and only
the producer knows which was meant. A reader that guesses will disagree with Word,
with a validator, and with the next reader.
"""

from __future__ import annotations

import io
import zipfile

import pytest

import diceo
from diceo import CorruptDocument

from .fixtures import tiny_docx


def _append_member(raw: bytes, name: str, payload: bytes) -> bytes:
    """Append a second member with an existing name, as a sloppy producer would."""
    buffer = io.BytesIO(raw)
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.fixture
def real_docx(tmp_path):
    path = tmp_path / "real.docx"
    path.write_bytes(
        tiny_docx(["The quick brown fox jumps.", "A second paragraph of real text."])
    )
    return path


def test_the_undamaged_file_still_reads(real_docx):
    """Guard against the check firing on every package."""
    text = "".join(piece.text for piece in diceo.chunk(real_docx))

    assert "quick brown fox" in text


def test_a_duplicated_document_part_is_refused(real_docx, tmp_path):
    doubled = tmp_path / "doubled.docx"
    doubled.write_bytes(
        _append_member(real_docx.read_bytes(), "word/document.xml", b"<w:document/>")
    )

    with pytest.raises(CorruptDocument) as caught:
        list(diceo.chunk(doubled))

    assert "more than once" in str(caught.value)
    assert "word/document.xml" in str(caught.value)


def test_the_silent_failure_it_replaces(real_docx, tmp_path):
    """The regression itself: before the fix this returned zero chunks and said
    nothing. Either an error or the text is acceptable; silence is not."""
    doubled = tmp_path / "doubled.docx"
    doubled.write_bytes(
        _append_member(real_docx.read_bytes(), "word/document.xml", b"<w:document/>")
    )

    try:
        chunks = list(diceo.chunk(doubled))
    except CorruptDocument:
        return
    assert chunks, "extracted nothing and raised nothing"


def test_case_differing_duplicates_are_caught_too(real_docx, tmp_path):
    """OPC part names are case-insensitive, so `Word/Document.xml` is the same part."""
    doubled = tmp_path / "cased.docx"
    doubled.write_bytes(
        _append_member(real_docx.read_bytes(), "Word/Document.xml", b"<w:document/>")
    )

    with pytest.raises(CorruptDocument):
        list(diceo.chunk(doubled))


def test_a_duplicated_unrelated_part_is_refused_as_well(real_docx, tmp_path):
    """Not only the part we happen to read. Any duplicate means two readers can
    disagree about what the file says, which is the thing being refused."""
    doubled = tmp_path / "styles.docx"
    doubled.write_bytes(_append_member(real_docx.read_bytes(), "word/styles.xml", b"<x/>"))

    with pytest.raises(CorruptDocument):
        list(diceo.chunk(doubled))


def test_the_refusal_is_a_diceo_error(real_docx, tmp_path):
    """D15's contract: a caller sees a DiceoError or chunks, never a third thing."""
    doubled = tmp_path / "doubled.docx"
    doubled.write_bytes(
        _append_member(real_docx.read_bytes(), "word/document.xml", b"<w:document/>")
    )

    with pytest.raises(diceo.DiceoError):
        list(diceo.chunk(doubled))
