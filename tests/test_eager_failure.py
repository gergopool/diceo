"""Where the exception surfaces, which is a different promise from which exception it is.

``tests/test_robustness.py`` pins down *what* diceo raises. This file pins down
*when*, and the two are separately breakable: a reader can keep raising exactly the
right ``CorruptDocument`` while moving the moment it does so past the caller's
``try``.

Both entry points are generators, so before ``api._open`` existed the whole library
failed late. Measured on 2026-07-31, every ordinary failure surfaced on the first
``next()``: a missing path, a directory, a zero-byte file, a truncated download, a
``.zip`` renamed to ``.docx``. None of those are exotic -- they are what a crawl is
made of -- and they all landed outside the handler in the obvious way to write the
call::

    try:
        pieces = diceo.chunk(path)     # a healthy iterator, always
    except diceo.DiceoError:
        quarantine(path)
    for piece in pieces:                 # ... and it raises here
        index(piece)

The rule these tests encode:

* A failure that can be seen **without reading the document** -- the source will not
  open, or the bytes are not a format we read -- raises **at the call**.
* A failure found **part-way through** is not an exception at all (rule 3). The
  chunks read before it are yielded, and the reason lands in ``diagnostics``.

The second half is what stops the first half from being satisfied by simply making
everything eager, which would cost the streaming guarantee and violate rule 2.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import diceo
from diceo.errors import CorruptDocument, DiceoError, DocumentNotFound, UnsupportedFormat
from tests import fixtures

ENTRY_POINTS = pytest.mark.parametrize(
    "call", [diceo.chunk, diceo.extract], ids=["chunk", "extract"]
)


def _raises_before_iteration(call, source, **kwargs) -> DiceoError:
    """Assert the call itself raises, and report what it raised.

    ``pytest.raises`` around the bare call is the whole assertion: if the entry
    point were still a generator function the body would not run at all and no
    exception would be raised here.
    """
    with pytest.raises(DiceoError) as caught:
        call(source, **kwargs)
    return caught.value


@ENTRY_POINTS
def test_a_missing_path_raises_at_the_call(call, tmp_path: Path) -> None:
    exc = _raises_before_iteration(call, tmp_path / "does-not-exist.pdf")
    assert isinstance(exc, DocumentNotFound)


@ENTRY_POINTS
def test_a_directory_raises_at_the_call(call, tmp_path: Path) -> None:
    exc = _raises_before_iteration(call, tmp_path)
    assert isinstance(exc, DocumentNotFound)
    assert "directory" in str(exc)


@ENTRY_POINTS
def test_an_empty_file_raises_at_the_call(call, tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    exc = _raises_before_iteration(call, empty)
    assert isinstance(exc, CorruptDocument)


@ENTRY_POINTS
@pytest.mark.parametrize("suffix", [".txt", ".csv", ".md"])
def test_an_empty_text_file_raises_like_every_other_format(
    call, suffix: str, tmp_path: Path
) -> None:
    """The three suffixes that were waved through, and the shape it produced.

    ``detect`` skipped its own empty-file guard whenever the name ended in a text or
    delimited suffix, on the theory that an empty ``.txt`` is legitimately empty. What
    a caller got was **zero chunks, no exception, and an untouched ``Diagnostics``** --
    no note, no truncation, ``lost_data`` False -- while the identical zero bytes named
    ``.html`` or ``.pdf`` raised ``CorruptDocument("file is empty (0 bytes)")``. Eight
    zero-byte files in the fixture corpus took that path.

    That is the failure mode this library exists to remove, and it contradicted
    ``chunk()``'s own docstring, which lists "an empty file" among the
    ``CorruptDocument`` cases raised *by the call*.
    """
    empty = tmp_path / f"empty{suffix}"
    empty.write_bytes(b"")
    exc = _raises_before_iteration(call, empty)
    assert isinstance(exc, CorruptDocument)
    assert "empty" in str(exc)


@ENTRY_POINTS
def test_a_truncated_archive_raises_at_the_call(call, tmp_path: Path) -> None:
    """The interrupted download: right magic bytes, no usable archive behind them."""
    half = tmp_path / "half.docx"
    whole = fixtures.tiny_docx(["a paragraph that makes the file long enough to halve"] * 40)
    half.write_bytes(whole[: len(whole) // 2])
    exc = _raises_before_iteration(call, half)
    assert isinstance(exc, CorruptDocument)


@ENTRY_POINTS
def test_a_plain_zip_renamed_raises_at_the_call(call, tmp_path: Path) -> None:
    renamed = tmp_path / "holiday-photos.docx"
    with zipfile.ZipFile(renamed, "w") as archive:
        archive.writestr("readme.txt", b"not an office document")
    exc = _raises_before_iteration(call, renamed)
    assert isinstance(exc, UnsupportedFormat)


@ENTRY_POINTS
def test_unrecognisable_bytes_raise_at_the_call(call) -> None:
    _raises_before_iteration(call, b"\x00\x01\x02\x03" * 200, name="from-a-queue.bin")


def test_extract_refuses_a_spreadsheet_at_the_call(tmp_path: Path) -> None:
    """D6: a sheet's retrieval unit is a row group, which is a chunking decision.

    This one is a programming error rather than a bad document, so surfacing it late
    is worse than for the others -- the caller wrote the wrong function name and
    should hear about it on the line they wrote.
    """
    book = tmp_path / "book.xlsx"
    book.write_bytes(fixtures.tiny_xlsx([["region", "spend"], ["north", "12"]]))
    exc = _raises_before_iteration(diceo.extract, book)
    assert isinstance(exc, UnsupportedFormat)
    assert "diceo.chunk()" in str(exc)


def test_chunk_accepts_the_spreadsheet_extract_refused(tmp_path: Path) -> None:
    book = tmp_path / "book.xlsx"
    book.write_bytes(fixtures.tiny_xlsx([["region", "spend"], ["north", "12"]]))
    assert [piece.text for piece in diceo.chunk(book)]


@ENTRY_POINTS
def test_a_healthy_document_is_still_lazy(call, tmp_path: Path) -> None:
    """Rule 2 has not been traded away: the call reads no content.

    If the entry point had become eager all the way down, the whole document would be
    parsed here and ``islice(chunk(path), 5)`` would stop meaning anything.
    """
    doc = tmp_path / "report.docx"
    doc.write_bytes(
        fixtures.tiny_docx(
            [f"paragraph number {n} with enough text to count" for n in range(400)]
        )
    )

    report = diceo.Diagnostics()
    stream = call(doc, diagnostics=report)
    assert report.blocks == 0, "the call parsed content it did not need to"

    first = next(iter(stream))
    assert first
    assert report.blocks > 0


@ENTRY_POINTS
def test_the_format_is_known_before_iteration(call, tmp_path: Path) -> None:
    """A crawler routing on format should not have to consume the document to learn it."""
    doc = tmp_path / "report.docx"
    doc.write_bytes(fixtures.tiny_docx(["one paragraph"]))
    report = diceo.Diagnostics()
    call(doc, diagnostics=report)
    assert report.format == "docx"


def test_a_mid_document_failure_stays_lazy_and_becomes_diagnostics(tmp_path: Path) -> None:
    """The other half of the rule, and the one that keeps the first half honest.

    A document that goes wrong part-way through must NOT raise -- not at the call and
    not during iteration. Rule 3: yield what was readable, count what was not.
    """
    good = fixtures.tiny_docx(
        [f"paragraph {n} with enough words to survive chunking" for n in range(60)]
    )
    wounded = tmp_path / "wounded.docx"
    # The archive stays intact and openable; only the main part is cut short, so the
    # failure is genuinely mid-document rather than at the container.
    with zipfile.ZipFile(io.BytesIO(good)) as source, zipfile.ZipFile(wounded, "w") as out:
        for member in source.namelist():
            payload = source.read(member)
            if member.endswith("document.xml"):
                payload = payload[: len(payload) * 2 // 3]
            out.writestr(member, payload)

    report = diceo.Diagnostics()
    pieces = list(diceo.chunk(wounded, diagnostics=report))

    assert pieces, "rule 3: the readable remainder must still come back"
    assert report.lost_data
    assert report.truncated


def test_the_source_is_closed_when_the_generator_is_abandoned(tmp_path: Path) -> None:
    """The eager open hands an open file to the generator; closing must still happen.

    A caller who takes five chunks and walks away is the documented use
    (``islice(chunk(path), 5)``), so the file descriptor cannot depend on the
    iterator being exhausted.
    """
    doc = tmp_path / "report.docx"
    doc.write_bytes(
        fixtures.tiny_docx([f"paragraph {n} with several words in it" for n in range(200)])
    )

    stream = diceo.chunk(doc)
    next(iter(stream))
    stream.close()  # type: ignore[union-attr]

    # The real assertion is that this does not leak: on Windows an unclosed handle
    # would make the unlink fail, and on Linux ResourceWarning is raised under -W error.
    doc.unlink()
    assert not doc.exists()
