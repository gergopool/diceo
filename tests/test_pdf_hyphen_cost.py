"""One page must not cost the caller candidates x characters.

`_resolve_hyphens` asks the page to corroborate every hyphen PDFium marked, and
`_spelled_solid_on_this_page` answers with `str.find`, which scans to the end of
the page whenever the answer is no. That is the right price at 3.4 candidates per
page -- the measured rate over the 4,061 pages of public-bench -- and a denial of
service at any rate a document can choose. Measured on synthetic pages of 20
characters per marker before the fix:

    =========  ============  =========  ========
      markers    page_chars     before     after
    =========  ============  =========  ========
        2,000        40,000    0.026 s   0.004 s
        4,000        80,000    0.101 s   0.008 s
        8,000       160,000    0.397 s   0.017 s
       16,000       320,000    1.575 s   0.034 s
    =========  ============  =========  ========

Four times the work per doubling, so a 3.2 MB text layer -- a few hundred KB of
compressed PDF, 160,000 line-ending hyphens -- extrapolated to ~157 s for **one
page** and 32 MB to hours. `Limits(max_seconds=...)` is checked between blocks,
so it could interrupt none of it: the document neither finished nor failed, and
nothing landed in `diagnostics` to say so. Those two now measure 0.39 s and
4.32 s.

The fix is `_words_on_this_page`, built once per page after `_SCANS_BEFORE_INDEX`
candidates. It has to be exact, not approximate: this file's other half is the
proof that the index and the scan decide **identically**, because a fix that
quietly rejoins one word differently has broken retrieval to save time nobody was
spending.
"""

from __future__ import annotations

import random
import sys
import time

from diceo.pdf import extract
from diceo.pdf.extract import _resolve_hyphens, _words_on_this_page

#: PDFium's line-break marker, in the spelling the raw page text carries.
MARK = "￾"
#: Enough markers to be past `_SCANS_BEFORE_INDEX` several times over, small
#: enough that the timing test below runs in well under a second.
SMALL, LARGE = 2_000, 8_000


def _page(markers: int, *, seed: int = 7, joinable: bool = False) -> str:
    """A page with `markers` broken words and ~20 characters per marker.

    Every candidate misses unless `joinable`, which is the expensive direction:
    a miss scans the page to the end, a hit can stop early.
    """
    rng = random.Random(seed)
    out: list[str] = []
    for _ in range(markers):
        left = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=5))
        right = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=5))
        out.append(left + MARK + right)
        out.append(left + right if joinable else "".join(rng.choices("qwertyu", k=7)))
    return " ".join(out)


def _fastest(page: str, rounds: int = 3) -> float:
    best = float("inf")
    for _ in range(rounds):
        start = time.perf_counter()
        _resolve_hyphens(page)
        best = min(best, time.perf_counter() - start)
    return best


# --------------------------------------------------------------------------- #
# the cost
# --------------------------------------------------------------------------- #


def test_four_times_the_markers_does_not_cost_sixteen_times_the_time() -> None:
    """The complexity, not the clock. A wall-clock ceiling would have to be loose
    enough to survive a slow CI box and would then also survive a mild regression;
    the ratio separates the two shapes by 3.7x on any box.

    Measured over eight runs on the dev box: 3.96-4.14 with the index, 14.87-15.39
    without it. Both page and marker count quadruple, so linear in the page is 4
    and the product is 16, and the bound sits halfway between.
    """
    small, large = _fastest(_page(SMALL)), _fastest(_page(LARGE))

    ratio = large / small
    assert ratio < 8.0, (
        f"4x the markers cost {ratio:.1f}x the time ({small:.3f}s -> {large:.3f}s) -- "
        "the page-wide scan is back in the per-candidate loop"
    )


def test_the_expensive_direction_is_bounded_too() -> None:
    """A page that *corroborates* every candidate was quadratic as well -- `find`
    restarts at 0 for each one, so a hit still walks half the page on average
    (0.016 s at 2,000 markers, 0.941 s at 16,000). The index answers both
    directions in one pass, so this must hold for hits as much as for misses."""
    small = _fastest(_page(SMALL, joinable=True))
    large = _fastest(_page(LARGE, joinable=True))

    ratio = large / small
    assert ratio < 8.0, f"4x the markers cost {ratio:.1f}x the time on corroborated joins"


def _count_index_builds(monkeypatch) -> list[int]:
    built: list[int] = []
    real = extract._words_on_this_page

    def counted(lowered: str) -> set[str]:
        built.append(len(lowered))
        return real(lowered)

    monkeypatch.setattr(extract, "_words_on_this_page", counted)
    return built


def test_an_ordinary_page_never_builds_the_index(monkeypatch) -> None:
    """The common path has to stay exactly what it was, and it is a *hyphen-heavy*
    page that has to prove it: the worst of 4,061 public-bench pages carries 44
    candidates against a threshold of 128, and the page here carries 127. Measured
    interleaved over the 2,594 hyphenated pages of that corpus, old logic against
    new in one wall-clock window, the difference is 1.000x."""
    built = _count_index_builds(monkeypatch)

    text, joined, kept = _resolve_hyphens(_page(extract._SCANS_BEFORE_INDEX - 1, joinable=True))

    assert built == [], f"an ordinary page built the index {len(built)} times"
    assert (joined, kept) == (extract._SCANS_BEFORE_INDEX - 1, 0)
    assert MARK in text


def test_a_page_past_the_threshold_builds_it_exactly_once(monkeypatch) -> None:
    """Once, not once per candidate -- the index is the whole page's vocabulary and
    rebuilding it per candidate would be the same product with a worse constant."""
    built = _count_index_builds(monkeypatch)

    _resolve_hyphens(_page(extract._SCANS_BEFORE_INDEX + 40, joinable=True))

    assert len(built) == 1, f"built the index {len(built)} times for one page"


# --------------------------------------------------------------------------- #
# ...bought at no cost to the decision, which is the part that matters
# --------------------------------------------------------------------------- #

#: Scripts that break the easy assumptions: a cased alphabet, an uncased one, a
#: right-to-left one, and `İ` -- the one codepoint in Unicode whose `str.lower` is
#: not one character, which is how a candidate can arrive non-alphabetic.
_ALPHABETS = (
    "abcdefghijklmnopqrstuvwxyz",
    "ABCdefGHI",
    "áéíöűñçßøæ",
    "αβγδεζηθ",
    "абвгдеёжз",
    "אבגדהו",
    "ابتثجح",
    "漢字日本語",
    "İIıi",
)
#: What sits between words on a real page, plus the characters that make the
#: pattern behind the index over-match: superscripts, fractions, digits.
_GLUE = (
    " ", " ", " ", "\r\n", "\t", ".", ",", "-", "(", ")", "/", "'", "’",
    "­", "²", "³", "½", "Ⅻ", "৴", "0", "7", "_", "​", "́",
)  # fmt: skip


def _random_page(rng: random.Random) -> str:
    """A page built to land on the seams: one-letter fragments, markers touching
    markers, the joined form present about half the time, glue inside words."""
    alphabet = "".join(rng.sample(_ALPHABETS, rng.randint(1, 3)))
    vocabulary = [
        "".join(rng.choices(alphabet, k=rng.randint(1, 7))) for _ in range(rng.randint(2, 40))
    ]
    out: list[str] = []
    for _ in range(rng.randint(1, 60)):
        roll = rng.random()
        if roll < 0.45:
            out.append(rng.choice(vocabulary))
        elif roll < 0.75:
            left, right = rng.choice(vocabulary), rng.choice(vocabulary)
            out.append(left + MARK + right)
            if rng.random() < 0.5:
                vocabulary.append(left + right)
        elif roll < 0.85:
            out.append(MARK)
        else:
            out.append(rng.choice(vocabulary) + rng.choice(_GLUE) + rng.choice(vocabulary))
        out.append(rng.choice(_GLUE))
    return "".join(out)


def _both_paths(page: str, monkeypatch) -> tuple[tuple, tuple]:
    """The same page decided by the scan alone and by the index from the first
    candidate on. The scan side is the pre-2026-08-03 function unchanged."""
    monkeypatch.setattr(extract, "_SCANS_BEFORE_INDEX", sys.maxsize)
    scanned = _resolve_hyphens(page)
    monkeypatch.setattr(extract, "_SCANS_BEFORE_INDEX", 1)
    indexed = _resolve_hyphens(page)
    return scanned, indexed


def test_the_index_decides_exactly_what_the_scan_decides(monkeypatch) -> None:
    """The whole licence for the optimisation, as a differential.

    Not "close enough": byte-identical text and identical counts, or a document
    reindexed after an upgrade changes under the caller. The same comparison was
    run against the shipped implementation copied out of git over 20,000 pages and
    eight thresholds -- 160,000 comparisons, 483,708 markers, no difference.
    """
    for seed in range(1_500):
        page = _random_page(random.Random(seed))

        scanned, indexed = _both_paths(page, monkeypatch)

        assert scanned == indexed, (
            f"seed {seed}: {page!r}\n scan  {scanned!r}\n index {indexed!r}"
        )


def test_a_candidate_lowercasing_left_non_alphabetic_goes_back_to_the_scan(monkeypatch) -> None:
    """`İ` lowercases to `i` plus a combining dot, so the slices `_resolve_hyphens`
    takes from the lowered page stop lining up with the runs it measured on the
    original. The misalignment predates the index and is not what was fixed here --
    but the index would have answered it *differently*, and "same defect" is the
    requirement. Brute-forced over {İ, a, b, marker, space}: without the guard, 312
    nine-character pages decide differently. This is one of them.
    """
    page = f"İa{MARK}İİ{MARK}İ"

    scanned, indexed = _both_paths(page, monkeypatch)

    assert scanned == (f"İa{MARK}İİ-İ", 1, 1), scanned
    assert indexed == scanned, indexed


def test_a_superscript_does_not_swallow_the_word_before_it() -> None:
    """The index is built with a regex, and Python has no character class that
    *is* `str.isalpha`: the closest, `[^\\W\\d_]`, also takes the 1,151 codepoints
    that are numeric but not decimal. A footnote marker is exactly that case and it
    is everywhere in the documents this reader is for, so a run the pattern glued
    together is split back apart -- otherwise ``appropriations²`` would stop
    corroborating ``appropriations`` and the word would stay broken.
    """
    words = _words_on_this_page("the appropriations² fell ½way to Ⅻ and a_b ended".lower())

    assert "appropriations" in words, words
    assert "way" in words, words  # the fraction did not swallow the word after it
    assert {"a", "b"} <= words, words  # nor did the underscore, which is not a letter


def test_every_alphabetic_codepoint_is_inside_the_pattern_the_index_is_built_with() -> None:
    """The soundness condition, brute-forced rather than argued.

    The index may over-match (the test above cleans that up); it may never
    *under*-match, because a run the pattern cut short is a word the index does not
    hold and a rejoin the scan would have made. `isalpha` implies `isalnum` implies
    `\\w`, and no alphabetic character is decimal, so `[^\\W\\d_]` covers all
    136,726 of them -- today. Unicode grows every year and this asks again.
    """
    alphabetic = "".join(chr(c) for c in range(sys.maxunicode + 1) if chr(c).isalpha())

    assert extract._WORD_RUN.fullmatch(alphabetic), "an alphabetic character fell outside it"


def test_the_index_still_refuses_a_compound_the_page_does_not_corroborate(monkeypatch) -> None:
    """The decision this reader exists to get right, taken through the fast path.

    `broker-dealer` survives only because nothing on the page spells
    `brokerdealer`; PyMuPDF4LLM destroys it 53 times on one Federal Register issue.
    The index must be no more willing to invent a word than the scan was, on a page
    big enough to have triggered it.
    """
    monkeypatch.setattr(extract, "_SCANS_BEFORE_INDEX", 1)
    page = f"{_page(200)} the broker{MARK}dealer rules and the appropri{MARK}ations bill"

    text, joined, kept = _resolve_hyphens(page + " appropriations rose")

    assert "broker-dealer rules" in text, "the index invented a compound"
    assert f"appropri{MARK}ations bill" in text, "the index missed a corroborated join"
    # 200 uncorroborated markers from `_page`, plus `broker-dealer`, against one join.
    assert (joined, kept) == (1, 201), (joined, kept)
