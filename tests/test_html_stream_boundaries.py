"""What an HTML page turned into after its first 64 KiB.

`iter_html_blocks` feeds the parser in fixed-size pieces, so that a 200 MB single-page
export costs the memory of a small one (rule 2). The first piece went through
`decode()` -- the BOM, the ``<meta charset>``, the WHATWG label table, the cp1252
fallback, all of it -- and every piece after it went through one line::

    text = raw.decode("utf-8", "replace")

Three separate defects sat in that line, and they had to be fixed together because
each fix alone still corrupts the document:

* **The encoding was worked out and then thrown away.** A windows-1252, shift_jis or
  iso-8859-2 page was read correctly up to byte 65,536 and as UTF-8 after it. The
  bigger the page, the smaller the share of it that arrived -- and encoding/garbled
  text is the largest single issue class in this area (657 issues across eight rival
  trackers), so this is the common document, not the exotic one.

* **Nothing counted the damage.** ``errors="replace"`` is silent by construction:
  ``lost_data`` stayed ``False`` while most of a page became U+FFFD. Four sibling
  decode sites were routed through `_report_loss` on 2026-07-31 and this one was
  missed -- the one the README's "never lose data silently" claim leans on hardest.

* **Each piece was decoded on its own.** That splits any multi-byte character
  straddling a boundary in a *valid UTF-8* file, and the strict-decode ladder in
  `decode()` then read the whole first piece as cp1252 because its last character was
  cut in half. An incremental decoder holds the fragment until the next piece
  completes it, which is why the fix removes this by construction rather than by
  arithmetic.

The controls matter as much as the cases: an ordinary page must gain no note, and a
page that genuinely contains U+FFFD must not be called a loss -- ``lost_data`` is the
boolean the API tells callers to branch on, and one that fires on healthy documents is
one they learn to ignore.

    uv run pytest tests/test_html_stream_boundaries.py -q
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

import diceo
from diceo.plaintext import iter_html_blocks
from diceo.types import Diagnostics

#: `iter_html_blocks`' real default piece size, and the offset every off-by-one here
#: is about. Tests that only ever pass a small `chunk_bytes` cannot see a bug that
#: lives at 65,536.
CHUNK = 1 << 16

_HEAD = b"<html><body><p>"

#: Characters no ASCII-only sample can predict: they are one byte in cp1252, two or
#: three in UTF-8, and nothing at all in the wrong codec.
ACCENTS = "café — Señor François, Ångström"


def _read(raw: bytes, **kwargs: int) -> tuple[str, Diagnostics]:
    report = Diagnostics()
    blocks = list(iter_html_blocks(io.BytesIO(raw), report, **kwargs))
    return "\n".join(block.text for block in blocks), report


def _labelled(charset: str, ascii_filler: int, tail: str, encoding: str) -> bytes:
    """A page that declares `charset`, is ASCII for `ascii_filler` bytes, then is not.

    The shape the defect needs: everything the first piece can see is ASCII, so the
    only evidence of what the *rest* of the document is, is the declaration.
    """
    head = f'<html><head><meta charset="{charset}"></head><body><p>'.encode()
    return (
        head
        + b"Revenue rose. " * (ascii_filler // 14)
        + b"</p><p>"
        + tail.encode(encoding)
        + b"</p></body></html>"
    )


# --------------------------------------------------------------------------- #
# 1. the encoding decided on the first piece has to hold for the rest
# --------------------------------------------------------------------------- #


def test_a_windows_1252_page_keeps_its_accents_after_the_first_piece() -> None:
    """The defect itself, at a piece size small enough to be a cheap fixture."""
    page = _labelled("windows-1252", 400, ACCENTS, "cp1252")

    text, report = _read(page, chunk_bytes=128)

    assert ACCENTS in text
    assert not report.lost_data, report.as_dict()


def test_the_same_page_at_the_real_piece_size(tmp_path: Path) -> None:
    """And again at 65,536 through the public API, because an off-by-one at the real
    boundary is exactly what a small `chunk_bytes` cannot catch."""
    page = _labelled("windows-1252", 90_000, ACCENTS, "cp1252")
    assert len(page) > CHUNK, "the fixture has to cross a real boundary"
    path = tmp_path / "long.html"
    path.write_bytes(page)
    report = Diagnostics()

    text = "\n".join(chunk.text for chunk in diceo.chunk(path, diagnostics=report))

    assert ACCENTS in text
    assert not report.lost_data, report.as_dict()


def test_a_multibyte_codec_survives_the_boundary_too() -> None:
    """Not only the single-byte codecs. shift_jis is two bytes per character, so
    decoding its second half as UTF-8 produces mojibake *and* replacements."""
    japanese = "四半期の売上高は12億円に達した"
    page = _labelled("shift_jis", 400, japanese, "shift_jis")

    text, report = _read(page, chunk_bytes=128)

    assert japanese in text
    assert not report.lost_data, report.as_dict()


def test_a_declared_utf_16_is_still_ignored() -> None:
    """The guard on the rule above. The declaration was found by an ASCII regex, so
    the document is not UTF-16 whatever it says -- obeying it would read an ordinary
    page as CJK, and a two-byte codec cannot report being wrong."""
    page = _labelled("utf-16", 400, "café", "utf-8")

    text, report = _read(page, chunk_bytes=128)

    assert "Revenue rose." in text
    assert "café" in text
    assert not report.lost_data, report.as_dict()


# --------------------------------------------------------------------------- #
# 2. a character cut in half by the piece size
# --------------------------------------------------------------------------- #


def _straddling(bytes_in_first_piece: int) -> bytes:
    """Valid UTF-8 whose em dash puts `bytes_in_first_piece` of its 3 bytes before
    byte 65,536 and the rest after."""
    filler = CHUNK - len(_HEAD) - bytes_in_first_piece
    page = _HEAD + b"A" * filler + "—record".encode() + b"</p></body></html>"
    at = CHUNK - bytes_in_first_piece
    assert page[at : at + 3] == b"\xe2\x80\x94", "the fixture does not straddle the boundary"
    return page


@pytest.mark.parametrize("bytes_in_first_piece", [3, 2, 1, 0])
def test_a_character_split_across_the_boundary_survives(bytes_in_first_piece: int) -> None:
    """0 and 3 are the controls (the character is wholly on one side and always
    worked); 1 and 2 are the defect. At 1, the first piece failed its strict UTF-8
    decode over a character that is not damaged at all -- only unfinished -- and fell
    all the way to cp1252, so 65 KB of ASCII arrived fine and the em dash arrived as
    ``â`` plus two U+FFFD."""
    text, report = _read(_straddling(bytes_in_first_piece))

    assert "—record" in text
    assert not report.lost_data, report.as_dict()


# --------------------------------------------------------------------------- #
# 3. and when bytes really are lost, they are counted
# --------------------------------------------------------------------------- #

#: 0x81 is undefined in cp1252 *and* invalid UTF-8, so it survives no decoder we try.
#: `\xff` will not do: cp1252 decodes it happily.
UNDECODABLE = b"\x81" * 1_000


def test_undecodable_bytes_after_the_first_piece_are_counted() -> None:
    """The silent half. These 1,000 characters are gone from the index either way;
    before, ``lost_data`` said the document was fine."""
    page = _HEAD + b"A" * CHUNK + b"</p><p>" + UNDECODABLE + b"</p></body></html>"

    _, report = _read(page)

    assert report.lost_data, report.as_dict()
    counted = [entry for entry in report.truncated if "undecodable_bytes=1000" in entry]
    assert counted, report.truncated


def test_the_count_is_reported_once_and_in_the_shape_the_other_sites_use() -> None:
    """Through `_report_loss`, like the four sibling decode sites -- a caller greps one
    spelling and adds the numbers up, so two spellings double-count their own loss."""
    page = _HEAD + b"A" * CHUNK + b"</p><p>" + UNDECODABLE + b"</p></body></html>"

    _, report = _read(page)

    mentions = [
        entry for entry in [*report.truncated, *report.notes] if "undecodable_bytes" in entry
    ]
    assert len(mentions) == 1, mentions
    assert "character your index will not have" in mentions[0]


def test_control_characters_past_the_boundary_are_removed_and_counted() -> None:
    """Only the first piece was ever stripped, so a NUL after byte 65,536 travelled
    into the caller's index -- where PostgreSQL rejects it in `text` and `jsonb`, and
    our success becomes an exception in their process."""
    page = _HEAD + b"A" * CHUNK + b"</p><p>before\x00after</p></body></html>"

    text, report = _read(page)

    assert "\x00" not in text
    assert "beforeafter" in text
    assert any("control_chars_removed=1" in note for note in report.notes), report.notes


# --------------------------------------------------------------------------- #
# 4. controls: the ordinary page over the boundary must be untouched
# --------------------------------------------------------------------------- #


def test_an_ordinary_utf8_page_over_the_boundary_gains_no_note() -> None:
    """The 99% case. Every diagnostic above must be silent here or it is noise."""
    page = _HEAD + "Revenue rose to 1200 million — a record. ".encode() * 3_000 + b"</p></body>"
    assert len(page) > CHUNK

    text, report = _read(page)

    assert not report.lost_data, report.as_dict()
    assert [note for note in report.notes if not note.startswith("title=")] == [], report.notes
    assert text.count("—") == 3_000, "every character crossed every boundary intact"


def test_a_page_that_genuinely_contains_u_fffd_past_the_boundary_is_not_a_loss() -> None:
    """The other half of the counting rule, at the far end of a stream. A U+FFFD the
    document really carries is a character it has, not a byte we lost -- counting the
    replacement characters in the output rather than the ones we put there would cry
    wolf on the boolean callers branch on."""
    page = _HEAD + b"A" * CHUNK + "</p><p>a � in the text</p></body></html>".encode()

    text, report = _read(page)

    assert "�" in text
    assert not report.lost_data, report.as_dict()
