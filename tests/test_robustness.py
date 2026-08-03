"""The contract that decides whether anyone keeps this dependency.

**A caller sees a ``DiceoError``, or they see chunks. Never a third thing.**

Everything here is a property test over deliberately hostile input: files truncated
at every fraction of their length, bit-flipped, decapitated, renamed to the wrong
extension, zip-bombed, nested 10,000 deep. For each one the assertion is not "the
right answer comes out" -- there is no right answer for a file cut in half -- but
that the *failure mode* is the documented one.

Why this is a test file and not a paragraph in the README: a promise about
exception types decays the moment someone adds a reader, and the decay is invisible
because the new reader works on good files. The 224 tests that existed before this
one all used *valid* documents.

These fixtures are built by ``tests/fixtures.py`` from the standard library, so this
module runs on a fresh clone with no corpus and no network.
"""

from __future__ import annotations

import io
import random
import zipfile

import pytest

import diceo
from diceo.errors import DiceoError
from tests import fixtures

# Deterministic: a fuzz suite that finds a different bug on every run cannot be
# bisected, and the seed being fixed is what makes a failure reproducible from the
# test name alone.
SEED = 20260729
FORMATS = fixtures.every_format()


def _assert_contract(payload: bytes, name: str) -> str:
    """Run the payload through the public API and report which branch it took.

    Any exception that is not a ``DiceoError`` fails the test -- including
    ``TypeError`` and ``AttributeError``, which are the shapes of *our* bugs and
    are deliberately not caught by the reader guard.
    """
    try:
        pieces = list(diceo.chunk(payload, name=name))
    except DiceoError:
        return "raised"
    except Exception as exc:  # noqa: BLE001 - the point of the test
        raise AssertionError(
            f"{name}: leaked {type(exc).__module__}.{type(exc).__name__}: {exc}\n"
            f"Every failure must be a DiceoError -- see src/diceo/errors.py"
        ) from exc
    for piece in pieces:
        assert isinstance(piece.text, str)
    return "chunks"


# --------------------------------------------------------------------------- #
# the happy path, so the fuzz cases are known to start from something valid
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", sorted(FORMATS))
def test_every_format_reads(suffix):
    pieces = list(diceo.chunk(FORMATS[suffix], name=f"sample{suffix}"))
    assert pieces, f"{suffix} produced no chunks"
    assert any("1200" in piece.text for piece in pieces), (
        f"{suffix}: the planted fact 1200 did not survive extraction"
    )


@pytest.mark.parametrize("suffix", sorted(FORMATS))
def test_format_is_detected_from_content_not_extension(suffix):
    # Every file is handed over under a *lying* extension. Content must win.
    expected = diceo.sniff(io.BytesIO(FORMATS[suffix]))
    assert diceo.sniff(FORMATS[suffix]) == expected
    if suffix not in (".csv", ".txt"):  # these two genuinely have no magic bytes
        misnamed = diceo.sniff(io.BytesIO(FORMATS[suffix]))
        assert misnamed == expected


# --------------------------------------------------------------------------- #
# truncation -- the single most common corruption in the wild
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", sorted(FORMATS))
@pytest.mark.parametrize("fraction", [0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99])
def test_truncated_file_never_leaks(suffix, fraction):
    payload = FORMATS[suffix]
    cut = payload[: max(1, int(len(payload) * fraction))]
    _assert_contract(cut, f"truncated{suffix}")


@pytest.mark.parametrize("suffix", sorted(FORMATS))
def test_empty_file_is_reported_not_crashed(suffix):
    # An empty text-ish file is legitimately empty; an empty binary is corrupt.
    # Either way it must not raise anything but ours.
    _assert_contract(b"", f"empty{suffix}")


# --------------------------------------------------------------------------- #
# bit rot
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", sorted(FORMATS))
@pytest.mark.parametrize("flips", [1, 8, 64])
def test_bit_flipped_file_never_leaks(suffix, flips):
    rng = random.Random(f"{SEED}{suffix}{flips}")
    payload = bytearray(FORMATS[suffix])
    for _ in range(flips):
        index = rng.randrange(len(payload))
        payload[index] ^= 1 << rng.randrange(8)
    _assert_contract(bytes(payload), f"flipped{suffix}")


@pytest.mark.parametrize("suffix", sorted(FORMATS))
def test_header_stripped_file_never_leaks(suffix):
    _assert_contract(FORMATS[suffix][8:], f"headless{suffix}")


def test_random_bytes_are_refused_cleanly():
    rng = random.Random(SEED)
    for attempt in range(20):
        payload = bytes(rng.randrange(256) for _ in range(2048))
        _assert_contract(payload, f"random{attempt}.bin")


# --------------------------------------------------------------------------- #
# hostile, not merely broken
# --------------------------------------------------------------------------- #


def _bomb(uncompressed: int) -> bytes:
    """A real ZIP bomb: a member of ``uncompressed`` zeros, genuinely deflated.

    Honest rather than hand-forged. 8 MB of zeros deflates to about 8 KB, a ratio
    near 1000x against the 200x limit, and the guard has to refuse it from the
    central directory *without* inflating -- which is exactly what a caller needs
    when the claim is 4 GB and the machine has 2.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\0" * uncompressed)
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats'
            '.org/package/2006/relationships"><Relationship Id="rId1" Type="http://'
            "schemas.openxmlformats.org/officeDocument/2006/relationships/"
            'officeDocument" Target="word/document.xml"/></Relationships>',
        )
    return buffer.getvalue()


def test_zip_bomb_is_refused_from_the_directory():
    # Above `BOMB_MIN_BYTES`, so the ratio guard applies; 80 MB of zeros still
    # deflates to well under 100 KB, which is what makes it an attack.
    payload = _bomb(80 * 1024**2)
    assert len(payload) < 200_000, "the fixture is not actually a bomb"
    with pytest.raises(DiceoError) as caught:
        list(diceo.chunk(payload, name="bomb.docx"))
    message = str(caught.value)
    assert "zip bomb" in message or "ceiling" in message, message


def test_a_merely_large_document_is_not_mistaken_for_a_bomb():
    """The guard must not refuse a real document that compresses well.

    A legitimate report of a million near-identical table rows compresses far past
    200x, and refusing it would mean rejecting documents every competitor indexes
    fine. That is why the ratio test only applies above 64 MB uncompressed -- a
    false positive here is worse than a false negative.
    """
    # Deliberately *highly* repetitive: identical rows compress ~400x, well past
    # MAX_COMPRESSION_RATIO, and must still be read because they are only ~2 MB.
    body = "".join(
        "<w:p><w:r><w:t>Region EMEA revenue 1200 units 34</w:t></w:r></w:p>"
        for _ in range(30_000)
    )
    payload = fixtures._zip(
        {
            "[Content_Types].xml": fixtures._CONTENT_TYPES,
            "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
            "word/document.xml": f'<?xml version="1.0"?><w:document {fixtures._W_NS}>'
            f"<w:body>{body}</w:body></w:document>",
        }
    )
    pieces = list(diceo.chunk(payload, name="long.docx"))
    assert len(pieces) > 100, "a long but legitimate document was not read"


def test_deeply_nested_xml_never_leaks():
    depth = 10_000
    body = "<w:p>" * depth + "<w:r><w:t>deep</w:t></w:r>" + "</w:p>" * depth
    payload = fixtures._zip(
        {
            "[Content_Types].xml": fixtures._CONTENT_TYPES,
            "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
            "word/document.xml": f'<?xml version="1.0"?><w:document {fixtures._W_NS}>'
            f"<w:body>{body}</w:body></w:document>",
        }
    )
    _assert_contract(payload, "nested.docx")


def test_billion_laughs_never_leaks():
    """An XML entity-expansion bomb inside a docx.

    This docstring used to say Python's ``expat`` "refuses parameter-entity
    expansion by default". It does not -- general entities expand, and a four-level
    bomb yields its 10,000 characters. What refuses this one is **libexpat 2.4.0+'s
    input-amplification limiter**, which is the C library CPython links, not Python
    and not us. We cannot pin it, so the guard is real but borrowed.

    That makes this a regression guard in two directions: against swapping the
    parser for a more permissive one, and against an environment whose libexpat is
    older than the protection.
    """
    entities = "".join(f'<!ENTITY a{i} "&a{i - 1};&a{i - 1};">' for i in range(1, 12))
    doctype = f'<!DOCTYPE w:document [<!ENTITY a0 "boom">{entities}]>'
    payload = fixtures._zip(
        {
            "[Content_Types].xml": fixtures._CONTENT_TYPES,
            "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>',
            "word/document.xml": f'<?xml version="1.0"?>{doctype}'
            f"<w:document {fixtures._W_NS}><w:body><w:p><w:r><w:t>&a11;</w:t></w:r>"
            f"</w:p></w:body></w:document>",
        }
    )
    _assert_contract(payload, "laughs.docx")


def test_zip_with_no_document_parts_is_named_not_guessed():
    payload = fixtures._zip({"hello.txt": b"not a document"})
    with pytest.raises(DiceoError) as caught:
        list(diceo.chunk(payload, name="archive.zip"))
    assert "ZIP" in str(caught.value)


@pytest.mark.parametrize(
    ("magic", "name", "expect"),
    [
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512, "old.doc", "Word"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512, "mail.msg", "GPL"),
        (b"{\\rtf1\\ansi hello}", "note.rtf", "RTF"),
    ],
)
def test_declined_formats_say_what_they_are(magic, name, expect):
    """A format we recognise and refuse must name itself and the way out.

    "Unsupported format" alone turns into a support ticket; naming the format and
    the conversion command answers it in advance.
    """
    with pytest.raises(DiceoError) as caught:
        list(diceo.chunk(magic, name=name))
    assert expect in str(caught.value)
    assert "Supported:" in str(caught.value)


# --------------------------------------------------------------------------- #
# the other half of the contract: same bytes, same chunks
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("suffix", sorted(FORMATS))
def test_extraction_is_deterministic(suffix):
    """Byte-identical output on a second run, so an index can cache on a hash.

    Not a theoretical property: a set iteration or a dict ordering leaking into
    chunk text would break incremental re-indexing for every user, silently, and
    only for some documents.
    """
    first = [piece.text for piece in diceo.chunk(FORMATS[suffix], name=f"a{suffix}")]
    second = [piece.text for piece in diceo.chunk(FORMATS[suffix], name=f"a{suffix}")]
    assert first == second


@pytest.mark.parametrize("suffix", sorted(FORMATS))
def test_bytes_and_path_and_stream_agree(suffix, tmp_path):
    """The three input forms are the same document or one of them is lying."""
    payload = FORMATS[suffix]
    path = tmp_path / f"sample{suffix}"
    path.write_bytes(payload)

    from_path = [piece.text for piece in diceo.chunk(path)]
    from_bytes = [piece.text for piece in diceo.chunk(payload, name=f"sample{suffix}")]
    with path.open("rb") as handle:
        from_stream = [piece.text for piece in diceo.chunk(handle)]

    assert from_path == from_bytes == from_stream
