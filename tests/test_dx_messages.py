"""Does the message tell the developer what is actually wrong?

An exception type is half a contract. The other half is whether the sentence attached to
it sends the reader somewhere useful, and that half is not covered by asserting the type
-- a test can be green while the message says the opposite of the truth.

The case that motivated this file: a **password-protected .xlsx** is an OOXML package
wrapped in an OLE2 container, so it reached the OLE2 branch of ``detect()`` and the caller
was told, with complete confidence:

    this is an OLE2 compound file (a pre-2007 Office document). Only legacy .xls is
    supported; rename it with its real extension or convert it with LibreOffice

Every clause is wrong. It is not pre-2007, renaming does nothing, and LibreOffice will
ask for the password the message never mentioned. ``EncryptedDocument`` existed the whole
time and was unreachable from ``detect()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import diceo
from diceo.errors import EncryptedDocument, UnsupportedFormat


def _ole2(names: list[str]) -> bytes:
    """A minimal MS-CFB container whose directory names the given streams.

    Enough of the header for ``_ole2_holds_encrypted_package`` to find the directory
    (sector shift at offset 30, directory start at 48), and one directory sector of
    128-byte entries with UTF-16LE names. Built from the standard library so the suite
    still runs on a fresh clone with no corpus.
    """
    header = bytearray(512)
    header[0:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header[26:28] = (0x003E).to_bytes(2, "little")
    header[28:30] = (0xFFFE).to_bytes(2, "little")
    header[30:32] = (9).to_bytes(2, "little")  # 1 << 9 == 512-byte sectors
    header[32:34] = (6).to_bytes(2, "little")
    header[44:48] = (1).to_bytes(4, "little")
    header[48:52] = (0).to_bytes(4, "little")  # directory starts at sector 0
    directory = bytearray(512)
    for index, name in enumerate(names):
        offset = index * 128
        raw = name.encode("utf-16-le") + b"\x00\x00"
        directory[offset : offset + len(raw)] = raw
        directory[offset + 64 : offset + 66] = len(raw).to_bytes(2, "little")
        directory[offset + 66] = 2  # STGTY_STREAM
    return bytes(header) + bytes(directory)


def _error(payload: bytes, name: str, tmp_path: Path):
    path = tmp_path / name
    path.write_bytes(payload)
    with pytest.raises(diceo.DiceoError) as caught:
        list(diceo.chunk(path))
    return caught.value


@pytest.mark.parametrize("name", ["locked.xlsx", "locked.docx", "locked.pptx"])
def test_a_password_protected_package_says_it_has_a_password(name: str, tmp_path: Path) -> None:
    exc = _error(_ole2(["Root Entry", "EncryptedPackage", "EncryptionInfo"]), name, tmp_path)
    assert isinstance(exc, EncryptedDocument)
    message = str(exc).lower()
    assert "password" in message, "the one fact the reader needs"
    assert "pre-2007" not in message, "the old message's central claim was false"


def test_encryption_info_alone_is_enough(tmp_path: Path) -> None:
    """Office writes both streams, but a file carrying only the key-derivation one is
    still protected and must not fall through to the wrong branch."""
    exc = _error(_ole2(["Root Entry", "EncryptionInfo"]), "locked.xlsx", tmp_path)
    assert isinstance(exc, EncryptedDocument)


def test_a_genuine_legacy_word_file_is_still_named_correctly(tmp_path: Path) -> None:
    """The guard against fixing one wrong message by breaking a right one."""
    exc = _error(_ole2(["Root Entry", "WordDocument", "1Table"]), "report.doc", tmp_path)
    assert isinstance(exc, UnsupportedFormat)
    assert "pre-2007 Word" in str(exc)
    assert "libreoffice" in str(exc).lower(), "a declined format must say what to do"


def test_an_unremarkable_ole2_file_is_unchanged(tmp_path: Path) -> None:
    exc = _error(_ole2(["Root Entry", "SomethingElse"]), "mystery.bin", tmp_path)
    assert isinstance(exc, UnsupportedFormat)


# --------------------------------------------------------------------------- #
# the general property, across every message the package can produce
# --------------------------------------------------------------------------- #

ACTIONABLE = (
    "convert",
    "rename",
    "unzip",
    "export",
    "open it with",
    "supply",
    "use diceo.chunk",
    "pass a path",
    "pass the members",
    "set ",
)


@pytest.mark.parametrize(
    ("payload", "name"),
    [
        (b"", "empty.pdf"),
        (b"{\\rtf1\\ansi hello}", "note.rtf"),
        (b"PK\x03\x04" + b"\x00" * 200, "half.docx"),
        (b"\x00\x01\x02\x03" * 100, "blob.bin"),
    ],
)
def test_every_refusal_names_the_file_and_says_something_concrete(
    payload: bytes, name: str, tmp_path: Path
) -> None:
    """A refusal must at minimum identify which file it is about.

    A pipeline logging one line per failure over ten thousand documents cannot use a
    message that does not name its subject, and ``DiceoError`` carries the source
    precisely so the caller does not have to thread it through.
    """
    exc = _error(payload, name, tmp_path)
    assert name in str(exc), "the message must name the file it is about"
    assert len(str(exc)) > len(name) + 20, "and say more than the file name"


def test_a_declined_format_always_offers_a_next_step(tmp_path: Path) -> None:
    """For the formats we recognise and refuse on purpose, "unsupported" is not enough:
    we know exactly what the file is, so we owe the reader the conversion command."""
    for payload, name in [
        (b"{\\rtf1\\ansi hello}", "note.rtf"),
        (_ole2(["Root Entry", "WordDocument"]), "old.doc"),
        (_ole2(["Root Entry", "PowerPoint Document"]), "old.ppt"),
    ]:
        message = str(_error(payload, name, tmp_path)).lower()
        assert any(word in message for word in ACTIONABLE), f"{name}: no next step offered"
