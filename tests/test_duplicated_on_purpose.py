"""The duplication that is deliberate, held to the terms it was argued on.

``diceo.pdf.extract`` imports **nothing** from the rest of the package, on purpose:
a cracker that an unrelated module's import error can break is the failure LangChain
hit when a PDF-side import made its CSV loader unusable. The price of that is a second
copy of the blank-character set, and the comment above
``diceo.pdf.extract._BLANK_CHARS`` accepts it in as many words -- "two copies of one
constant is the cheaper of the two problems".

That is a fair trade *while the copies agree*. If they drift, the cost is not a tidy
duplicate any more: `has_visible_text` is the blank-line test on both sides, so one
copy missing a character means one reader emits a block of nothing but zero-width
spaces -- a chunk that gets embedded, stored, searched and returned as a hit -- and
the other does not. The trade was argued on cheapness; this test is what keeps it
cheap, so the argument cannot quietly stop being true.

Sets, not strings: the two are written in different orders and grouped by different
comments, and neither of those matters to ``str.strip``.
"""

from __future__ import annotations

from diceo.pdf.extract import _BLANK_CHARS as PDF_BLANK
from diceo.pdf.extract import has_visible_text as pdf_has_visible_text
from diceo.plaintext import _BLANK_CHARS as TEXT_BLANK
from diceo.plaintext import has_visible_text as text_has_visible_text


def test_the_two_blank_character_sets_are_equal():
    text_only = sorted(set(TEXT_BLANK) - set(PDF_BLANK))
    pdf_only = sorted(set(PDF_BLANK) - set(TEXT_BLANK))

    assert not text_only and not pdf_only, (
        f"the deliberate duplicate has drifted: "
        f"plaintext-only={[hex(ord(c)) for c in text_only]}, "
        f"pdf-only={[hex(ord(c)) for c in pdf_only]}"
    )


def test_neither_set_is_empty():
    """A guard on the guard: two empty strings are also equal."""
    assert len(set(TEXT_BLANK)) > 20


def test_both_readers_agree_on_every_character_in_the_set():
    """The set equality is the mechanism; this is the behaviour it exists to protect.

    Asserted through the two functions rather than the two constants, because a
    caller's chunk is decided by `has_visible_text` and not by what it strips with.
    """
    for char in sorted(set(TEXT_BLANK) | set(PDF_BLANK)):
        assert not text_has_visible_text(char), f"plaintext sees {hex(ord(char))}"
        assert not pdf_has_visible_text(char), f"pdf sees {hex(ord(char))}"

    assert text_has_visible_text("x") and pdf_has_visible_text("x")


def test_the_pdf_cracker_still_imports_nothing_from_the_package():
    """The reason the duplication exists at all. If this ever fails, the second copy
    has lost its justification and should be deleted rather than kept in step."""
    from pathlib import Path

    import diceo.pdf.extract as extract

    source = Path(extract.__file__).read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if ("import diceo" in line or "from diceo" in line) and "diceo.pdf" not in line
    ]
    assert not offenders, offenders
