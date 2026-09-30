"""Structured PDF extraction at close to plain-text speed.

The bet this file exists to test (decisions D3 and D4): a PDF's heading
structure can be *synthesized* from font geometry that PDFium already computed,
instead of recovered by layout analysis. If that holds, structured output costs
a small constant over flat text rather than the ~40x the AGPL markdown
converters charge.

**How it actually works, and why not the obvious way.**
D4 proposed iterating page *objects* (hundreds per page) rather than characters
(tens of thousands). Measurement said neither: the cheapest correct unit is the
**line**, and PDFium will hand us lines for free.

``FPDFText_GetText(textpage, 0, n_chars, buf)`` returns the whole page's text in
one C-side call, and -- verified in experiment 004 -- the returned string is
index-for-index aligned with PDFium's character array. So a Python-level
``str.find`` walk over ``\\r\\n`` yields line spans whose start/end are valid
character indices, and every per-line property (font size, font weight, char
box) is one ctypes call at a known index. That is ~6 calls per *line* instead of
~6 per object or ~2 per character:

    =====================  =========  =====================  ===============
    unit                     count    ctypes calls / page    pages/s (300 p)
    =====================  =========  =====================  ===============
    characters                 3,538                 ~7,076             83.0
    text objects                 194                   ~776            121.8
    lines (this module)           54                   ~324            136.4
    =====================  =========  =====================  ===============

Counts are page 6 of paper-tables.pdf; throughput is paper-large.pdf, best of 7.
Line density is also the only one of the three that is a property of the *page*
rather than of the producer -- across 11 documents from 10 producers, objects per
1,000 characters spanned 20-993 while lines per 1,000 characters spanned 13-46.
That is why this path has no producer-triggered performance cliff.

Font weight is the second measured surprise. ``FPDFText_GetFontWeight`` reports
700 for the bold face and ~425 for the regular one, per character index, for one
call. That matters because the *subsection* headings of an ordinary paper are set
at body size and differ from body text only by weight: 16 of the fixture's 27
headings are body-size bold, so a size-only clusterer cannot see any of them.

**Streaming (D1).**
``blocks()`` is a generator and holds at most ``calibration_pages`` pages of
lines at once (default 4). Font-size clustering needs a document-level view
before it can label anything, so the first pages are buffered, the body size and
the heading style ranking are fitted on them, the buffer is drained, and every
later page streams straight through. ``islice(blocks(huge), 1000)`` returns in
0.43 s against 12.8 s for the whole 1,500-page document, so early exit really
exits.

Laziness alone was *not* enough to bound memory -- see ``REOPEN_EVERY`` below,
which is the part of this file that actually delivers the 300 MB budget.

``calibration="full"`` instead makes a complete first pass over font metrics
only (no text assembly) before emitting anything. It costs 1.97x and, on the
fixture, found exactly the same styles. Kept because a document whose front
matter is unrepresentative (a cover page, a title-only first page) is a real
shape, and because "does the cheap approximation cost recall?" must stay
answerable.
"""

from __future__ import annotations

import ctypes
import math
import re
import threading
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

__all__ = [
    "PDFIUM_LOCK",
    "Block",
    "Census",
    "Line",
    "StyleModel",
    "blocks",
    "font_census",
    "lines",
    "page_lines",
]

# --------------------------------------------------------------------------- #
# tunables -- every one of these is a guess that a fixture could refute
# --------------------------------------------------------------------------- #

#: A line whose font is this much larger than body text is a heading candidate.
LARGER_THAN_BODY = 1.06
#: Bold at (nearly) body size still counts -- this is how subsection headings
#: are set in most papers, and the reason weight is read at all.
BOLD_MIN_RATIO = 0.95
#: Headings are short. A long line in a big font is a pull quote or a legal
#: notice, and promoting it costs precision. 140 chars ~ two printed lines.
HEADING_MAX_CHARS = 140
#: ...and a heading *style* is short on average. This is the document-level
#: version of the same test and it is the one that earns its keep: the fixture's
#: front-page legal notice is set 20% larger than body text, so size alone
#: promotes it, but its lines average 77 characters against ~21 for the real
#: section headings. Rejecting the style removes three false positives *and*
#: frees the heading level it was occupying, which is why the subsections land
#: at h3 rather than h4.
HEADING_STYLE_MAX_MEAN = 55
#: Lines rotated more than this (radians) are page furniture -- arXiv stamps,
#: watermarks -- not headings.
MAX_HEADING_ANGLE = 0.15
#: The nominal-size census is *degenerate* when its modal value is at or below
#: this: the producer wrote ``/F1 1 Tf`` and put the type scale in the text
#: matrix, so ``FPDFText_GetFontSize`` reports the same number for every
#: character in the document and no heading can ever be distinguished from body
#: text. Measured 2026-07-31: **both** real PDFs this repository holds do this
#: (911-report.pdf reports 1.0 on all 585 pages, the IPBES assessment likewise),
#: against 0 of the 14 synthetic corpus PDFs -- our own renderer emits honest
#: ``Tf`` sizes, which is exactly why no measurement in ``docs/`` caught it.
DEGENERATE_SIZE = 2.0
#: Two *measured* sizes within this fraction of each other are one level. A
#: nominal census holds a handful of authored values and needs no clustering; a
#: glyph-box census is a measurement, and two font families set at the same point
#: size measure a little differently, so without this each becomes its own level.
LEVEL_SIZE_TOL = 0.04
#: Cap on levels fitted from a measured signal. Styles past it are *dropped*
#: rather than clamped to level 6: a hair above body size is glyph-box noise, and
#: a false heading puts a chunk boundary in the middle of a paragraph.
MAX_LEVELS = 4
#: While the size signal is measured, keep censusing and refit this often.
#:
#: A calibration window is only as good as its representativeness, and on
#: 911-report.pdf it is not: **pages 0-31 are all front matter**, so the body size
#: fitted from them is 6.4 (the cover's small print) where the document's real
#: body is 9.3 and carries 105,117 characters. Widening the window fixes nothing
#: -- measured, 4/8/16/32 pages all give the same wrong answer.
#:
#: Refitting is confined to the measured path, which fires only on documents that
#: currently yield *no* structure at all. So it cannot regress anything the corpus
#: measures, and on the class of document it does touch, any model beats none.
#: (The same defect on the nominal path is a real and separate finding -- see
#: experiment 035 -- but fixing it there moves published numbers and needs the
#: harness first.)
EM_REFIT_PAGES = 8
#: A style seen on fewer lines than this is a one-off -- cover-page display type,
#: a half title, an imprint -- not a level in the document's hierarchy. Measured
#: on 911-report.pdf: the working section-heading style (em 10.9, 699 characters
#: over 20-odd lines) and the chapter style (14.5) were both pushed past
#: ``MAX_LEVELS`` by three styles carrying 24, 24 and 27 characters on **one line
#: each**.
MIN_STYLE_LINES = 3
#: ...and pruning is only sound once the census can tell "rare" from "not seen
#: yet". The structure-synthesis experiment measured heading F1 against PyMuPDF4LLM
#: collapsing 0.568 -> 0.246 when rare-style pruning was applied to a 4-page
#: calibration window, against 0.529 -> 0.535 on a census over the whole document.
#: Continuous refitting (``EM_REFIT_PAGES``) grows the census toward the second case, so the
#: gate is on size rather than on a flag a caller could get wrong.
PRUNE_AFTER_CHARS = 20_000
#: A vertical gap larger than this many times the line height ends a paragraph.
PARA_BREAK_GAP = 1.7
#: Table-row candidate: horizontal advance per character this much above the
#: page's median means wide inter-column gaps. Weak; opt-in only.
TABLE_DENSITY = 1.45
# The geometry-free row signal -- see _is_data_row. Thresholds are the measured
# separation on the corpus: data rows run 43-80% numeric fields, prose far lower.
_MIN_ROW_FIELDS = 4
_ROW_NUMERIC_SHARE = 0.3
_NUMERIC_FIELD = re.compile(r"^[-+(]?(?:\d[\d,]*(?:\.\d+)?%?|n/?a)[)]?$", re.IGNORECASE)
#: Every character `_NUMERIC_FIELD` can match in first position, as a set.
#:
#: This is the hottest pattern in the module by a wide margin -- **6.27 matches per
#: line, 74,447 on the 22-file held-out corpus**, because `_numeric_fields` runs it
#: over every whitespace-separated token of every line. A token that cannot begin a
#: numeric field is an ordinary word, and 83.9% of corpus tokens are, so a set
#: membership test on the first character keeps the regex engine out of the common
#: case entirely: 16.1 ms to 8.1 ms over 105,632 real tokens.
#:
#: Correctness rests on this set being a superset of the pattern's possible first
#: characters. The first version was ASCII-only and **wrong**: Python's `\d` matches
#: the whole Unicode `Nd` category, so `١٢٣` and `４５６` match the pattern while
#: failing an ASCII guard -- the optimisation would have silently stopped detecting
#: data rows in Arabic, Persian, Urdu, Devanagari and full-width CJK documents, with
#: no error anywhere. Hence the `isdigit()` arm below, and hence the brute-force test
#: over the entire BMP in `tests/test_pdfium_probe.py` rather than over a token list.
_NUMERIC_FIRST = frozenset("0123456789-+(nN")

_CAPTION = re.compile(
    r"^\s*(?:table|figure|fig\.|listing|algorithm|chart|exhibit)\s*\d+(?:[.-]\d+)*"
    r"(?:\s*[:.)]|\s*$)",
    re.I,
)
_LIST_ITEM = re.compile(
    r"^\s*(?:[•‣◦⁃∙·▪●−–-]\s+"
    r"|\(?\d{1,3}[.)]\s+|[a-z][.)]\s+|[ivxlIVXL]{1,5}[.)]\s+)"
)
#: PDFium's marker for **a hyphen that ends a visual line**: U+FFFE in the raw text
#: `FPDFText_GetText` returns, U+0002 in bounded text (`FPDFText_GetUnicode` reports
#: 0x0002 for both spellings). Rewriting it to ``-`` is the *fallback*, not the
#: meaning -- see `_resolve_hyphens`. Same length, so character offsets survive.
_HYPHEN_ARTEFACT = str.maketrans({"￾": "-", "\x02": "-"})
#: C0 controls and DEL, minus the three that are deliberate layout (tab, LF, CR).
#:
#: `chunk.text` is what a caller writes to a database and hands an embedder. A NUL is
#: rejected outright by PostgreSQL's `text` type, has to be escaped in JSON, and
#: tokenises to noise. Experiment 028 closed this for the plain-text reader and the PDF
#: path was never covered: measured, a text layer holding \x00, \x01 and \x1b handed all
#: three to the caller. Searched before substituting, so a clean page pays one C-speed
#: scan and nothing else.
_TAU = 2 * math.pi
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
#: The characters `_HYPHEN_ARTEFACT` rewrites, derived from the table so the two
#: cannot drift apart.
#:
#: `str.translate` walks every character of every page and builds a new string
#: whether or not it changes anything. Scanning first and rewriting only on a hit
#: is strictly cheaper, and how much it saves depends entirely on the document:
#:
#:     ============  ======  =========  =========
#:     corpus        pages   U+FFFE     U+0002
#:     ============  ======  =========  =========
#:     holdout-23       506          0          0
#:     holdout-41       520          0          0
#:     fixtures       1,815      1,210          0
#:     ============  ======  =========  =========
#:
#: So on the held-out corpora the translate was 52.5 ms of the 481 ms (11%) spent
#: doing **nothing**, while on the real-document fixtures two thirds of pages do
#: carry the artefact and still pay for the rewrite. The guard is never worse than
#: the unconditional call and is a large win on documents that do not hyphenate --
#: it is not a claim that the artefact is rare, which
#: experiment 004 (font metadata cost) showed it is not.
#: U+0002 is the *bounded*-text spelling and cannot occur in the
#: raw text this function reads; it stays in the tuple because the table is the
#: single source of truth and the extra scan is two C-speed passes.
_HYPHEN_CHARS = tuple(map(chr, _HYPHEN_ARTEFACT))
#: Either spelling of the marker, derived from the table so the two cannot drift.
#: One `search` replaces the two `in` scans the guard used to make, and the same
#: pattern then walks the hits with `finditer`.
_HYPHEN_MARKER = re.compile(f"[{re.escape(''.join(_HYPHEN_CHARS))}]")
#: What `page_lines` leaves in `text` where it has decided to *delete* the hyphen.
#:
#: The deletion cannot happen in the page string: ``Line.start``/``end`` are indices
#: into PDFium's character array and every font, weight, angle and box probe in
#: `page_lines` is made at one of them, so a rewrite that changed the length would
#: silently misalign every metric on the page. So the decision is recorded in place,
#: one character wide like the ``-`` it stands in for, and applied when the line's
#: own text is assembled. U+FFFE is a Unicode *noncharacter* -- a decoder emits
#: U+FFFD for a bad sequence, never this -- so it cannot collide with real text, and
#: it is what PDFium already handed us, so marking a join costs no rewrite at all.
_JOIN_MARK = "￾"
#: Shortest fragment either side of a line-break hyphen that may be rejoined.
#: Not a quality gate -- `_resolve_hyphens` has a far stronger one -- just a guard
#: against searching the page for a two-letter needle.
_MIN_FRAGMENT = 2
#: A superset of "run of characters `str.isalpha` accepts", written as a class the
#: regex engine can scan at C speed.
#:
#: Python has no class that *is* `isalpha`: `\w` also takes digits and ``_``, and
#: subtracting `\d` and ``_`` leaves behind the 1,151 numeric oddities that are
#: neither alphabetic nor decimal -- ``²``, ``½``, ``৴``, ``Ⅻ``. Those are the only
#: over-match, and `_words_on_this_page` splits them back out.
#:
#: What must hold the other way is that the class never *under*-matches, because a
#: run it cut short would be a word the index does not hold and a rejoin
#: `_spelled_solid_on_this_page` would have made. It cannot: `isalpha` implies
#: `isalnum` implies `\w`, and no alphabetic character is decimal, so `\d` removes
#: none of them. Checked over all 136,726 alphabetic codepoints in
#: `tests/test_pdf_hyphen_cost.py` rather than argued, because the argument is
#: about Unicode and Unicode grows.
_WORD_RUN = re.compile(r"[^\W\d_]+")
#: Full-page scans one page may pay for before `_resolve_hyphens` indexes it instead.
#:
#: Scanning is cheaper *per candidate*; indexing is cheaper *per page*. Both are
#: linear in the page, so the crossover is a candidate count that barely moves with
#: page size -- measured 2026-08-03:
#:
#:     ===========================  =========  ===========  ==========  ==========
#:     page                             chars    miss scan       index   crossover
#:     ===========================  =========  ===========  ==========  ==========
#:     worst public-bench page          2,803      0.76 us    0.070 ms          93
#:     largest public-bench page        9,311      2.14 us    0.265 ms         124
#:     synthetic, all-distinct        159,999     38.50 us    4.070 ms         106
#:     synthetic, all-distinct        639,999    156.40 us   17.390 ms         111
#:     ===========================  =========  ===========  ==========  ==========
#:
#: 128 is just above that, and far above any real page: over the 4,061 pages of
#: public-bench the *worst* page holds 44 candidates and the 99th percentile holds
#: 20, so every ordinary document keeps the scan and pays nothing for this. A page
#: that does cross the threshold has by then spent at most one index's worth of
#: scanning, so the switch can cost it about 2x -- against the unbounded product it
#: removes: 16,000 markers on one page took 1.58 s where 2,000 took 0.026 s, and a
#: 32 MB text layer ran for hours inside a single `page_lines` call that
#: `Limits(max_seconds=...)` has no opportunity to interrupt. It now takes 4.3 s.
_SCANS_BEFORE_INDEX = 128

_BOLD_WEIGHT = 600  # PDFium reports 700 for bold faces, 400-425 for regular

#: Everything to strip before asking "is this line blank?".
#:
#: `str.strip()` handles NBSP and U+3000 because they are `isspace()`, but not the
#: zero-width family, so a line holding only a zero-width space is truthy to Python
#: and would become a `Line`, then a block, then an indexed chunk of nothing.
#:
#: Duplicated from `diceo.plaintext` on purpose: this module has **no** imports from
#: the rest of the package and keeping it that way is the point -- a cracker that can
#: be broken by an unrelated module's import error is the failure LangChain hit when a
#: PDF-side import made its CSV loader unusable. Two copies of one constant is the
#: cheaper of the two problems.
_BLANK_CHARS = (
    " \t\r\n\f\v   　"  # whitespace, including the non-breaking kinds
    "­"  # soft hyphen
    "​‌‍⁠"  # zero-width space / non-joiner / joiner, word joiner
    "‎‏‪‫‬‭‮"  # bidi marks, embedding, override
    "⁦⁧⁨⁩"  # bidi isolates
    "﻿"  # zero-width no-break space
)


def has_visible_text(text: str) -> bool:
    """Is there anything here a reader would see? See `_BLANK_CHARS`."""
    return bool(text.strip(_BLANK_CHARS))


def _spelled_solid_on_this_page(lowered: str, needle: str) -> bool:
    """Does this page write ``needle`` as one whole word somewhere?

    ``str.find`` rather than a token set: a set costs a scan plus a set build over
    every page (~2,000 tokens), where this costs one C-speed scan per *candidate*,
    and candidates are rare -- 4.3 per page on the 1,354-page budget appendix, 3.4
    per page over the 4,061 pages of public-bench. The boundary test is what makes
    `find` sound: without it ``appropriations`` would be confirmed by
    ``reappropriations``.

    Rare is not bounded, though, and the cost is a *product*: a candidate the page
    does not corroborate scans the page to the end, so the page pays
    candidates x characters. Measured on synthetic pages of 20 characters per
    marker, that is exactly the quadratic it looks like -- 0.026 s at 2,000
    markers, 1.58 s at 16,000, 4x per doubling -- and it happens inside one
    `page_lines` call, where no limit can interrupt it. So `_resolve_hyphens` pays
    per candidate only while candidates are rare and switches to
    `_words_on_this_page` when they stop being; see `_SCANS_BEFORE_INDEX`.
    """
    at = 0
    span = len(needle)
    while True:
        hit = lowered.find(needle, at)
        if hit < 0:
            return False
        after = hit + span
        if not (hit and lowered[hit - 1].isalpha()) and not (
            after < len(lowered) and lowered[after].isalpha()
        ):
            return True
        at = hit + 1


def _words_on_this_page(lowered: str) -> set[str]:
    """Every whole word the page writes, once -- the same evidence
    `_spelled_solid_on_this_page` hunts for, in a form that answers in O(1).

    Membership is *exactly* that function's answer, not an approximation of it, for
    any alphabetic needle. It accepts a hit only when neither neighbour is
    alphabetic, which is to say only when the hit is a maximal alphabetic run,
    which is to say only when it is one of these. A needle that is *not* purely
    alphabetic keeps the scan -- see `_resolve_hyphens` -- so the switch cannot
    move a decision either way.

    One pass, and it holds the page's *vocabulary* rather than the page, which is
    the difference between a bounded structure and a second copy of the document:
    0.9 MB for 33.5 MB of ordinary prose. The residual is a page built to defeat
    that -- 32 MB of nothing but distinct words indexes 4.4 million of them for
    340 MB -- and it is worth paying, because the same page costs more than that
    in `lowered`, `pieces` and the returned string before this function is
    reached, and because it is what turns hours into 4.3 s.

    `finditer` and not `findall` for the same reason (rule 2): the list `findall`
    builds is proportional to the page rather than to its vocabulary -- 4.2 MB
    against ~0 on that prose -- and the pages that reach this function are by
    definition the large ones. That costs 1.2-1.7x, paid at most once per page.
    """
    words: set[str] = set()
    for match in _WORD_RUN.finditer(lowered):
        run = match.group()
        if run.isalpha():
            words.add(run)
        else:
            # A superscript footnote marker or a fraction glued two words
            # together (`_WORD_RUN`). Cheap to split in Python because it is rare
            # -- 332 of 1,426,740 runs over public-bench, 0.023% -- but it is not
            # optional: ``appropriations²`` has to keep confirming
            # ``appropriations``, which is what the scan it replaces does.
            words.update("".join(g) for alpha, g in groupby(run, str.isalpha) if alpha)
    return words


def _resolve_hyphens(text: str) -> tuple[str, int, int]:
    """Decide, for each hyphen PDFium marked as ending a line, whether to delete it.

    PDFium does the hard half already and we were throwing it away. Measured over
    17,000 markers in the stress corpus (2026-07-31), **every** one of them has
    its following character on a different visual line -- x jumps back to the margin,
    or y drops a line, or both, and a column break jumps to the top of the next
    column. An ordinary mid-word hyphen never carries the marker; it arrives as
    U+002D. So "does this hyphen end a line?" needs no geometry and no heuristic.

    What PDFium cannot know is the half that matters: whether the typesetter
    *inserted* that hyphen to break a word (``appropri-ations`` -- delete it, or the
    word is unfindable) or merely broke the line at a hyphen the author wrote
    (``broker-dealer`` -- keep it, or the compound is destroyed). Deleting
    unconditionally is what PyMuPDF4LLM does and it is not a fix: adjudicated against
    each document's own vocabulary, it is right 5,447 times and wrong 168 on the
    budget appendix, but on `federal-register-2024-01-16.pdf` -- three narrow columns
    of ``broker-dealer``, ``security-based``, ``non-centrally`` -- it is right 126
    times and **wrong 393**.

    So the page adjudicates. Rejoin only when the page itself spells the joined form
    as a whole word somewhere else; otherwise leave the hyphen alone. That is the
    same evidence the defect was counted with, it needs nothing but the page already
    in hand (no dictionary, no document-level state, no second pass, identical output
    for any ``page_range``), and it cannot invent a word the document does not use.
    Measured over the four stress documents: 8,677 words rejoined, **zero** genuine
    compounds joined -- against 594 destroyed by the unconditional rule.

    The residual is a *miss*, not damage: 36% of hyphenated words are broken across a
    page boundary from their only solid spelling and stay broken. `hyphens_kept` in
    the diagnostics is that number, so it is visible rather than assumed (rule 3).

    Returns the page text with every marker rewritten to ``-`` or left as
    `_JOIN_MARK` -- same length either way -- and the two counts.

    Asking the page is one C-speed scan per candidate, which is the right price
    while candidates are rare and a denial of service when they are not: the work
    is candidates x page characters, and a page is indivisible, so no limit the
    caller sets can cut a page short. Past `_SCANS_BEFORE_INDEX` candidates this
    asks `_words_on_this_page` instead and the page becomes linear. The answer is
    the same one either way -- that is the whole design constraint here, and
    `tests/test_pdf_hyphen_cost.py` pins it by running both paths over the same
    randomized pages.
    """
    lowered = text.lower()
    limit = len(text)
    pieces: list[str] = []
    at = 0
    joined = kept = 0
    scans = 0
    words: set[str] | None = None
    for match in _HYPHEN_MARKER.finditer(text):
        here = match.start()
        pieces.append(text[at:here])
        at = here + 1
        # The alphabetic runs either side. A previous marker is not alphabetic, so
        # `a-b-c` stops at the nearer one rather than running through it.
        left = here
        while left and text[left - 1].isalpha():
            left -= 1
        right = at
        while right < limit and text[right].isalpha():
            right += 1
        solid = False
        if here - left >= _MIN_FRAGMENT and right - at >= _MIN_FRAGMENT:
            candidate = lowered[left:here] + lowered[at:right]
            scans += 1
            if scans == _SCANS_BEFORE_INDEX:  # once per page, never twice
                words = _words_on_this_page(lowered)
            # `str.lower` is length-preserving for every codepoint but ``İ``
            # (U+0130 -> ``i`` and a combining dot), which slides these slices off
            # the runs they were measured against. That misalignment predates this
            # function and is not what is being fixed here -- but it stays the
            # *same* misalignment only while both paths answer it identically, and
            # without this guard they do not: brute-forced over the alphabet
            # {İ, a, b, marker, space}, dropping it changes the decision on 312
            # nine-character pages. A candidate the slide left non-alphabetic is
            # not a word, so it goes back to the scan.
            solid = (
                candidate in words
                if words is not None and candidate.isalpha()
                else _spelled_solid_on_this_page(lowered, candidate)
            )
        if solid:
            pieces.append(_JOIN_MARK)
            joined += 1
        else:
            pieces.append("-")
            kept += 1
    pieces.append(text[at:])
    return "".join(pieces), joined, kept


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class Line:
    """One PDFium text line, with the font metrics needed to classify it.

    ``start``/``end`` are indices into *that page's* character array, which is
    also the index space of ``FPDFText_GetCharBox`` -- so a consumer can always
    get back to pixels on the page.
    """

    text: str
    page: int
    start: int
    end: int
    size: float
    bold: bool
    bbox: tuple[float, float, float, float]  # left, bottom, right, top
    #: Height of the first character's *loose* glyph box -- the font's
    #: ascent-to-descent extent at the rendered scale, which is proportional to
    #: the size a reader sees no matter what the producer wrote in ``Tf``. It is
    #: ``0.0`` unless the page's nominal sizes were degenerate, because the probe
    #: is one extra FFI call per line and PDF throughput is the headline number.
    #: The *loose* box and not ``bbox``: an ink box measures the glyphs that
    #: happen to be on the line, so "Introduction" and "page 3" differ at one
    #: font size. Measured on 911-report.pdf pages 20-27: the loose box takes 32
    #: distinct heights down to 5.
    em: float = 0.0

    @property
    def height(self) -> float:
        return self.bbox[3] - self.bbox[1]

    @property
    def width(self) -> float:
        return self.bbox[2] - self.bbox[0]


@dataclass(slots=True, frozen=True)
class Block:
    """The narrow waist of the pipeline (D1).

    Offsets are into *this page's* PDFium character array, so
    ``textpage.get_text_range(start, end - start)`` recovers the source span --
    which is what lets a UI highlight a citation on the rendered page.

    The D9 invariant ``source[start:end] == text`` holds **exactly** for
    single-line blocks (112 of 301 on the fixture) and only up to line-join
    whitespace for merged paragraphs (the other 189): joining two lines replaces
    ``"\\r\\n"`` with ``" "``, and de-hyphenation deletes a character -- at the
    join between two lines (`_Paragraph.flush`) and, far more often, *inside* one,
    where PDFium reports a hyphenated word as a single line (`_resolve_hyphens`).
    The span is therefore always correct as a *locator* and is not byte-exact as a
    *slice*. Anything that needs byte-exactness must re-slice the source itself.
    """

    kind: str  # heading | para | list_item | table_row | caption
    text: str
    page: int
    start: int  # first character index on the page
    end: int  # one past the last
    bbox: tuple[float, float, float, float]
    level: int | None = None  # 1..6, headings only
    row: int = -1  # 0: recovered table header; >0: data; -1: not established

    def __str__(self) -> str:
        if self.kind == "heading":
            return f"{'#' * (self.level or 1)} {self.text}"
        return self.text


@dataclass
class StyleModel:
    """What "body text" and "heading level 2" mean *in this document*.

    Fitted from a font-size census: the modal size weighted by characters is
    body text (weighting by characters and not by lines matters -- a paper has
    many short heading lines and few long body lines, and line-weighting lets a
    heading style win the mode).
    """

    body_size: float
    #: (rounded size, bold) -> heading level, ordered by size desc then bold.
    levels: dict[tuple[float, bool], int] = field(default_factory=dict)
    sizes: Counter = field(default_factory=Counter)
    #: ``"nominal"`` (the ``Tf`` size) or ``"em"`` (the rendered glyph box).
    #: Surfaced in diagnostics -- a silent fallback is how the degenerate-census
    #: defect survived unnoticed.
    size_source: str = "nominal"
    #: ``(low, high, bold, level)``, for the measured source only. A measurement
    #: needs a *band*: the exact-key lookup below finds a heading only if its
    #: glyph box matched a value already in the census, and on 911-report.pdf 13
    #: of the 17 distinct heights across 60 pages were not -- including the one
    #: carrying 105,117 characters.
    bands: tuple[tuple[float, float, bool, int], ...] = ()

    def level_for(self, size: float, bold: bool) -> int | None:
        if self.size_source == "em":
            for low, high, band_bold, level in self.bands:
                if band_bold == bold and low <= size <= high:
                    return level
            return None
        return self.levels.get((round(size, 1), bold))

    def size_of(self, line: Line) -> float:
        """The size signal this document is being classified by."""
        return line.em if self.size_source == "em" else line.size


class Census(dict):
    """style -> [character count, line count]. A dict so it stays picklable."""

    def add(self, size: float, bold: bool, length: int) -> None:
        entry = self.get((size, bold))
        if entry is None:
            self[(size, bold)] = [length, 1]
        else:
            entry[0] += length
            entry[1] += 1


def fit_styles(census: Census) -> StyleModel:
    """census: {(rounded size, bold): [chars, lines]} -> a StyleModel."""
    by_size: Counter = Counter()
    for (size, _bold), (chars, _n) in census.items():
        by_size[size] += chars
    if not by_size:
        return StyleModel(body_size=0.0)
    body = by_size.most_common(1)[0][0]

    candidates = [
        (size, bold)
        for (size, bold), (chars, count) in census.items()
        if (size > body * LARGER_THAN_BODY or (bold and size >= body * BOLD_MIN_RATIO))
        and chars / count <= HEADING_STYLE_MAX_MEAN
    ]
    # Bigger first; at equal size, bold outranks regular. Regular text that is
    # merely *larger* than body (a legal notice, a running title) does get a
    # level here, but the per-line length and angle tests usually reject it.
    candidates.sort(key=lambda sb: (-sb[0], not sb[1]))
    levels = {style: min(i + 1, 6) for i, style in enumerate(candidates)}
    return StyleModel(body_size=body, levels=levels, sizes=by_size)


def _fit_measured(census: Census) -> StyleModel:
    """``fit_styles`` for a census keyed on *measured* glyph-box heights.

    Two differences, both because the keys are a measurement rather than a short
    list of values an author chose:

    * neighbouring sizes cluster into one level (``LEVEL_SIZE_TOL``), so two font
      families set at the same point size do not become two heading levels;
    * styles past ``MAX_LEVELS`` are dropped instead of clamped to level 6.

    Kept as a separate function rather than a flag on ``fit_styles`` so the
    nominal path -- which every published retrieval number was measured on --
    stays untouched by construction.
    """
    by_size: Counter = Counter()
    for (size, _bold), (chars, _n) in census.items():
        by_size[size] += chars
    if not by_size:
        return StyleModel(body_size=0.0, size_source="em")
    body = by_size.most_common(1)[0][0]

    ranked = [
        (size, bold, count)
        for (size, bold), (chars, count) in census.items()
        if (size > body * LARGER_THAN_BODY or (bold and size >= body * BOLD_MIN_RATIO))
        and chars / count <= HEADING_STYLE_MAX_MEAN
    ]
    ranked.sort(key=lambda sbc: (-sbc[0], not sbc[1]))
    if ranked and sum(chars for chars, _n in census.values()) >= PRUNE_AFTER_CHARS:
        # The largest style is never pruned: a document whose only heading is its
        # title must still get that title.
        ranked = [ranked[0]] + [item for item in ranked[1:] if item[2] >= MIN_STYLE_LINES]
    candidates = [(size, bold) for size, bold, _count in ranked]

    levels: dict[tuple[float, bool], int] = {}
    level = 0
    previous: tuple[float, bool] | None = None
    for style in candidates:
        if previous is None:
            level = 1
        elif (
            style[1] != previous[1]
            or abs(style[0] - previous[0]) > previous[0] * LEVEL_SIZE_TOL
        ):
            level += 1
        if level > MAX_LEVELS:
            break
        levels[style] = level
        previous = style

    spans: dict[tuple[int, bool], list[float]] = {}
    for (size, bold), level in levels.items():
        spans.setdefault((level, bold), []).append(size)
    bands = tuple(
        (min(sizes) * (1 - LEVEL_SIZE_TOL), max(sizes) * (1 + LEVEL_SIZE_TOL), bold, level)
        for (level, bold), sizes in sorted(spans.items())
    )
    return StyleModel(
        body_size=body, levels=levels, sizes=by_size, size_source="em", bands=bands
    )


def _is_degenerate(census: Census) -> bool:
    """Can this census tell a heading from body text at all? See ``DEGENERATE_SIZE``."""
    if not census:
        return False
    by_size: Counter = Counter()
    for (size, _bold), (chars, _n) in census.items():
        by_size[size] += chars
    return by_size.most_common(1)[0][0] <= DEGENERATE_SIZE


def fit_model(census: Census, em_census: Census) -> StyleModel:
    """Fit a ``StyleModel``, falling back to glyph boxes when ``Tf`` is useless.

    The fallback needs the nominal census to be degenerate and *some* measured
    signal to exist. It deliberately does **not** require the measured census to
    discriminate yet: a degenerate nominal census is proof that the nominal signal
    carries no information at all, so a flat measured one is no worse, and the
    calibration window may simply be front matter -- which on 911-report.pdf it is
    for 32 pages. Refusing to switch on a flat window is refusing to ever learn.
    """
    if em_census and _is_degenerate(census):
        return _fit_measured(em_census)
    return fit_styles(census)


# --------------------------------------------------------------------------- #
# the PDFium layer: one page -> lines
# --------------------------------------------------------------------------- #

_c_double = ctypes.c_double


def _reading_order(line: Line) -> tuple[float, float]:
    """Top of the page first, then left. Sound only for text that is horizontal in
    *page* space, which is what `_reads_across_the_page` establishes."""
    return -line.bbox[3], line.bbox[0]


def _reads_across_the_page(page: list[Line]) -> bool:
    """Does this page's text run horizontally in *page* space, whatever /Rotate says?

    The two rotated shapes are different documents and only this tells them apart.
    A page somebody rotated *after* it was typeset -- a straightened scan, a
    ``pdftk rotate``, an imposition tool dropping a wide table in sideways -- still
    holds ordinary horizontal text, so its lines are wide and short in page space
    and their page-space top really is the author's reading order. A page
    ``pdflscape`` produced sets ``/Rotate`` *and* draws the text rotated to match,
    so its lines are narrow and tall, PDFium's display-frame order is already the
    author's, and sorting those boxes by page-space top ranks four consecutive
    lines 3, 2, 4, 1 -- a new bug in the place the old one was.

    Weighted by characters rather than by lines because a sideways table page
    usually carries a horizontal folio or running head, and one page number must
    not outvote the body.
    """
    across = sum(len(line.text) for line in page if line.width >= line.height)
    return 2 * across > sum(len(line.text) for line in page)


def page_lines(
    page: pdfium.PdfPage,
    index: int,
    *,
    want_angle: bool = True,
    control_removed: list[int] | None = None,
    hyphen_counts: list[int] | None = None,
    reordered: list[int] | None = None,
    replacement_chars: dict[int, int] | None = None,
) -> list[Line]:
    """All text lines on one page, in PDFium's reading order.

    Order note (the D4 risk): PDFium's character array is what
    ``get_text_bounded`` iterates, and the spans produced here *are* slices of
    that array. Reading order and inter-word spacing are therefore identical to
    PDFium's own text extraction by construction -- not approximated. Experiment
    004 measures the residual difference at 0.06% of characters, all of it the
    hyphen artefact above.

    ``hyphen_counts``, if given, is ``[rejoined, kept]`` and is added to in place --
    the out-parameter idiom ``control_removed`` uses. See `_resolve_hyphens`.
    ``reordered`` counts the pages whose lines had to be put back into the author's
    order; see the ``/Rotate`` block at the end of this function.
    """
    textpage = pdfium_c.FPDFText_LoadPage(page.raw)
    if not textpage:
        return []
    try:
        n_chars = pdfium_c.FPDFText_CountChars(textpage)
        if n_chars <= 0:
            return []
        buffer = (ctypes.c_ushort * (n_chars + 1))()
        pdfium_c.FPDFText_GetText(textpage, 0, n_chars, buffer)
        text = bytes(memoryview(buffer).cast("B")[: n_chars * 2]).decode("utf-16-le", "replace")
        if replacement_chars is not None:
            replacements = text.count("\ufffd")
            if replacements:
                replacement_chars[index] = replacements
        rejoined = 0
        if _HYPHEN_MARKER.search(text):
            text, rejoined, kept = _resolve_hyphens(text)
            if hyphen_counts is not None:
                hyphen_counts[0] += rejoined
                hyphen_counts[1] += kept
        # Non-BMP characters: PDFium indexes UTF-16 units, the decode above made each
        # surrogate pair one character, and every probe after it landed one late.
        # One character per unit keeps the indices; lines are re-paired below.
        paired = len(text) != n_chars
        if paired:
            text = "".join(map(chr, buffer[:n_chars]))
        marked = bool(rejoined)
        if _CONTROL_CHARS.search(text):
            # Marked, not deleted: deleting shortened the page string and misaligned
            # every font and box probe after it -- the reason `_JOIN_MARK` exists.
            text, removed = _CONTROL_CHARS.subn(_JOIN_MARK, text)
            if control_removed is not None:
                control_removed[0] += removed
            marked = True

        out: list[Line] = []
        left, bottom, right, top = _c_double(), _c_double(), _c_double(), _c_double()
        # `byref` allocates a fresh CArgObject per call, and these four targets never
        # change, so the references are built once instead of 8x per line.
        ref_left, ref_right = ctypes.byref(left), ctypes.byref(right)
        ref_bottom, ref_top = ctypes.byref(bottom), ctypes.byref(top)
        get_box = pdfium_c.FPDFText_GetCharBox
        get_size = pdfium_c.FPDFText_GetFontSize
        get_weight = pdfium_c.FPDFText_GetFontWeight
        get_angle = pdfium_c.FPDFText_GetCharAngle

        turn = pdfium_c.FPDFPage_GetRotation(page.raw) * (_TAU / 4) if want_angle else 0.0

        pos = 0
        limit = len(text)
        while pos < limit:
            stop = text.find("\r\n", pos)
            end = limit if stop < 0 else stop
            raw = text[pos:end]
            body = raw.strip()
            # Apply the de-hyphenation decided above. Done here and not in `text`
            # because `first`/`last` below index PDFium's character array; deleting
            # from the page string would misalign every probe after the first
            # hyphen. `Line.start`/`end` therefore stay a correct *locator* and stop
            # being a byte-exact slice -- which is what `Block` already documents.
            if marked and _JOIN_MARK in body:
                body = body.replace(_JOIN_MARK, "")
                # Probe real characters, never a mark: same length, so indices hold.
                raw = raw.replace(_JOIN_MARK, " ")
            if paired:
                body = body.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
            # `has_visible_text`, not just truthiness: a PDF line consisting only of
            # zero-width characters is invisible on the page and would otherwise
            # become a `Line`, then a block, then an indexed chunk of nothing.
            if body and has_visible_text(body):
                # Indices of the first and last non-space characters: probing a
                # space gives a degenerate box and an arbitrary font.
                first = pos + (len(raw) - len(raw.lstrip()))
                last = pos + len(raw.rstrip()) - 1
                size = get_size(textpage, first)
                bold = get_weight(textpage, first) >= _BOLD_WEIGHT
                if bold and last != first:
                    # A bold run-in lead-in ("Encoder: the encoder is...") is
                    # not a heading. Requiring both ends bold rejects it for
                    # one extra call.
                    bold = get_weight(textpage, last) >= _BOLD_WEIGHT
                get_box(textpage, first, ref_left, ref_right, ref_bottom, ref_top)
                x0, y0, y1 = left.value, bottom.value, top.value
                get_box(textpage, last, ref_left, ref_right, ref_bottom, ref_top)
                x1 = right.value
                if y1 < top.value:
                    y1 = top.value
                if y0 > bottom.value:
                    y0 = bottom.value
                if want_angle and size > 0:
                    # Rotated only if upright in *neither* frame. The angle is in page
                    # space, so a pdflscape page -- `/Rotate 90`, text drawn sideways
                    # to read upright -- measured 3pi/2 and lost every heading, while
                    # a page rotated after typesetting is upright in page space.
                    raw_angle = get_angle(textpage, first) % _TAU
                    shown = (raw_angle + turn) % _TAU
                    tilt = min(raw_angle, _TAU - raw_angle, shown, _TAU - shown)
                    if tilt > MAX_HEADING_ANGLE:
                        size = -size  # negative size == rotated; never a heading
                out.append(
                    Line(
                        text=body,
                        page=index,
                        start=first,
                        end=last + 1,
                        size=size,
                        bold=bold,
                        bbox=(x0, y0, x1, y1),
                    )
                )
            pos = end + 2
        if out and max(abs(line.size) for line in out) <= DEGENERATE_SIZE:
            # Every nominal size on this page is ~1: the producer scaled type with
            # the text matrix, so `size` cannot rank anything. Take a second,
            # rendered signal from the loose glyph box. Gated on the page being
            # degenerate so a document with honest `Tf` sizes -- which is nearly
            # all of them -- never pays the extra FFI call.
            out = _with_em(textpage, out)
        if out and pdfium_c.FPDFPage_GetRotation(page.raw):
            # `/Rotate` is a *viewing* instruction, but PDFium orders the character
            # array in the frame it produces, so a page that was rotated after it
            # was typeset comes back bottom-to-top: the heading lands last, and
            # `_emit_page`'s `previous.bbox[1] - line.bbox[3]` gap goes negative on
            # every pair, so each line of a paragraph becomes its own block. A
            # landscape appendix -- how every wide table in every government report
            # is typeset -- is both mis-ordered and shredded into one-line chunks.
            #
            # Measured against pypdfium2 152.0.7947.0, only quarter turn 1 actually
            # scrambles; 2 and 3 hand back content-stream order. The key is the same
            # for all three and is not a guess: `FPDFText_GetCharBox` reports
            # *unrotated page space* at every rotation (the same fixture's boxes are
            # identical whatever /Rotate says), so text that is horizontal in page
            # space reads top-then-left however the page is displayed. Sorting moves
            # whole records and never an index, so `Line.start`/`end` stay valid
            # PDFium character offsets and every font, weight and box probe above is
            # unaffected -- they were made before this line runs.
            #
            # The residual, unfixed and worth knowing: a page whose columns the
            # content stream interleaves is re-sorted here into row order, which is
            # the multi-column defect experiment 038 records, now reachable on
            # rotated pages that previously arrived in content order.
            ordered = sorted(out, key=_reading_order) if _reads_across_the_page(out) else out
            if ordered != out:
                # List `==` short-circuits on identity, so an already-ordered page
                # pays a pointer walk rather than a text compare.
                out = ordered
                if reordered is not None:
                    reordered[0] += 1
        return out
    finally:
        pdfium_c.FPDFText_ClosePage(textpage)


def _with_em(textpage: object, page: list[Line]) -> list[Line]:
    """Attach the loose-glyph-box height of each line's first character."""
    rect = pdfium_c.FS_RECTF()
    ref = ctypes.byref(rect)
    get_loose = pdfium_c.FPDFText_GetLooseCharBox
    out = []
    for line in page:
        em = rect.top - rect.bottom if get_loose(textpage, line.start, ref) else 0.0
        out.append(replace(line, em=em))
    return out


#: Pages to read before closing and reopening the ``PdfDocument``.
#:
#: This is not a micro-optimisation, it is the only reason memory is bounded.
#: PDFium caches every indirect object it parses on the *document*, and closing
#: a page does not release it -- ``FPDF_ClosePage`` frees the page, not the
#: objects behind it. Measured on paper-huge.pdf (1,500 pages, 217 MB), touching
#: text on every page with one open document:
#:
#:     ====================  ==============  ==========
#:     reopen every          peak RSS        pages/s
#:     ====================  ==============  ==========
#:     never (1 document)      1,462 MB         111
#:     300 pages                 362 MB         119
#:     100 pages                 190 MB         126
#:     ====================  ==============  ==========
#:
#: Peak RSS tracked ~6.5x the file size with a single open document, i.e. O(file
#: size), which fails the 300 MB acceptance criterion outright on any large PDF.
#: Reopening costs nothing measurable -- reparsing the xref is cheap next to the
#: page work, and a smaller cache is friendlier to L3 -- so there is no trade-off
#: to tune here, only a default to pick.
REOPEN_EVERY = 100

#: Serialises every PDFium call in this process.
#:
#: Not a precaution -- a requirement. PDFium's own documentation is unambiguous:
#: "simultaneous calls across different threads, **even with different
#: documents**, are not allowed and can lead to crashes or corruption", and the
#: remedy it names is a mutex. Without this, two threads each reading their own
#: PDF segfault the interpreter; that was reproduced here by
#: ``tests/test_readers.py::test_chunking_works_from_threads`` before the lock
#: existed, and a segfault is not a failure mode any caller can handle.
#:
#: The cost is one uncontended lock acquire per *page*, about 60 ns against a
#: ~1.3 ms page, so under 0.01%. The consequence a caller must know: PDF work is
#: **serialised** across threads. Threads therefore buy nothing for PDFs -- use
#: processes, which is what this package is designed for anyway (one worker per
#: document, D12). Every other format is genuinely parallel: they are pure Python
#: and stdlib ``zipfile``, with no shared C state.
#:
#: Re-entrant because ``blocks()`` may call ``font_census()``, which takes it too.
PDFIUM_LOCK = threading.RLock()


#: A page carrying no more text than this is treated as textless for the purpose of
#: asking whether it is a picture. It used to be zero -- ``if not found`` -- and that
#: is one character too strict to survive real documents. The IPBES French assessment
#: draws seven whole pages as bitmaps and stamps a **page number** on each: two
#: characters of text, so ``found`` is non-empty, so the image check never ran, so
#: 20,196 characters of table (measured against Docling on the same pages) were absent
#: from the index while ``pages_image_only=0``, ``needs_ocr=False`` and
#: ``lost_data=False`` all reported success. A page number, a running head and a folio
#: fit comfortably under 40; a page with a paragraph on it does not, and would not
#: reach the image check anyway on any document where this matters.
IMAGE_PAGE_MAX_CHARS = 40


def _has_image_gap(found: list[Line]) -> bool:
    """Suspicious sparse layout, without probing objects on ordinary short pages."""
    if any(
        _CAPTION.match(line.text) and not line.text.lstrip().lower().startswith("table")
        for line in found
    ):
        return True
    ordered = sorted(found, key=_reading_order)
    # An ordinary footer has a large gap above it; it is not missing image text.
    return any(
        upper.bbox[1] - lower.bbox[3] > max(72, 8 * max(upper.height, lower.height))
        for upper, lower in zip(ordered[1:-2], ordered[2:-1], strict=True)
    )


def _page_has_unread_image(page: pdfium.PdfPage, found: list[Line]) -> bool:
    """Large raster content without a corresponding text layer on a sparse page.

    ponytail: inspect 64 direct objects on sparse pages; deeper/dense image discovery
    needs a separately measured pass, rather than taxing every ordinary text page.
    """
    bounds = [ctypes.c_float() for _ in range(4)]
    refs = [ctypes.byref(value) for value in bounds]
    area = 0.0
    for index in range(min(pdfium_c.FPDFPage_CountObjects(page.raw), 64)):
        obj = pdfium_c.FPDFPage_GetObject(page.raw, index)
        if pdfium_c.FPDFPageObj_GetType(obj) != pdfium_c.FPDF_PAGEOBJ_IMAGE:
            continue
        if not pdfium_c.FPDFPageObj_GetBounds(obj, *refs):
            continue
        left, bottom, right, top = (value.value for value in bounds)
        if not area:
            width, height = page.get_size()
            area = width * height
        if (right - left) * (top - bottom) < area * 0.08:
            continue
        covered = sum(
            len(line.text)
            for line in found
            if line.bbox[0] < right
            and line.bbox[2] > left
            and line.bbox[1] < top
            and line.bbox[3] > bottom
        )
        if covered <= IMAGE_PAGE_MAX_CHARS:
            return True
    return False


def page_is_image(page: object) -> bool:
    """Does this page draw an image (or a form that contains one)?

    Only ever called for a page whose text is negligible (``IMAGE_PAGE_MAX_CHARS``),
    which is what keeps it cheap: on a born-digital document that is almost no pages,
    and on a scan it is every page but there was no text work to do anyway. Cost
    proportional to suspicion.

    The distinction it buys is the one that matters to a caller. "No text" has two
    completely different causes -- a deliberately blank separator page, which is
    fine, and a page that is a photograph of text, which means the document is
    **not in the index at all** and needs OCR. Every extractor reports both as
    success; telling them apart is most of what rule 3 is for.
    """
    handle = page.raw  # type: ignore[attr-defined]
    count = pdfium_c.FPDFPage_CountObjects(handle)
    for index in range(min(count, 24)):  # a scan's image is one of the first objects
        obj = pdfium_c.FPDFPage_GetObject(handle, index)
        kind = pdfium_c.FPDFPageObj_GetType(obj)
        if kind == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            return True
        if kind == pdfium_c.FPDF_PAGEOBJ_FORM:
            # A scanner that wraps its bitmap in a form XObject is common enough
            # (Xerox and Canon both do it) that missing it would defeat the check.
            inner_count = min(pdfium_c.FPDFFormObj_CountObjects(obj), 8)
            for inner in range(inner_count):
                sub = pdfium_c.FPDFFormObj_GetObject(obj, inner)
                if pdfium_c.FPDFPageObj_GetType(sub) == pdfium_c.FPDF_PAGEOBJ_IMAGE:
                    return True
    return False


def lines(
    path: str | Path,
    *,
    page_range: range | None = None,
    want_angle: bool = True,
    reopen_every: int = REOPEN_EVERY,
    image_only: list[int] | None = None,
    image_mixed: list[int] | None = None,
    replacement_chars: dict[int, int] | None = None,
    chars: list[int] | None = None,
    control_removed: list[int] | None = None,
    hyphen_counts: list[int] | None = None,
    reordered: list[int] | None = None,
    page_count: list[int] | None = None,
) -> Iterator[list[Line]]:
    """Yield one list of ``Line`` per page, lazily, in bounded memory.

    Set ``reopen_every=0`` to keep one document open for the whole file -- faster
    on small documents by a hair, and unbounded in memory on large ones.

    ``image_only``, if given, collects the indices of pages whose text is
    negligible and which draw an image -- the pages a caller must send to OCR.
    """
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(path)
        indices = range(len(doc))
        if page_range is not None:
            # Clipped to the document, so `range(max_pages)` is a safe cap.
            indices = indices[page_range.start : page_range.stop : page_range.step]
        if page_count is not None:
            page_count[0] = len(doc)
    try:
        since_open = 0
        for i in indices:
            # One page's worth of PDFium work, then the lock is released across the
            # `yield` so the consumer's Python work (classification, chunking,
            # embedding) never blocks another thread's page.
            with PDFIUM_LOCK:
                if reopen_every and since_open >= reopen_every:
                    doc.close()
                    doc = pdfium.PdfDocument(path)
                    since_open = 0
                since_open += 1
                page = doc[i]
                try:
                    found = page_lines(
                        page,
                        i,
                        want_angle=want_angle,
                        control_removed=control_removed,
                        hyphen_counts=hyphen_counts,
                        reordered=reordered,
                        replacement_chars=replacement_chars,
                    )
                    page_chars = (
                        sum(len(line.text) for line in found)
                        if chars is not None
                        or image_only is not None
                        or image_mixed is not None
                        else 0
                    )
                    if chars is not None:
                        chars[0] += page_chars
                    if (
                        image_only is not None
                        and page_chars <= IMAGE_PAGE_MAX_CHARS
                        and page_is_image(page)
                    ):
                        image_only.append(i)
                    elif (
                        image_mixed is not None
                        and IMAGE_PAGE_MAX_CHARS < page_chars <= 1000
                        and len(found) <= 12
                        and _has_image_gap(found)
                        and _page_has_unread_image(page, found)
                    ):
                        image_mixed.append(i)
                finally:
                    page.close()
            yield found
    finally:
        with PDFIUM_LOCK:
            doc.close()


def font_census(
    path: str | Path,
    page_range: range | None = None,
    reopen_every: int = REOPEN_EVERY,
    em_census: Census | None = None,
) -> Census:
    """A (size, bold) -> [chars, lines] census. Used by ``calibration="full"``.

    ``em_census``, if given, is filled in the same pass with the rendered
    glyph-box census -- the out-parameter idiom ``lines(image_only=...)`` uses,
    kept so the return type stays one thing for the callers that only want it.
    """
    census = Census()
    # want_angle=True is not optional here. Rotated furniture -- the 20 pt arXiv
    # stamp down the side of the fixture's first page -- is large and short, so
    # it satisfies every heading test and, once ranked, steals level 1 and pushes
    # every real heading one level deeper. Measured: skipping the angle probe
    # dropped level agreement against PyMuPDF4LLM from 26/26 to 0/26 while
    # leaving recall untouched, which is exactly the kind of error a
    # count-only metric never sees.
    for page in lines(path, page_range=page_range, want_angle=True, reopen_every=reopen_every):
        for line in page:
            census.add(round(line.size, 1), line.bold, len(line.text))
            if em_census is not None and line.em:
                em_census.add(round(line.em, 1), line.bold, len(line.text))
    return census


# --------------------------------------------------------------------------- #
# lines -> blocks
# --------------------------------------------------------------------------- #


def _is_heading(line: Line, model: StyleModel) -> int | None:
    # The `size <= 0` test stays on `size` whatever the source is: that is where
    # `page_lines` puts the rotated-text sentinel, and a 20 pt stamp down the side
    # of page 1 satisfies every other heading test.
    if line.size <= 0 or len(line.text) > HEADING_MAX_CHARS:
        return None
    level = model.level_for(model.size_of(line), line.bold)
    if level is None:
        return None
    # A "heading" that is mostly punctuation or digits is a page number, a
    # formula, or a table cell.
    letters = sum(ch.isalpha() for ch in line.text)
    if letters < 2 or letters < 0.4 * len(line.text):
        return None
    return level


class _Paragraph:
    """Accumulates consecutive body lines into one block. Bounded: one paragraph."""

    __slots__ = ("parts", "page", "start", "end", "box", "kind")

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.kind = "para"
        self.page = -1
        self.start = 0
        self.end = 0
        self.box = (0.0, 0.0, 0.0, 0.0)

    def add(self, line: Line, kind: str = "para") -> None:
        if not self.parts:
            self.kind, self.page, self.start = kind, line.page, line.start
            self.box = line.bbox
        else:
            left, bottom, right, top = self.box
            x0, y0, x1, y1 = line.bbox
            self.box = (min(left, x0), min(bottom, y0), max(right, x1), max(top, y1))
        self.parts.append(line.text)
        self.end = line.end

    def flush(self, *, row: int = -1) -> Block | None:
        if not self.parts:
            return None
        text = self.parts[0]
        for part in self.parts[1:]:
            # De-hyphenate across the line break; PDFium leaves the hyphen in. Not
            # before a digit or a capital, nor after a digit: `2019-` + `2020` read
            # `20192020`, `1-` + `stage` read `1stage`. Letter-spaced text still
            # joins (`ρ -` + `ι`), as it always did.
            if text.endswith("-") and not text.endswith("--"):
                if part[:1].islower() and not text[-2:-1].isdigit():
                    text = text[:-1] + part
                else:
                    text += part
            else:
                text = f"{text} {part}"
        block = Block(
            kind=self.kind,
            text=text,
            page=self.page,
            start=self.start,
            end=self.end,
            bbox=self.box,
            row=row,
        )
        self.parts = []
        return block


def _numeric_fields(text: str, fields: list[str] | None = None) -> int:
    """How many whitespace-separated tokens look like a number.

    The guard is a pure speed filter over `_NUMERIC_FIELD` and must stay a superset
    of it. `ch > "\\x7f"` is what keeps it cheap: an ASCII word -- 84% of tokens on
    the corpus, and every token in a Latin-script document -- is rejected by one set
    lookup and one character comparison, and only non-ASCII first characters pay for
    `isdigit()`, which is there to cover the Unicode `Nd` digits `\\d` also matches.
    """
    total = 0
    for token in text.split() if fields is None else fields:
        ch = token[0]
        if (ch in _NUMERIC_FIRST or (ch > "\x7f" and ch.isdigit())) and _NUMERIC_FIELD.match(
            token
        ):
            total += 1
    return total


def _is_data_row(text: str) -> bool:
    """Several numeric fields on one line, and mostly numeric.

    A second, geometry-free table signal, and it exists because the geometric one
    fails in the worst possible direction: it looks for gaps wider than the median
    advance, but a 14-column table packs its columns tight to fit the page, so the
    gaps vanish. **Detection degrades exactly as a table gets wider, which is when
    missing it costs most** -- a 14-column table arrived as one 1,814-character
    paragraph and `wide_table_cell` retrieval scored 0.000 against 0.846 for authored
    structure (experiment 020). With this it scores 0.538.

    The density requirement is what keeps footnotes and reference lines out: a data
    row runs 43-80% numeric fields, prose with a few numbers in it runs far lower.
    """
    fields = text.split()
    if len(fields) < _MIN_ROW_FIELDS:
        return False
    numeric = _numeric_fields(text, fields)
    return numeric >= _MIN_ROW_FIELDS and numeric / len(fields) >= _ROW_NUMERIC_SHARE


def _classify(line: Line, model: StyleModel, median_advance: float, tables: bool) -> str:
    if _CAPTION.match(line.text):
        return "caption"
    if _LIST_ITEM.match(line.text):
        return "list_item"
    if (
        tables
        and median_advance > 0
        and len(line.text) > 8
        and line.width / len(line.text) > median_advance * TABLE_DENSITY
    ):
        return "table_row"
    if tables and _is_data_row(line.text):
        return "table_row"
    return "para"


def _table_headers(
    page: list[Line], levels: list[int | None], kinds: list[str], model: StyleModel
) -> tuple[dict[int, Block], set[int], dict[int, int]]:
    """Recover compact label bands above numeric rows, without sorting the page.

    Some producers write column labels after their data in the content stream.
    Their existing glyph boxes still put them immediately above the table. Only
    those labels move; columns, surrounding prose and source character offsets stay
    intact. Uncertain rows retain ``row=-1`` rather than acquiring a numeric header.
    """
    headers: dict[int, Block] = {}
    consumed: set[int] = set()
    rows: dict[int, int] = {}
    runs = 0
    for is_table, indices in groupby(range(len(page)), key=lambda i: kinds[i] == "table_row"):
        if not is_table:
            continue
        run = list(indices)
        if len(run) < 2:
            continue
        runs += 1
        # ponytail: bounded band scans, at most 16 table runs/page; a spatial index
        # is only warranted if dense, fragmented table pages need more recovery.
        if runs > 16:
            break
        first_data = next((i for i in run if _is_table_data_row(page[i].text)), None)
        if first_data is None or run[-1] == first_data:
            continue
        first = page[first_data]
        height = first.height or model.body_size
        data = run[run.index(first_data) :]
        left = min(page[i].bbox[0] for i in data)
        right = max(page[i].bbox[2] for i in data)
        top = first.bbox[3]
        size = model.size_of(first)
        labels = [
            i
            for i, line in enumerate(page)
            if i not in consumed
            and levels[i] is None
            and kinds[i] in ("para", "table_row")
            and top <= line.bbox[1] <= line.bbox[3] <= top + 8 * height
            and left - 5 * height <= line.bbox[0] < line.bbox[2] <= right + 5 * height
            and 0 < line.height <= 2.25 * height
            and model.size_of(line) <= size * 1.15
            and len(line.text) <= 180
            and line.text[-1:] not in ".;!?:"
            and not _is_table_data_row(line.text)
        ]
        if not labels:
            continue
        if len(labels) == 1:
            label = page[labels[0]]
            if len(label.text.split()) < 2 or (
                not label.bold
                and kinds[labels[0]] != "table_row"
                and label.width / len(label.text)
                <= first.width / len(first.text) * TABLE_DENSITY
            ):
                continue
        # Overlapping horizontal label fragments belong to the same header region.
        # A minimum overlap prevents one long line from joining adjacent columns.
        groups: list[list[Line]] = []
        for i in sorted(labels, key=lambda i: page[i].bbox[0]):
            line = page[i]
            for group in groups:
                if any(
                    min(part.bbox[2], line.bbox[2]) - max(part.bbox[0], line.bbox[0])
                    >= 0.5 * min(part.width, line.width)
                    for part in group
                ):
                    group.append(line)
                    break
            else:
                groups.append([line])
        text = " / ".join(
            " ".join(line.text for line in sorted(group, key=_reading_order))
            for group in groups
        )
        selected = [page[i] for i in labels]
        headers[first_data] = Block(
            kind="table_row",
            text=text,
            page=first.page,
            start=min(line.start for line in selected),
            end=max(line.end for line in selected),
            bbox=(
                min(line.bbox[0] for line in selected),
                min(line.bbox[1] for line in selected),
                max(line.bbox[2] for line in selected),
                max(line.bbox[3] for line in selected),
            ),
            row=0,
        )
        consumed.update(labels)
        rows.update((i, number) for number, i in enumerate(data, 1))
    return headers, consumed, rows


def _is_period_header(fields: list[str], numeric: int | None = None) -> bool:
    """A named row with several year columns, including repeated quarterly years."""
    if not fields or not fields[0][0].isalpha():
        return False
    years = 0
    for token in fields:
        if (
            len(token) == 4
            and token.isascii()
            and token.isdigit()
            and 1900 <= int(token) <= 2200
        ):
            years += 1
        elif _NUMERIC_FIELD.match(token):
            # A single non-year measurement disqualifies the whole header. Most
            # data rows stop here rather than examining every remaining column.
            return False
    return years >= 2 and (numeric is None or years == numeric)


def _is_table_data_row(text: str) -> bool:
    """Numeric content in an already geometrically identified table run."""
    fields = text.split()
    numeric = _numeric_fields(text, fields)
    return bool(
        numeric
        and numeric / len(fields) >= _ROW_NUMERIC_SHARE
        and not _is_period_header(fields, numeric)
    )


def _emit_page(
    page: list[Line],
    model: StyleModel,
    *,
    tables: bool,
    median_advance: float,
    diagnostics: dict | None = None,
) -> Iterator[Block]:
    """Turn one page's lines into blocks. Paragraphs never span a page here --
    a deliberate simplification; the chunker can rejoin using ``page``/``bbox``."""
    para = _Paragraph()
    previous: Line | None = None
    levels = [_is_heading(line, model) for line in page] if tables else []
    kinds = (
        [
            _classify(line, model, median_advance, True) if level is None else "heading"
            for line, level in zip(page, levels, strict=True)
        ]
        if tables
        else []
    )
    headers, consumed, rows = (
        _table_headers(page, levels, kinds, model)
        if tables and kinds.count("table_row") > 1
        else ({}, set(), {})
    )
    if diagnostics is not None and headers:
        diagnostics["table_headers_recovered"] = diagnostics.get(
            "table_headers_recovered", 0
        ) + len(headers)
    row_run = False
    for index, line in enumerate(page):
        if index in consumed:
            continue
        if index in headers:
            block = para.flush()
            if block:
                yield block
            yield headers[index]
        level = levels[index] if tables else _is_heading(line, model)
        if level is not None:
            row_run = False
            block = para.flush()
            if block:
                yield block
            yield Block(
                kind="heading",
                text=line.text,
                page=line.page,
                start=line.start,
                end=line.end,
                bbox=line.bbox,
                level=level,
            )
            previous = line
            continue

        kind = kinds[index] if tables else _classify(line, model, median_advance, False)
        if (
            kind == "table_row"
            and index not in rows
            and not row_run
            and diagnostics is not None
        ):
            diagnostics["tables_without_headers"] = (
                diagnostics.get("tables_without_headers", 0) + 1
            )
        row_run = kind == "table_row"
        # A caption may wrap; its next line must stay in the same font and region.
        # Font/column changes are stronger boundaries than a guessed vertical gap.
        if (
            kind == "para"
            and previous is not None
            and para.kind == "caption"
            and line.size == previous.size
            and abs(line.bbox[0] - previous.bbox[0]) < line.height
        ):
            kind = "caption"
        gap_break = False
        if previous is not None:
            gap = previous.bbox[1] - line.bbox[3]  # previous bottom - this top
            height = line.height or model.body_size
            gap_break = gap > PARA_BREAK_GAP * height or gap < -height
        if kind != para.kind or gap_break:
            block = para.flush()
            if block:
                yield block
        if kind in ("table_row", "list_item"):
            # These are row/item-granular: one block each, no merging across
            # rows, because a merged table is unreadable.
            para.add(line, kind)
            block = para.flush(row=rows.get(index, -1))
            if block:
                yield block
        else:
            para.add(line, kind)
        previous = line
    block = para.flush()
    if block:
        yield block


# --------------------------------------------------------------------------- #
# running heads and feet
# --------------------------------------------------------------------------- #

#: A margin line whose digit-normalised text is seen on this many distinct pages is
#: furniture from then on.
FURNITURE_PAGES = 2
#: A running head is a label, not a sentence. Measured against the **digit-normalised
#: key**, not the raw line, and that distinction is the whole finding of
#: experiment 041 (stress corpus) result 5: the Federal Register's
#: production stamp
#:
#:     VerDate Sep<11>2014 16:02 Jan 12, 2024 Jkt 262001 PO 00000 Frm ... 16JAR1.SGM 16JAR1
#:
#: is 113 raw characters and so was rejected as "too long to be a label" -- while the key it
#: would have been filed under, ``verdate sep<#># #:# jan #, # jkt # po # ...``, is 83. It is
#: the single largest piece of furniture in that document (45,669 chars, 1.658% of the
#: output, on all 401 pages) and `suppress_furniture` removed everything except it.
#:
#: Measuring the key is also the more principled test. What makes a line furniture is its
#: *shape* repeating, and the key is exactly the shape with the varying parts removed -- a
#: stamp whose length is mostly serial numbers is still a label.
FURNITURE_MAX_CHARS = 90
#: How many lines at each end of a page count as "in the margin". Positional rather
#: than geometric because a `Line` carries its own bbox but not the page height, and
#: reading order already puts the running head first and the foot last.
FURNITURE_EDGE_LINES = 2
_FURNITURE_DIGITS = re.compile(r"\d+")


def _furniture_key(text: str, side: str) -> tuple[str, str]:
    """Digit-normalised text, plus which edge of the page it sat on.

    Normalising digits is what makes ``Internal 1`` and ``Internal 12`` one key. The
    **side** is in the key because normalisation alone is too coarse: every digit-only
    line collapses to ``#``, so a bare page-number footer would teach a key that then
    deletes a genuine ``2025`` section title at the top of a later page. A footer
    teaches a bottom key and a title is a top key, so they never collide.
    """
    return side, _FURNITURE_DIGITS.sub("#", text).strip().lower()


def _edge_sides(count: int) -> dict[int, str]:
    """Line index -> ``"t"``/``"b"`` for the positions that count as margin.

    On a page with few lines, ``first two + last two`` covers everything and a repeated
    *body* sentence would look like a running head, so a short page offers only its
    very first and very last line.
    """
    if count <= 0:
        return {}
    if count <= 2 * FURNITURE_EDGE_LINES:
        return {0: "t", count - 1: "b"}
    sides = {index: "t" for index in range(FURNITURE_EDGE_LINES)}
    sides.update({index: "b" for index in range(count - FURNITURE_EDGE_LINES, count)})
    return sides


class _Furniture:
    """Running heads and feet, learned continuously as the document streams.

    Learning only from the calibration buffer is not enough, and the mistake is easy to
    make: a real report opens with a cover page and a table of contents, so its running
    head starts on page 5 and a buffer-only learner suppresses **nothing at all**. Our
    generated corpus carries its header from page 1, which is precisely why that version
    measured clean. So keys are observed on every page and promoted once seen on
    ``FURNITURE_PAGES`` distinct pages.

    The consequence is honest rather than hidden: the copies seen before a key was known
    have already been emitted, and ``leaked`` counts them.
    """

    def __init__(self) -> None:
        self.pages: dict[tuple[str, str], set[int]] = {}
        self.known: set[tuple[str, str]] = set()
        self.leaked = 0
        self._last_page: list[Line] | None = None
        self._last_keys: dict[int, tuple[str, str]] = {}

    def _keys(self, page: list[Line]) -> dict[int, tuple[str, str]]:
        # Observation and stripping use the same four margin keys. Cache only
        # this page, so continuous furniture learning stays bounded in memory.
        if page is not self._last_page:
            self._last_page = page
            self._last_keys = {
                index: _furniture_key(page[index].text, side)
                for index, side in _edge_sides(len(page)).items()
                if page[index].text.strip()
            }
        return self._last_keys

    def observe(self, page: list[Line]) -> None:
        for index, key in self._keys(page).items():
            if len(key[1]) > FURNITURE_MAX_CHARS:
                continue
            if key in self.known:
                continue
            seen = self.pages.setdefault(key, set())
            seen.add(page[index].page)
            if len(seen) >= FURNITURE_PAGES:
                self.known.add(key)
                # Every page but this one already went out with the line on it.
                self.leaked += len(seen) - 1

    def observe_all(self, pages: list[list[Line]]) -> None:
        for page in pages:
            self.observe(page)

    def strip(self, page: list[Line]) -> tuple[list[Line], int]:
        """Drop the learned heads and feet from one page."""
        if not self.known:
            return page, 0
        keys = self._keys(page)
        kept: list[Line] = []
        dropped = 0
        for index, line in enumerate(page):
            key = keys.get(index)
            if key is not None and len(key[1]) <= FURNITURE_MAX_CHARS and key in self.known:
                dropped += 1
                continue
            kept.append(line)
        if not kept:
            # Suppression must never empty a page. A document whose every page is the
            # same two short lines is degenerate but real -- a form, a slide export --
            # and deleting all of it would be the worst possible failure (rule 3).
            return page, 0
        return kept, dropped


def _report_model(stats: dict, model: StyleModel) -> None:
    """Make the style model observable (rule 3).

    ``heading_levels == 0`` is the observable that would have caught the
    degenerate-census defect in one glance: a 585-page government report with no
    heading styles fitted is a defect, not a document without headings.

    ``heading_styles`` and ``heading_levels`` are deliberately two numbers. ``levels``
    is keyed by ``(size, bold)``, so its length counts *styles*, and a document that
    refits as it streams accumulates many styles mapping onto the same few levels --
    the Federal Register fits 31 styles across 49 refits and emits levels 1-4. Reporting
    the style count under the name ``heading_levels`` said "31 heading levels" about a
    document that has four, which is the kind of number this field exists to prevent.
    """
    stats["size_source"] = model.size_source
    stats["heading_styles"] = len(model.levels)
    stats["heading_levels"] = len(set(model.levels.values()))


def blocks(
    path: str | Path,
    *,
    calibration: str = "bootstrap",
    calibration_pages: int = 4,
    tables: bool = False,
    furniture: bool = False,
    page_range: range | None = None,
    reopen_every: int = REOPEN_EVERY,
    diagnostics: dict | None = None,
) -> Iterator[Block]:
    """Stream ``Block``s out of a PDF.

    Args:
        calibration: ``"bootstrap"`` fits the font model on the first
            ``calibration_pages`` pages (buffered, then drained) -- one pass.
            ``"full"`` makes a metrics-only pass over the whole document first;
            more faithful on documents with unrepresentative front matter, and
            measured ~1.7x slower.
        tables: enable the (weak, geometric) table-row heuristic. Off by
            default because its precision is unproven and it is not free.
        diagnostics: if given, updated in place with ``pages``,
            ``pages_without_text`` and ``chars`` -- D7's no-silent-loss rule
            needs the count to come out of the same pass. Also
            ``hyphens_rejoined``/``hyphens_kept``: words put back together across a
            line break, and line-break hyphens left in place because the page did
            not corroborate the join (`_resolve_hyphens`).

    Yields:
        ``Block`` in reading order. At most ``calibration_pages`` pages of lines
        are resident at any time.
    """
    stats = diagnostics if diagnostics is not None else {}
    stats.setdefault("pages", 0)
    stats.setdefault("pages_without_text", 0)
    stats.setdefault("chars", 0)
    stats.setdefault("furniture_lines", 0)
    stats.setdefault("size_source", "nominal")
    stats.setdefault("heading_levels", 0)
    stats.setdefault("model_refits", 0)
    stats.setdefault("control_chars_removed", 0)
    stats.setdefault("hyphens_rejoined", 0)
    stats.setdefault("hyphens_kept", 0)
    stats.setdefault("pages_reordered", 0)
    #: Per-call, so two documents on two threads cannot share it (D16).
    control_removed = [0]
    #: Pages put back into the author's order -- see the `/Rotate` block in
    #: `page_lines`. Reported because it is a *repair*, and a caller diffing our
    #: output against another extractor's needs to know where we disagreed on
    #: purpose rather than by accident (rule 3).
    reordered = [0]
    #: ``[rejoined, kept]`` -- see `_resolve_hyphens`. `hyphens_kept` is the residual
    #: defect, and reporting it is rule 3: a word this reader knowingly left broken
    #: is a word the caller's index cannot find.
    hyphen_counts = [0, 0]
    since_refit = 0

    model: StyleModel | None = None
    if calibration == "full":
        measured = Census()
        nominal = font_census(path, page_range, reopen_every=reopen_every, em_census=measured)
        model = fit_model(nominal, measured)
        _report_model(stats, model)
    elif calibration != "bootstrap":
        raise ValueError(f"unknown calibration: {calibration!r}")

    census = Census()
    #: The same census keyed on rendered glyph-box height instead of nominal size,
    #: so the nominal-vs-measured decision can be made *after* the calibration pass
    #: without re-reading anything. Stays empty unless `page_lines` found a
    #: degenerate page, so this costs one dict update per line on the documents
    #: that need it and nothing at all on the rest.
    em_census = Census()
    #: Digit-normalised running heads and feet, learned from the calibration buffer.
    learner = _Furniture() if furniture else None
    advances: list[float] = []
    buffered: list[list[Line]] = []

    def median_advance() -> float:
        if not advances:
            return 0.0
        return sorted(advances)[len(advances) // 2]

    image_only: list[int] = []
    stats.setdefault("pages_image_only", image_only)
    image_mixed: list[int] = []
    stats.setdefault("pages_image_mixed", image_mixed)
    replacement_chars: dict[int, int] = {}
    stats.setdefault("pages_with_replacement_chars", replacement_chars)
    stats.setdefault("replacement_chars", 0)
    chars = [0]
    spacing = 0.0
    page_count = [0]
    for page in lines(
        path,
        page_range=page_range,
        reopen_every=reopen_every,
        image_only=image_only,
        image_mixed=image_mixed,
        replacement_chars=replacement_chars,
        chars=chars,
        control_removed=control_removed,
        hyphen_counts=hyphen_counts,
        reordered=reordered,
        page_count=page_count,
    ):
        stats["pages"] += 1
        stats["page_count"] = page_count[0]
        # Updated per page rather than after the loop: `blocks()` is a generator, and a
        # caller that stops early with `islice` never reaches code past the loop.
        stats["control_chars_removed"] = control_removed[0]
        stats["hyphens_rejoined"], stats["hyphens_kept"] = hyphen_counts
        stats["pages_reordered"] = reordered[0]
        stats["replacement_chars"] = sum(replacement_chars.values())
        stats["chars"] = chars[0]
        if not page:
            stats["pages_without_text"] += 1
            continue

        if model is None:
            for line in page:
                census.add(round(line.size, 1), line.bold, len(line.text))
                if line.em:
                    em_census.add(round(line.em, 1), line.bold, len(line.text))
                if len(line.text) > 20:
                    advances.append(line.width / len(line.text))
            buffered.append(page)
            if len(buffered) < calibration_pages:
                continue
            model = fit_model(census, em_census)
            _report_model(stats, model)
            spacing = median_advance()
            # The buffer is why furniture can be suppressed on page 1: the keys are
            # learned from these pages before any of them is emitted.
            if learner is not None:
                learner.observe_all(buffered)
            for held in buffered:
                kept, dropped = learner.strip(held) if learner is not None else (held, 0)
                stats["furniture_lines"] += dropped
                if kept:
                    yield from _emit_page(
                        kept, model, tables=tables, median_advance=spacing, diagnostics=stats
                    )
            buffered.clear()
            continue

        if model.size_source == "em":
            # Keep learning: the calibration window was front matter (see
            # EM_REFIT_PAGES). Confined to the measured path, so the nominal path
            # -- every published number -- is classified once, as before.
            for line in page:
                if line.em:
                    em_census.add(round(line.em, 1), line.bold, len(line.text))
            since_refit += 1
            if since_refit >= EM_REFIT_PAGES:
                since_refit = 0
                model = _fit_measured(em_census)
                stats["model_refits"] += 1
                _report_model(stats, model)

        if not advances or calibration == "full":
            for line in page:
                if len(line.text) > 20:
                    advances.append(line.width / len(line.text))
                    if len(advances) > 400:
                        break
            spacing = median_advance()
        # Observe *before* stripping, so a head that first appears after the
        # calibration window is still learned -- the cover-page-and-contents case.
        if learner is not None:
            learner.observe(page)
        kept, dropped = learner.strip(page) if learner is not None else (page, 0)
        stats["furniture_lines"] += dropped
        if kept:
            yield from _emit_page(
                kept, model, tables=tables, median_advance=spacing, diagnostics=stats
            )

    stats["page_count"] = page_count[0]
    if learner is not None:
        stats["furniture_leaked"] = learner.leaked
    if buffered:  # document shorter than the calibration window
        model = fit_model(census, em_census)
        _report_model(stats, model)
        spacing = median_advance()
        if learner is not None:
            learner.observe_all(buffered)
        for held in buffered:
            kept, dropped = learner.strip(held) if learner is not None else (held, 0)
            stats["furniture_lines"] += dropped
            if kept:
                yield from _emit_page(
                    kept, model, tables=tables, median_advance=spacing, diagnostics=stats
                )
