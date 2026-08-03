"""The furniture cap must measure the key, not the raw line.

experiment 041 (stress corpus) result 5 found ``suppress_furniture``
removing the *smaller* half of the furniture it exists for: on the 401-page Federal
Register it took the watermark and about half the running heads, and left the single
largest piece -- the production stamp, on every one of the 401 pages -- completely
untouched.

The stamp::

    VerDate Sep<11>2014 16:02 Jan 12, 2024 Jkt 262001 PO 00000 Frm 00001 Fmt 4703
    Sfmt 4703 E:\\FR\\FM\\16JAR1.SGM 16JAR1

is 113 raw characters, over ``FURNITURE_MAX_CHARS``. The key it would have been filed
under is 83, because normalising the digits is most of the line. The learner filed by
key and rejected by raw length, so a line whose length is mostly serial numbers could
never be learned however many pages it appeared on.

Measured after the fix, on the real document: **VerDate occurrences 401 -> 58**, output
3.20% smaller (was 1.270%), and the only word types lost are `jacn`, `jaws` and
`frmatter-cn` -- fragments of the stamp itself. On the 9/11 report, zero types lost.
"""

from __future__ import annotations

import pytest

from diceo.pdf.extract import FURNITURE_MAX_CHARS, Line, _Furniture, _furniture_key

STAMP = (
    "VerDate Sep<11>2014 16:02 Jan 12, 2024 Jkt 262001 PO 00000 Frm 00001 "
    "Fmt 4703 Sfmt 4703 E:\\FR\\FM\\16JAR1.SGM 16JAR1"
)


def test_the_stamp_is_longer_than_the_cap_but_its_key_is_not() -> None:
    """The arithmetic the fix rests on. If this ever stops holding the test below is
    passing for the wrong reason."""
    assert len(STAMP) > FURNITURE_MAX_CHARS
    assert len(_furniture_key(STAMP, "b")[1]) <= FURNITURE_MAX_CHARS


def _page(texts: list[str], number: int) -> list[Line]:
    return [
        Line(
            text=text,
            page=number,
            start=0,
            end=len(text),
            size=9.0,
            bold=False,
            bbox=(72.0, 700.0 - 12 * index, 540.0, 712.0 - 12 * index),
        )
        for index, text in enumerate(texts)
    ]


def _stamp_for(page: int) -> str:
    return STAMP.replace("00001", f"{page:05d}").replace("16:02", f"{page % 24:02d}:02")


#: Headings that stay distinct *after* digit normalisation. Numbering them would give
#: every page the key ``heading #``, which is genuinely furniture-shaped -- the learner
#: would be right to strip it, and the test would be measuring the wrong thing.
TITLES = ["Scope", "Definitions", "Applicability", "Compliance", "Recordkeeping", "Appeals"]


def test_a_stamp_repeating_across_pages_is_learned_and_stripped() -> None:
    learner = _Furniture()
    pages = [
        _page([TITLES[n], "body text that differs on every page", _stamp_for(n)], n)
        for n in range(6)
    ]
    learner.observe_all(pages)
    kept, dropped = learner.strip(pages[3])
    assert dropped == 1
    assert all("VerDate" not in line.text for line in kept)
    assert any(TITLES[3] in line.text for line in kept), "the real heading must survive"


def test_a_long_repeated_sentence_is_still_not_furniture() -> None:
    """The guard the cap exists for.

    Normalising digits must not turn a genuine repeated *sentence* into a label. This
    one carries no digits, so its key is as long as it is and stays over the cap.
    """
    sentence = (
        "The Administrator has determined that this action is not a significant "
        "regulatory action and was therefore not submitted for review under the order."
    )
    assert len(_furniture_key(sentence, "b")[1]) > FURNITURE_MAX_CHARS
    learner = _Furniture()
    pages = [_page([TITLES[n], "unique body", sentence], n) for n in range(6)]
    learner.observe_all(pages)
    _, dropped = learner.strip(pages[2])
    assert dropped == 0


def test_suppression_still_never_empties_a_page() -> None:
    """Rule 3's backstop, and a wider net makes it matter more.

    A document whose every page is the same two lines is degenerate but real -- a form,
    a slide export -- and returning nothing for it is the worst available answer.
    """
    learner = _Furniture()
    pages = [_page(["Repeated head 1", "Repeated foot 1"], n) for n in range(6)]
    learner.observe_all(pages)
    kept, dropped = learner.strip(pages[3])
    assert kept == pages[3]
    assert dropped == 0


@pytest.mark.parametrize("side", ["t", "b"])
def test_the_side_still_separates_a_footer_from_a_title(side: str) -> None:
    """Digit normalisation collapses every number-only line to the same string, so the
    edge is what stops a page-number footer teaching a key that deletes a `2025` section
    title. Widening the length test must not weaken that."""
    assert _furniture_key("2025", "t") != _furniture_key("2025", "b")
    assert _furniture_key("2025", side)[0] == side
