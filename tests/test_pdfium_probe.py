"""Invariants under the PDFium per-line probe in ``diceo.pdf.extract``.

This path had no unit tests when the hyphen-artefact guard was added --
`str.translate` was being called unconditionally on every page, which
experiment 027 (where the time goes) measured at 52.5 ms of 481 ms
on a corpus where **no page contained the artefact at all**. Guarding it is only
safe if the guard fires whenever the unconditional call would have changed
something, so that is what these pin.

    uv run pytest tests/test_pdfium_probe.py -q
"""

from __future__ import annotations

import pytest

from diceo.pdf import extract


def test_guard_chars_are_derived_from_the_translation_table():
    """The two cannot drift: adding a character to the table extends the guard.

    If these were written out separately, a new artefact character would be
    translated in the slow path and skipped in the fast one -- silent corruption
    that only shows up on documents nobody tested.
    """
    assert set(extract._HYPHEN_CHARS) == {chr(o) for o in extract._HYPHEN_ARTEFACT}
    assert extract._HYPHEN_CHARS, "an empty guard would skip every rewrite"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no artefact here",
        "soft￾hyphen at a line break",
        "￾",
        "\x02",
        "both ￾ and \x02 in one page",
        "￾￾￾ repeated",
        "unicode ok: naïve café — ﬁ ligature",
    ],
)
def test_guarded_rewrite_matches_the_unconditional_one(text: str):
    """Scanning first must be indistinguishable from always translating."""
    unconditional = text.translate(extract._HYPHEN_ARTEFACT)
    guarded = (
        text.translate(extract._HYPHEN_ARTEFACT)
        if any(ch in text for ch in extract._HYPHEN_CHARS)
        else text
    )
    assert guarded == unconditional


def test_numeric_first_is_a_superset_of_what_the_pattern_can_match():
    """The speed guard must never reject a token `_NUMERIC_FIELD` would accept.

    Brute-forced over the whole BMP rather than over a token list, because the
    failure mode is a *character class* nobody thought of -- a full-width digit, an
    Arabic-Indic numeral, a Unicode minus sign. A guard that is narrower than the
    pattern silently stops detecting data rows, which costs table structure with no
    error anywhere.
    """

    def guard(ch: str) -> bool:
        """Must mirror the condition in `_numeric_fields` exactly."""
        return ch in extract._NUMERIC_FIRST or (ch > "\x7f" and ch.isdigit())

    missed = [
        chr(code)
        for code in range(0x10000)
        if extract._NUMERIC_FIELD.match(chr(code)) and not guard(chr(code))
    ]
    assert missed == [], f"guard rejects {len(missed)} chars the pattern accepts: {missed[:10]}"


def test_the_guarded_numeric_count_matches_the_unguarded_one():
    """Same counts on prose, on data rows, and on the awkward cases between."""
    rows = [
        "Revenue 1,234 5,678 -910 (11) 12.5% n/a",
        "The quick brown fox jumps over the lazy dog",
        "",
        "   ",
        "-",
        "+",
        "(",
        "n",
        "N/A na N/a",
        "Table 3 shows 42 of 100 samples",
        "€1,000 £2,000 $3,000",
        "١٢٣ ４５６",
    ]
    for text in rows:
        unguarded = sum(1 for t in text.split() if extract._NUMERIC_FIELD.match(t))
        assert extract._numeric_fields(text) == unguarded, text


def test_the_rewrite_preserves_length_so_character_offsets_survive():
    """`Line.start`/`Line.end` index into this string, so a length change would
    silently misalign every span after the first artefact on the page."""
    text = "a￾b\x02c"
    assert len(text.translate(extract._HYPHEN_ARTEFACT)) == len(text)
