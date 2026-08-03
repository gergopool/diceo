"""A word broken across a line break must not be indexed with a hyphen in it.

Measured 2026-07-31 on `us-budget-appendix-2024.pdf` from the stress corpus (1,354
pages): our own extraction contained **5,921 occurrences of a word broken by a
leftover hyphen**, across 1,970 distinct words -- `appropri-ations` x89,
`appro-priations` x64, `depart-ment` x63, `devel-opment` x49. The count is
conservative: it only counts `a-b` when the same document also spells `ab` solid
somewhere, so `broker-dealer` and other real compounds are excluded. A query for
"appropriations" cannot match `appropri-ations`; the word is present and unfindable.

The cause is that PDFium hands the break back *inside a single line*. It marks the
hyphen it broke at with U+0002 (spelled U+FFFE in the raw text `FPDFText_GetText`
returns) and then does **not** emit the `\\r\\n` that separates every other pair of
lines, so the split `_Paragraph.flush` de-hyphenates at never happens:

    >>> [ln.text for ln in page_lines(doc[20], 20)]
    ['... There are authorized to be appro\\ufffepriated for fiscal year 2023 ...']

and `_HYPHEN_ARTEFACT` then rewrote that marker to a literal `-`, converting
PDFium's "I broke a word here" into "the author wrote a hyphen here".

The marker is a *perfect* line-break signal -- over 17,000 of them across the stress
corpus, every one has its next character on a different visual line -- but it says
nothing about whether the hyphen was the typesetter's or the author's. Deleting it
unconditionally is what PyMuPDF4LLM does, and it is the mirror-image defect: on
`federal-register-2024-01-16.pdf` it is right 126 times and wrong 393, destroying
`broker-dealer`, `security-based` and `non-centrally`. So both directions are pinned
here, and both directions matter to retrieval.
"""

from __future__ import annotations

import ctypes

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

import diceo
from diceo.pdf.extract import Line, _Paragraph, _resolve_hyphens, blocks, page_lines

from .fixtures import _scaled_pdf, tiny_pdf

#: The marker PDFium leaves where it broke a word, in both of its spellings.
MARKERS = ("￾", "\x02")

#: Two visual lines, the first ending mid-word. The page also spells the joined form
#: solid, which is the evidence the reader requires before rejoining anything.
BROKEN_WORD = [
    "The Congress made an appropri-",
    "ations decision. Total appropriations rose.",
]
#: The same shape, but the hyphen is the author's. Nothing on the page spells
#: `brokerdealer`, so joining it would be an invention.
GENUINE_COMPOUND = [
    "We regulate every broker-",
    "dealer today. The broker-dealer rules apply.",
]


def _text(path) -> str:
    return " ".join(block.text for block in blocks(path))


def _raw_page_text(path) -> str:
    """Exactly what `FPDFText_GetText` returns, markers and all."""
    doc = pdfium.PdfDocument(path)
    page = doc[0]
    textpage = pdfium_c.FPDFText_LoadPage(page.raw)
    try:
        count = pdfium_c.FPDFText_CountChars(textpage)
        buffer = (ctypes.c_ushort * (count + 1))()
        pdfium_c.FPDFText_GetText(textpage, 0, count, buffer)
        return bytes(memoryview(buffer).cast("B")[: count * 2]).decode("utf-16-le", "replace")
    finally:
        pdfium_c.FPDFText_ClosePage(textpage)
        page.close()


# --------------------------------------------------------------------------- #
# guard the guard -- if PDFium ever changes, the rest of this module is theatre
# --------------------------------------------------------------------------- #


def test_pdfium_reports_the_broken_word_as_one_line_with_a_marker(tmp_path):
    """The defect's mechanism, stated as a test.

    Two things have to hold together for the bug to exist: the marker is there, and
    the line break is *not*. If PDFium ever starts emitting `\\r\\n` after the marker,
    `_Paragraph.flush` handles this on its own and `_resolve_hyphens` is dead code.
    """
    path = tmp_path / "broken.pdf"
    path.write_bytes(tiny_pdf(BROKEN_WORD))

    raw = _raw_page_text(path)

    assert "appropri￾ations" in raw, repr(raw)
    assert "\r\n" not in raw.split("appropri")[1][:12], "PDFium now splits the line"


def test_the_marker_is_a_line_break_signal_and_a_real_hyphen_is_not(tmp_path):
    """PDFium distinguishes the two for us; the old code threw the distinction away.

    A hyphen the author wrote arrives as U+002D. A hyphen PDFium broke a line at
    arrives as the marker. That is the entire signal `_resolve_hyphens` rests on.
    """
    path = tmp_path / "compound.pdf"
    path.write_bytes(tiny_pdf(GENUINE_COMPOUND))

    raw = _raw_page_text(path)

    assert "broker￾dealer today" in raw, repr(raw)  # broken at the line end
    assert "broker-dealer rules" in raw, repr(raw)  # mid-line, a plain hyphen


# --------------------------------------------------------------------------- #
# both directions
# --------------------------------------------------------------------------- #


def test_a_word_broken_by_hyphenation_is_rejoined(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_bytes(tiny_pdf(BROKEN_WORD))

    text = _text(path)

    assert "appropriations decision" in text, text
    assert "appropri-ations" not in text, text


def test_a_compound_broken_at_its_own_hyphen_keeps_the_hyphen(tmp_path):
    """The mirror-image defect, and the one that is *harder* to see.

    Measured on `federal-register-2024-01-16.pdf`, PyMuPDF4LLM turns `broker-dealer`
    into `brokerdealer` 53 times. A fix that trades one error for the other is not a
    fix, so this is asserted as hard as the case above.
    """
    path = tmp_path / "compound.pdf"
    path.write_bytes(tiny_pdf(GENUINE_COMPOUND))

    text = _text(path)

    assert "broker-dealer today" in text, text
    assert "brokerdealer" not in text, text


def test_the_page_has_to_corroborate_the_join(tmp_path):
    """The mechanism itself: identical break, opposite outcome, one word of difference.

    The reader never guesses and never consults a dictionary -- it rejoins only what
    the page in hand already spells as a whole word. That is what makes the rule
    language-independent, streaming (no document-level state, no second pass) and
    identical for any `page_range`.
    """
    without = tmp_path / "without.pdf"
    without.write_bytes(tiny_pdf(["A helpful super-", "vision note here."]))
    with_it = tmp_path / "with.pdf"
    with_it.write_bytes(
        tiny_pdf(["A helpful super-", "vision note here. Supervision matters."])
    )

    assert "super-vision" in _text(without), _text(without)
    assert "supervision note" in _text(with_it), _text(with_it)


def test_a_substring_is_not_corroboration(tmp_path):
    """`appropriations` must not be confirmed by `reappropriations`.

    `_spelled_solid_on_this_page` uses `str.find`, which is why it also has to test
    the boundary -- without that, any word containing the joined form as a substring
    would license the join.
    """
    path = tmp_path / "substring.pdf"
    path.write_bytes(tiny_pdf(["The office was under-", "staffed. Note the understaffedness."]))

    assert "under-staffed" in _text(path), _text(path)


def test_a_one_letter_fragment_is_left_alone(tmp_path):
    """`e-mail` is not evidence of anything; see `_MIN_FRAGMENT`."""
    path = tmp_path / "email.pdf"
    path.write_bytes(tiny_pdf(["Send it by e-", "mail. The email arrived."]))

    assert "e-mail" in _text(path), _text(path)


# --------------------------------------------------------------------------- #
# the invariant the rewrite must not break
# --------------------------------------------------------------------------- #


def test_the_rewrite_never_changes_the_length_of_the_page(tmp_path):
    """`Line.start`/`end` index PDFium's character array and every font, weight,
    angle and box probe is made at one of them. Deleting the hyphen from the page
    string would misalign every metric after the first hyphenated word -- silently,
    because the text would still look right."""
    page = "one appro￾priation and appropriation, two broker￾dealer here"

    rewritten, joined, kept = _resolve_hyphens(page)

    assert len(rewritten) == len(page)
    assert (joined, kept) == (1, 1)
    assert rewritten.count("￾") == 1 and "broker-dealer" in rewritten


def test_font_metrics_still_land_on_the_right_character_after_a_rejoin(tmp_path):
    """The consequence of the invariant above, measured through the real API.

    Three rejoins happen before the heading, so an implementation that shortened the
    page string would probe three characters early -- inside the body line -- and
    report body size for a 24 pt heading.
    """
    path = tmp_path / "offsets.pdf"
    path.write_bytes(
        _scaled_pdf(
            [
                [
                    (12.0, 1.0, "Total appropriations development programs listed"),
                    (12.0, 1.0, "The appropri-"),
                    (12.0, 1.0, "ations and the devel-"),
                    (12.0, 1.0, "opment of pro-"),
                    (12.0, 1.0, "grams matter."),
                    (24.0, 1.0, "Heading After The Rejoins"),
                ]
            ]
        )
    )

    sizes = {line.text: line.size for page in [page_lines_of(path)] for line in page}

    assert sizes.get("Heading After The Rejoins") == 24.0, sizes


def page_lines_of(path) -> list[Line]:
    doc = pdfium.PdfDocument(path)
    page = doc[0]
    try:
        return page_lines(page, 0)
    finally:
        page.close()


def test_the_locator_still_spans_the_deleted_hyphen(tmp_path):
    """D9: the span stays a correct *locator* -- it covers the characters that are on
    the page, including the hyphen that is no longer in `text`."""
    path = tmp_path / "broken.pdf"
    path.write_bytes(tiny_pdf(BROKEN_WORD))

    (line,) = page_lines_of(path)

    assert "appropriations decision" in line.text
    assert line.end - line.start == len(line.text) + 1


# --------------------------------------------------------------------------- #
# nothing leaks, everything is counted
# --------------------------------------------------------------------------- #


def test_no_marker_ever_reaches_the_caller(tmp_path):
    """U+FFFE is carried through `page_lines` on purpose. If a path ever forgets to
    drop it, the caller gets a replacement box in the middle of a word and stores it."""
    for name, content in (("broken", BROKEN_WORD), ("compound", GENUINE_COMPOUND)):
        path = tmp_path / f"{name}.pdf"
        path.write_bytes(tiny_pdf(content))
        joined = "".join(piece.text for piece in diceo.chunk(path))
        assert not any(mark in joined for mark in MARKERS), repr(joined)


def test_both_outcomes_are_counted(tmp_path):
    """Rule 3. `hyphens_kept` is the residual defect -- words this reader knowingly
    left broken -- and a number nobody reports is a number nobody fixes."""
    path = tmp_path / "mixed.pdf"
    path.write_bytes(tiny_pdf(BROKEN_WORD + GENUINE_COMPOUND))

    stats: dict = {}
    list(blocks(path, diagnostics=stats))

    assert stats["hyphens_rejoined"] == 1
    assert stats["hyphens_kept"] == 1


def test_a_clean_page_is_unchanged_and_counts_nothing(tmp_path):
    path = tmp_path / "clean.pdf"
    path.write_bytes(tiny_pdf(["Nothing is hyphenated on this page at all."]))

    stats: dict = {}
    text = " ".join(block.text for block in blocks(path, diagnostics=stats))

    assert text == "Nothing is hyphenated on this page at all."
    assert (stats["hyphens_rejoined"], stats["hyphens_kept"]) == (0, 0)


# --------------------------------------------------------------------------- #
# the pre-existing path, which this change must not disturb
# --------------------------------------------------------------------------- #


def _line(text: str, top: float) -> Line:
    return Line(
        text=text,
        page=0,
        start=0,
        end=len(text),
        size=10.0,
        bold=False,
        bbox=(72.0, top - 10.0, 300.0, top),
    )


def test_de_hyphenation_between_two_lines_still_works():
    """`_Paragraph.flush` handles the case where PDFium *does* split the line. It is
    rare against PDFium (it marks the break instead) but it is the only thing that
    covers a word broken across a page, so it stays."""
    para = _Paragraph()
    para.add(_line("The appropri-", 700.0))
    para.add(_line("ations bill", 688.0))

    block = para.flush()

    assert block is not None and block.text == "The appropriations bill"


def test_a_double_hyphen_between_two_lines_is_still_not_a_break():
    para = _Paragraph()
    para.add(_line("an em dash--", 700.0))
    para.add(_line("like this", 688.0))

    block = para.flush()

    assert block is not None and block.text == "an em dash-- like this"
