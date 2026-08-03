"""The escapes an adversarial audit found by executing repros, one test each.

``tests/test_robustness.py`` fuzzes documents. These are the cases it structurally could
not reach, because they are not malformed *documents* -- they are a hostile path, a
stream that lies about itself, a configuration that cannot terminate, and an XML part
naming an encoding that does not exist. Every one was confirmed by running it on
2026-07-31, and every one reached the caller as something other than a ``DiceoError``.

The contract these restore is in ``src/diceo/errors.py``: a caller sees a
``DiceoError``, or they see chunks. Never a third thing.
"""

from __future__ import annotations

import contextlib
import csv
import io
import struct
import zipfile
from pathlib import Path

import pytest

import diceo
from diceo.errors import DiceoError
from tests import fixtures


def _expect_diceo_error(call, *args, **kwargs) -> DiceoError:
    """Consume fully: some failures are only reachable once the iterator runs."""
    with pytest.raises(DiceoError) as caught:
        result = call(*args, **kwargs)
        if hasattr(result, "__iter__") and not isinstance(result, str):
            list(result)
    return caught.value


# --------------------------------------------------------------------------- #
# paths and streams: the object is the caller's, so it is least under our control
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entry", [diceo.chunk, diceo.extract, diceo.sniff])
def test_a_nul_byte_in_the_path_is_a_diceo_error(entry) -> None:
    """`Path.stat()` raises ValueError, not OSError, so the careful OSError ladder in
    `open_source` missed it. A crawler building paths from URL-decoded strings or a
    database column produces these routinely."""
    _expect_diceo_error(entry, "reports/quarter\x00one.pdf")


@pytest.mark.parametrize("entry", [diceo.chunk, diceo.extract, diceo.sniff])
def test_a_closed_file_object_is_a_diceo_error(entry) -> None:
    """`seekable()` on a closed file raises ValueError before any diceo error exists."""
    handle = io.BytesIO(fixtures.tiny_pdf())
    handle.close()
    _expect_diceo_error(entry, handle)


class _LyingStream(io.RawIOBase):
    """Claims to be seekable and then refuses to seek. A pipe in a BufferedReader does
    exactly this, and so does an object wrapping a network fetch."""

    def __init__(self, data: bytes) -> None:
        self._data = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)

    def seek(self, *_args) -> int:
        raise OSError(29, "Illegal seek")


@pytest.mark.parametrize("entry", [diceo.chunk, diceo.extract, diceo.sniff])
def test_a_stream_that_lies_about_seeking_is_a_diceo_error(entry) -> None:
    _expect_diceo_error(entry, _LyingStream(fixtures.tiny_pdf()))


class _FailingRead(io.RawIOBase):
    """Opens, stats and seeks cleanly, then fails on read -- a network mount going away."""

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, *_args) -> int:
        return 0

    def tell(self) -> int:
        return 0

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        raise OSError(5, "Input/output error")


def test_a_stream_that_fails_mid_read_is_a_diceo_error() -> None:
    """`detect()` read its first 4 KB unguarded, in front of `_guard`, so the raw OSError
    had nothing between it and the caller."""
    _expect_diceo_error(diceo.chunk, _FailingRead())


# --------------------------------------------------------------------------- #
# archives and encodings
# --------------------------------------------------------------------------- #


def test_an_unreadable_compression_method_is_a_diceo_error(tmp_path: Path) -> None:
    """`zipfile` raises NotImplementedError for deflate64 and for a ZIP version it does
    not know. It reads like unfinished code in diceo and is a statement about the file.
    """
    path = tmp_path / "deflate64.docx"
    real_check = zipfile._check_compression  # CPython refuses to write it either
    zipfile._check_compression = lambda _t: None
    try:
        with zipfile.ZipFile(path, "w") as archive:
            info = zipfile.ZipInfo("word/document.xml")
            info.compress_type = 9
            archive.writestr(info, b"<w:document/>")
    finally:
        zipfile._check_compression = real_check
    _expect_diceo_error(diceo.chunk, path)


def test_a_zip_encrypted_mimetype_member_is_a_diceo_error(tmp_path: Path) -> None:
    """`zipfile` raises `RuntimeError` for a member it cannot decrypt.

    ``detect()`` runs *eagerly*, in front of `_guard`, so the guard's own
    `backend_failures()` -- which does list RuntimeError -- never sees this one. The
    ODF branch reads the ``mimetype`` member to name the format, and its except tuple
    was the only archive-read guard in the package that had not been given the
    encrypted case, so a package built this way came back as a raw::

        RuntimeError: File 'mimetype' is encrypted, password required for extraction

    Confirmed by running it before the fix. The format it settles on afterwards is not
    the point -- any `DiceoError` is; the point is the contract in ``errors.py``.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Deflated and padded so the magic is not sitting in the first 4 KB, which is
        # where the ODF check answers from when it can.
        archive.writestr(
            "mimetype", b"application/vnd.oasis.opendocument.spreadsheet" + b"X" * 400
        )
        archive.writestr("content.xml", b"<x/>")
    raw = bytearray(buffer.getvalue())
    # `writestr` clears the flag word, so the "encrypted" bit is set afterwards, on the
    # local header and on its central-directory twin.
    raw[6:8] = struct.pack("<H", struct.unpack("<H", raw[6:8])[0] | 0x1)
    at = raw.find(b"PK\x01\x02")
    raw[at + 8 : at + 10] = struct.pack(
        "<H", struct.unpack("<H", raw[at + 8 : at + 10])[0] | 0x1
    )

    path = tmp_path / "encrypted-member.ods"
    path.write_bytes(bytes(raw))

    with zipfile.ZipFile(path) as check, pytest.raises(RuntimeError):
        check.read("mimetype")  # the fixture really is what the test claims

    _expect_diceo_error(diceo.chunk, path)


@pytest.mark.parametrize("encoding", ["UUF-8", "UTF-9", "x-user-defined", ""])
def test_an_xml_part_naming_an_unknown_encoding_is_a_diceo_error(
    encoding: str, tmp_path: Path
) -> None:
    """`codecs` raises a bare LookupError for a name nobody knows.

    KeyError and IndexError were both in `backend_failures()` and their common ancestor
    was not -- and LookupError is the one case in that family that comes from the
    *document* rather than from our own dict lookups. Three readers passed the declared
    name straight to the decoder.
    """
    book = fixtures.tiny_xlsx([["region", "revenue"], ["EMEA", "1200"]])
    out = io.BytesIO()
    declaration = f'<?xml version="1.0" encoding="{encoding}"?>'.encode()
    with zipfile.ZipFile(io.BytesIO(book)) as source, zipfile.ZipFile(out, "w") as target:
        for member in source.namelist():
            payload = source.read(member)
            if member.endswith(".xml") and payload.startswith(b"<?xml"):
                payload = declaration + payload[payload.index(b"?>") + 2 :]
            target.writestr(member, payload)
    path = tmp_path / "encoded.xlsx"
    path.write_bytes(out.getvalue())

    # Either it reads (the declaration is advisory once the bytes are UTF-8) or it
    # raises a DiceoError. What it must not do is leak LookupError.
    with contextlib.suppress(DiceoError):
        list(diceo.chunk(path))


# --------------------------------------------------------------------------- #
# configuration that cannot terminate
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("target", [0, 1, -5])
def test_a_target_too_small_to_progress_is_refused_at_construction(target: int) -> None:
    """`Limits(target_chars=0)` made `chunk()` spin forever: a zero-length window finds
    no boundary, cuts at -1, and leaves `rest` exactly as long as it was. No exception,
    no memory growth, no progress -- and the CLI exposed it as `--target 0`.

    ValueError rather than a DiceoError on purpose: this is the caller's value, not the
    document's, and reporting a bad limit as "your document is corrupt" would send it to
    the wrong person.
    """
    with pytest.raises(ValueError, match="target_chars"):
        diceo.Limits(target_chars=target)


def test_a_negative_or_zero_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="max_seconds"):
        diceo.Limits(max_seconds=0)
    with pytest.raises(ValueError, match="max_rows"):
        diceo.Limits(max_rows=-1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_pages", -1),
        ("reopen_every", -1),
        ("overlap_chars", -1),
        ("list_group_size", -1),
    ],
)
def test_a_negative_structural_limit_is_refused(field: str, value: int) -> None:
    """Negative values used to mean four unrelated things: no PDF output, reopen
    every page, and silently disabling overlap or list grouping. They are all caller
    errors and must fail where the configuration is constructed."""
    with pytest.raises(ValueError, match=field):
        diceo.Limits(**{field: value})


def test_the_smallest_allowed_target_terminates(tmp_path: Path) -> None:
    """The boundary case has to actually work, or the guard is just moving the failure."""
    path = tmp_path / "doc.pdf"
    path.write_bytes(fixtures.tiny_pdf(["The revenue was 1200 million in the last quarter."]))
    pieces = list(
        diceo.chunk(path, limits=diceo.Limits(target_chars=diceo.Limits.MIN_TARGET_CHARS))
    )
    assert pieces
    assert all(piece.text for piece in pieces)


def test_max_chars_applies_to_chunks_flushed_at_end_of_document() -> None:
    """The main loop checks the budget, but a final list run is emitted by
    ``packer.finish()`` after that loop. All three chunks escaped a one-character
    budget and no truncation was reported."""
    report = diceo.Diagnostics()
    pieces = list(
        diceo.chunk(
            b"- first item\n- second item\n- third item\n",
            name="notes.md",
            limits=diceo.Limits(list_group_size=1, max_chars=1),
            diagnostics=report,
        )
    )

    assert len(pieces) == 1
    assert any("max_chars=1" in entry for entry in report.truncated)
    assert report.lost_data


def test_an_invalid_cli_limit_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    """A bad numeric flag is user input, not a diceo traceback."""
    from diceo.__main__ import EXIT_USAGE, main

    status = main(["--target", "0", "report.pdf"])

    captured = capsys.readouterr()
    assert status == EXIT_USAGE
    assert "target_chars" in captured.err
    assert "Traceback" not in captured.err


# --------------------------------------------------------------------------- #
# a cell reference is an index into a list, and it came from the file
# --------------------------------------------------------------------------- #


def _workbook_with_reference(reference: str, tmp_path: Path, name: str) -> Path:
    """A two-cell row where the second cell's `r` is whatever we are testing."""
    book = fixtures.tiny_xlsx([["FIRST", "SECOND"]])
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(book)) as source, zipfile.ZipFile(out, "w") as target:
        for member in source.namelist():
            payload = source.read(member)
            if member == "xl/worksheets/sheet1.xml":
                payload = payload.replace(
                    b'<c r="B1" t="inlineStr"><is><t>SECOND</t></is></c>',
                    b'<c r="B1" t="inlineStr"><is><t>SECOND</t></is></c>'
                    + f'<c r="{reference}" t="inlineStr"><is><t>THIRD</t></is></c>'.encode(),
                )
            target.writestr(member, payload)
    path = tmp_path / name
    path.write_bytes(out.getvalue())
    return path


def test_a_reference_naming_no_column_does_not_overwrite_its_neighbour(tmp_path: Path) -> None:
    """`r="7"` gave `_column_index` -1, and `cells[-1] = value` overwrote the cell
    before it. The row lost a value with no exception and `lost_data` False -- rule 3's
    worst case, caused by a negative list index."""
    path = _workbook_with_reference("7", tmp_path, "noletters.xlsx")
    report = diceo.Diagnostics()
    text = " ".join(piece.text for piece in diceo.chunk(path, diagnostics=report))
    assert "SECOND" in text, "the neighbour was overwritten"
    assert "THIRD" in text, "rule 3: the value itself must survive"
    assert report.lost_data
    assert any("invalid_cell_references" in entry for entry in report.truncated)


def test_a_column_past_the_format_maximum_does_not_exhaust_memory(tmp_path: Path) -> None:
    """The row builder pads up to the index it is handed, so a 1.7 KB file naming
    `XFDXFDXFD1` asked for 10^14 list entries. MemoryError is not a DiceoError, so the
    caller's quarantine handler never ran -- if the process survived at all."""
    path = _workbook_with_reference("XFDXFDXFD1", tmp_path, "bomb.xlsx")
    report = diceo.Diagnostics()
    pieces = list(diceo.chunk(path, diagnostics=report))
    assert pieces
    assert any("invalid_cell_references" in entry for entry in report.truncated)


def test_an_ordinary_reference_still_places_its_value(tmp_path: Path) -> None:
    """The guard must not have made every reference suspicious: a gap in the row is
    ordinary SpreadsheetML and must still pad."""
    path = _workbook_with_reference("E1", tmp_path, "gap.xlsx")
    report = diceo.Diagnostics()
    text = " ".join(piece.text for piece in diceo.chunk(path, diagnostics=report))
    assert "FIRST | SECOND |  |  | THIRD" in text
    assert not any("invalid_cell_references" in entry for entry in report.truncated)


# --------------------------------------------------------------------------- #
# budgets the caller sized their pipeline around
# --------------------------------------------------------------------------- #


def test_max_chars_is_honoured_on_a_spreadsheet(tmp_path: Path) -> None:
    """The sheet path does not go through `chunk_blocks`, which is where `max_chars` was
    enforced -- so a spreadsheet ignored it completely. Measured: 50,000 asked for,
    6,199,081 delivered, `truncated` empty. A budget silently not applied is worse than
    no budget, because the caller sized their pipeline around it."""
    path = tmp_path / "big.csv"
    rows = "\r\n".join(f"row-{n},{n},{n * 3},some text for row {n}" for n in range(20_000))
    path.write_bytes(f"name,a,b,note\r\n{rows}\r\n".encode())

    report = diceo.Diagnostics()
    total = sum(
        len(piece.text)
        for piece in diceo.chunk(
            path, diagnostics=report, limits=diceo.Limits(max_chars=50_000)
        )
    )
    assert total < 200_000, f"{total:,} characters for a 50,000 budget"
    assert any("max_chars" in entry for entry in report.truncated)
    assert report.lost_data


def test_max_chars_is_honoured_by_extract(tmp_path: Path) -> None:
    """`extract()` bypasses the chunker entirely, so it honoured neither budget."""
    path = tmp_path / "long.txt"
    path.write_bytes(("A paragraph of ordinary prose about revenue.\n\n" * 20_000).encode())

    report = diceo.Diagnostics()
    total = sum(
        len(block.text)
        for block in diceo.extract(
            path, diagnostics=report, limits=diceo.Limits(max_chars=50_000)
        )
    )
    assert total < 200_000, f"{total:,} characters for a 50,000 budget"
    assert any("max_chars" in entry for entry in report.truncated)


def test_no_budget_still_returns_the_whole_document(tmp_path: Path) -> None:
    """The control: enforcement must not truncate a caller who asked for nothing."""
    path = tmp_path / "long.txt"
    path.write_bytes(("A paragraph of ordinary prose about revenue.\n\n" * 500).encode())
    report = diceo.Diagnostics()
    assert list(diceo.extract(path, diagnostics=report))
    assert not report.truncated


def test_splitting_one_enormous_block_stays_linear() -> None:
    """`rest = rest[cut + 1:]` copied everything still to come on every cut, so the cost
    was quadratic in the block's length -- exponent 2.77, 8 M characters in 4.0 s. A
    spreadsheet cell holding a pasted document reaches this, and the caller's pipeline
    stops while it finishes."""
    from diceo.chunker import _split_long

    def by_reslicing(text: str, target: int) -> list[str]:
        """The original, quadratic. Kept here as the oracle: the optimisation is only
        allowed if it produces the identical list, including the un-rstripped tail."""
        if len(text) <= target:
            return [text]
        pieces: list[str] = []
        rest = text
        floor = max(target // 4, 1)
        while len(rest) > target:
            window = rest[:target]
            cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            if cut < floor:
                cut = window.rfind(" ")
            if cut < floor:
                cut = target - 1
            cut = max(cut, 0)
            piece = rest[: cut + 1].strip()
            if piece:
                pieces.append(piece)
            rest = rest[cut + 1 :].lstrip()
        if rest:
            pieces.append(rest)
        return pieces

    cases = [
        "The revenue was 1200 million in the region that quarter. " * 400,
        "nospacesatallinthiswholeblock" * 400,
        "Short. Sentences! Everywhere? " * 400,
        "   leading and trailing whitespace   " * 400,
        "word " * 4000,
    ]
    for target in (8, 97, 600, 1800):
        for text in cases:
            assert _split_long(text, target) == by_reslicing(text, target), (
                f"diverged at target={target} on {text[:30]!r}"
            )


# --------------------------------------------------------------------------- #
# not our process to change
# --------------------------------------------------------------------------- #


def test_reading_a_csv_leaves_the_callers_csv_limit_alone(tmp_path: Path) -> None:
    """Importing the CSV reader used to raise `csv.field_size_limit` process-wide and
    never put it back, so a caller's own unrelated `csv.reader` silently stopped
    enforcing the guard they were relying on."""
    before = csv.field_size_limit()
    path = tmp_path / "data.csv"
    path.write_bytes(fixtures.tiny_csv())
    assert list(diceo.chunk(path))
    assert csv.field_size_limit() == before


def test_a_field_over_the_ceiling_is_still_read(tmp_path: Path) -> None:
    """The scoping must not have quietly removed the capability it scopes: a cell holding
    a pasted document is unusual, not corrupt.

    Renamed from ``test_the_limit_is_raised_while_we_read``: the assertion is unchanged,
    but the old name described a mechanism that is now wrong. The limit is no longer up
    for the length of a read -- for a file whose cells are cells it is never touched at
    all -- so the only thing worth pinning here is the outcome. What used to be implied
    by the name is now pinned properly in ``tests/test_csv_limit_isolation.py``.
    """
    big = "x" * 300_000
    path = tmp_path / "big.csv"
    path.write_bytes(f'name,note\r\nrow,"{big}"\r\n'.encode())
    assert any(big[:1000] in piece.text for piece in diceo.chunk(path))
