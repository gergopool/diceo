"""The reader knows; the caller has to be told. Rule 3, on the four places it failed.

An adversarial audit on 2026-07-31 ran four failure modes against the real fixtures and
cleared diceo on the two that actually killed a comparable package: its PDF text is a
character-for-character subset of the pdfium text layer (0 novel token types in 14,599 on
the 9/11 report), and 150,553 string cells reach ``chunk.text`` with zero omissions. Nobody
is going to write "40% missing" about the text.

What survived was one shape, seven times: **the reader observed the loss and the caller
could not find out.** That is worse than the loss, because the documented handler is::

    report = diceo.Diagnostics()
    pieces = list(diceo.chunk(path, diagnostics=report))
    if report.lost_data:
        log.warning("incomplete: %s", report.as_dict())

and a diagnostic that stays inside the reader turns that handler into decoration. Each test
here is one of those cases, pinned so it cannot go quiet again.
"""

from __future__ import annotations

from pathlib import Path

import diceo
from tests import fixtures


def _run(payload: bytes, name: str, tmp_path: Path, **kwargs) -> tuple[list, diceo.Diagnostics]:
    path = tmp_path / name
    path.write_bytes(payload)
    report = diceo.Diagnostics()
    return list(diceo.chunk(path, diagnostics=report, **kwargs)), report


def _notes(report: diceo.Diagnostics) -> str:
    return " || ".join(report.notes)


def _patch_sheet(book: bytes, old: bytes, new: bytes) -> bytes:
    """Rewrite one member of an .xlsx.

    A plain ``bytes.replace`` on the archive silently does nothing -- the parts are
    deflated -- and a test built that way passes on a fixture it never modified.
    """
    import io
    import zipfile

    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(book)) as source, zipfile.ZipFile(out, "w") as target:
        for member in source.namelist():
            payload = source.read(member)
            if member == "xl/worksheets/sheet1.xml":
                assert old in payload, "the fixture changed; this patch matches nothing"
                payload = payload.replace(old, new)
            target.writestr(member, payload)
    return out.getvalue()


# --------------------------------------------------------------------------- #
# A page that is a picture, wearing a page number
# --------------------------------------------------------------------------- #


def test_a_stamped_image_page_still_counts_as_image_only(tmp_path: Path) -> None:
    """The check used to be `if not found`, and a folio is `found`.

    Seven pages of the IPBES French assessment are tables drawn as bitmaps with a page
    number stamped on each. Two characters of text was enough to make diceo call them
    healthy text pages and report the document complete.
    """
    _, report = _run(fixtures.stamped_image_pdf("38"), "stamped.pdf", tmp_path)
    assert report.pages_image_only == 1
    assert report.needs_ocr, "needs_ocr is the boolean a caller routes OCR on"
    assert report.lost_data


def test_a_blank_page_is_still_not_a_loss(tmp_path: Path) -> None:
    """The distinction the whole check exists for, and the one a looser threshold could
    destroy: a deliberately blank separator page draws no image and is not a loss."""
    _, report = _run(fixtures.blank_pdf(), "blank.pdf", tmp_path)
    assert report.pages_image_only == 0
    assert not report.needs_ocr


def test_a_page_of_real_text_is_never_called_an_image(tmp_path: Path) -> None:
    """The false-positive guard. Raising the threshold from 0 to 40 characters must not
    reach any page that carries actual prose."""
    _, report = _run(fixtures.tiny_pdf(pages=3), "text.pdf", tmp_path)
    assert report.pages_image_only == 0
    assert not report.needs_ocr


# --------------------------------------------------------------------------- #
# A sheet whose columns were never named
# --------------------------------------------------------------------------- #


def _headerless_workbook() -> bytes:
    """Years across the top and codes down the side: no row of labels anywhere.

    This is the ONS CPI shape. The detector is right that there is no header -- what
    was wrong is that it said so only inside a prose line of the summary chunk.
    """
    rows = [["2019", "2020", "2021", "2022"]]
    rows += [[f"{n}.{d}" for d in range(1, 5)] for n in range(100, 140)]
    return fixtures.tiny_xlsx(rows)


def test_a_sheet_with_no_header_says_so(tmp_path: Path) -> None:
    _, report = _run(
        _headerless_workbook(), "codes.xlsx", tmp_path, limits=diceo.Limits(target_chars=200)
    )
    assert "sheets_without_header=" in _notes(report)


def test_chunks_with_no_letter_in_them_are_counted(tmp_path: Path) -> None:
    """A chunk of pure digits cannot be reached by any text query.

    It is not *lost* -- the values are emitted, and rule 3 forbids dropping them -- but a
    caller measuring their index's recall deserves to know how much of it is unreachable.
    On the ONS CPI workbook this is 967 of 2,334 chunks.
    """
    chunks, report = _run(
        _headerless_workbook(), "codes.xlsx", tmp_path, limits=diceo.Limits(target_chars=200)
    )
    letterless = sum(1 for piece in chunks if not any(c.isalpha() for c in piece.text))
    assert letterless
    assert f"chunks_without_letters={letterless}" in _notes(report)


def test_a_labelled_sheet_reports_neither(tmp_path: Path) -> None:
    """The control. A sheet with an ordinary header row must stay quiet, or the notes
    become noise a caller learns to ignore."""
    rows = [["region", "revenue", "year"]]
    rows += [[f"region-{n}", str(n * 10), "2024"] for n in range(40)]
    _, report = _run(fixtures.tiny_xlsx(rows), "clean.xlsx", tmp_path)
    assert "sheets_without_header" not in _notes(report)
    assert "chunks_without_letters" not in _notes(report)


def test_the_over_target_note_does_not_blame_a_header_that_does_not_exist(
    tmp_path: Path,
) -> None:
    """The ONS workbook's single diagnostic explained its overrun with a mechanism that
    had not run. A note that misdirects is worse than no note: a caller who reads it
    stops looking."""
    wide = [[f"{n}.{d}" for d in range(1, 40)] for n in range(100, 130)]
    _, report = _run(
        fixtures.tiny_xlsx(wide), "wide.xlsx", tmp_path, limits=diceo.Limits(target_chars=60)
    )
    notes = _notes(report)
    if "chunks_over_target" in notes:
        assert "header line is re-prefixed" not in notes


# --------------------------------------------------------------------------- #
# An element that never closes eats the rest of the page
# --------------------------------------------------------------------------- #

PARAGRAPHS = b"<p>Paragraph with enough words in it to be a real block of text.</p>" * 80


def test_an_unclosed_style_tag_is_reported(tmp_path: Path) -> None:
    """`_skip` goes up on `<style>` and only comes down on `</style>`, so a missing close
    tag discards every byte after it. A browser does the same -- but a browser is not
    building somebody's index, and the caller got zero chunks, no exception,
    `lost_data` False and an empty `notes`."""
    _, report = _run(
        b"<html><head><style>body{color:red}</head><body>" + PARAGRAPHS + b"</body></html>",
        "unclosed.html",
        tmp_path,
    )
    assert report.lost_data
    assert any("unclosed_element" in entry for entry in report.truncated)


def test_the_mid_body_variant_is_reported_too(tmp_path: Path) -> None:
    """The nastier shape: one chunk comes back, so nothing looks wrong at all -- the
    document merely looks short."""
    chunks, report = _run(
        b"<html><body><p>The first real paragraph.</p><style>x{}"
        + PARAGRAPHS
        + b"</body></html>",
        "midbody.html",
        tmp_path,
    )
    assert chunks, "the readable prefix must still come back (rule 3)"
    assert report.lost_data
    assert any("unclosed_element" in entry for entry in report.truncated)


def test_ordinary_css_and_script_stay_quiet(tmp_path: Path) -> None:
    """The control. Every real page has a `<style>` block; if a closed one reported a
    loss the diagnostic would be noise within a day."""
    _, report = _run(
        b"<html><head><style>body{color:red}</style><script>var x=1;</script></head>"
        b"<body>" + PARAGRAPHS + b"</body></html>",
        "healthy.html",
        tmp_path,
    )
    assert not report.lost_data
    assert not any("unclosed_element" in entry for entry in report.truncated)


# --------------------------------------------------------------------------- #
# Four counters the reader kept where nobody could read them
# --------------------------------------------------------------------------- #


def test_a_declared_dimension_that_disagrees_is_reported(tmp_path: Path) -> None:
    """`dimension` said one thing and the stream delivered another. Neither is corrected
    -- either could be right -- but on the ONS workbook Table 16 declares 516 rows and
    delivers 407, and nothing said so."""
    book = _patch_sheet(
        fixtures.tiny_xlsx([["a", "b"], ["1", "2"]]),
        b'<dimension ref="A1:B2"/>',
        b'<dimension ref="A1:B99"/>',
    )
    _, report = _run(book, "mismatch.xlsx", tmp_path)
    assert "dimension_mismatch" in _notes(report)


def test_error_cells_reach_the_caller(tmp_path: Path) -> None:
    """A `#REF!` cell had a value once. Blanking it is right; blanking it silently is not."""
    book = _patch_sheet(
        fixtures.tiny_xlsx([["region", "revenue"], ["EMEA", "1200"]]),
        b'<c r="B2" t="inlineStr"><is><t>1200</t></is></c>',
        b'<c r="B2" t="e"><v>#REF!</v></c>',
    )
    _, report = _run(book, "errors.xlsx", tmp_path)
    assert report.lost_data
    assert any("error_cells" in entry for entry in report.truncated)


# --------------------------------------------------------------------------- #
# undecodable bytes: the same loss, reported four different ways
# --------------------------------------------------------------------------- #
#
# A byte that matched no encoding is decoded to U+FFFD, and that is a character the
# caller's index will not have. `text_lines` has always called it a truncation, so
# `lost_data` was True for `.txt`. The three other sites that decode -- `_decode_text`
# (the HTML reader's entry point), `iter_text_blocks` and `_decode_part` (mail) -- put
# it in `notes`, where nothing sets `lost_data` and nothing downstream can act on it.
#
# Measured 2026-07-31 on identical bytes under two names::
#
#     x.txt    lost_data=True   truncated=['undecodable_bytes=1000 (...)']
#     x.html   lost_data=False  truncated=[]  notes=['undecodable_bytes=25600']
#
# Mis-declared charsets are not an edge case in HTML; they are the largest single
# issue class in this whole area (657 issues across eight rival trackers).

#: 0x81 is undefined in cp1252 and invalid UTF-8, so it survives no decoder we try
#: and every path has to replace it. `\xff` will not do: cp1252 decodes it happily.
_UNDECODABLE = b"\x81" * 1_000


def test_undecodable_bytes_set_lost_data_whatever_the_file_is_called(tmp_path: Path) -> None:
    """The defect itself: one payload, four names, one verdict."""
    for name in ("x.txt", "x.md", "x.html", "x.csv"):
        _, report = _run(_UNDECODABLE, name, tmp_path)
        assert report.lost_data, f"{name}: 1,000 characters are missing and nothing said so"
        assert any("undecodable_bytes" in entry for entry in report.truncated), (
            f"{name}: {report.truncated}"
        )


def test_a_mail_part_in_a_charset_that_does_not_fit_is_a_loss_too() -> None:
    """`_decode_part`, the fourth site, tested at its own level.

    Reached only when a part declares a charset that is neither empty nor UTF-8 --
    every other label delegates to ``decode()`` -- so the whole-message path cannot
    single it out. ``get_content()`` replaces these bytes silently, which is why this
    reader decodes them itself; reporting the replacements as a *note* was only half
    the step, because ``lost_data`` stayed False for a body that is gone.
    """
    import email
    import email.policy

    from diceo.plaintext import _part_text

    part = email.message_from_bytes(
        b'Content-Type: text/plain; charset="iso-8859-1"\r\n\r\n'
        b"Please approve the budget of 1200 EUR.\r\n" + _UNDECODABLE,
        policy=email.policy.default,
    )
    report = diceo.Diagnostics()

    text = _part_text(part, report)

    assert "budget" in text, "the readable half of the body must survive"
    assert report.lost_data, report.as_dict()
    assert any("undecodable_bytes" in entry for entry in report.truncated), report.truncated


def test_each_undecodable_byte_is_reported_exactly_once(tmp_path: Path) -> None:
    """`iter_text_blocks` re-counted what `text_lines` had already counted over the
    same lines, so a `.txt` file named its bytes once in `truncated` and again in
    `notes`. A caller adding the two up double-counted their own loss."""
    for name in ("x.txt", "x.md"):
        _, report = _run(_UNDECODABLE, name, tmp_path)
        mentions = [
            entry
            for entry in [*report.truncated, *report.notes]
            if "undecodable_bytes" in entry
        ]
        assert len(mentions) == 1, f"{name}: reported {len(mentions)} times -- {mentions}"


def test_a_document_that_really_contains_u_fffd_is_not_called_a_loss(
    tmp_path: Path,
) -> None:
    """The other half of the rule. A strict decode replaced nothing, so a U+FFFD that
    came through it is a character the document genuinely has -- and raising
    `lost_data` for it would cry wolf on the boolean callers branch on."""
    _, report = _run("a � in the text\n".encode(), "genuine.html", tmp_path)
    assert not report.lost_data, report.as_dict()
