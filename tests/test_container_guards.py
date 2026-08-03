"""What the container says about itself, before we believe any of it.

Four defects found by the experiment 038 (edge case mining) audit and
reproduced here before they were fixed. Two are memory safety and two are identity:

*The bomb guard measured the wrong members.* Its ceilings are sized for parts we
**stream** -- a real government workbook holds a 72 MB ``sheet11.xml`` and must still be
read. But ``word/styles.xml`` is read *whole* to resolve style ids, and a package
declaring 63 MB there passed every test (under 2 GB, one byte under the 64 MB where the
ratio test starts) and took peak RSS from 41 MB to **823 MB** on a 225 KB file. Measured
with ``resource.getrusage`` in a subprocess; after the fix the same file is refused from
the central directory and peak RSS never leaves the import baseline.

*An .ods got no guard at all.* It is a ZIP like the other three, and it was identified
from the uncompressed ``mimetype`` member in the head before the archive was ever opened
-- cheap, and it skipped both the duplicate-part check and the bomb check. The identical
forged member is refused in a .docx and accepted in a .ods.

*A CSV export renamed .xlsx* fell through to ``text``, which loses the header line
repeated into every row group (0.528 against 0.449, experiment 022) *and* says nothing.
The second half is the one that matters: a pipeline quietly renaming exports produces
chunks that look perfectly healthy.

*An MHTML archive named .html* -- "Save as Web Page, complete" -- indexed its own MIME
envelope. Measured on one such file: 1,201 of 1,201 characters in its single chunk were
headers, boundary strings and base64, with the two real sentences buried inside.

Every fixture here is built from the standard library, so the file runs on a fresh clone
with no corpus.
"""

from __future__ import annotations

import io
import struct
import time
import zipfile

import pytest

import diceo
from diceo import CorruptDocument, Diagnostics

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_REL_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types"><Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _rels(target: str) -> str:
    return (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        f'Target="{target}"/></Relationships>'
    )


def _zipped(parts: dict[str, bytes | str], first: tuple[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        if first is not None:
            # ODF requires `mimetype` first and uncompressed; the detector reads it
            # straight out of the head.
            name, payload = first
            archive.writestr(zipfile.ZipInfo(name), payload, compress_type=zipfile.ZIP_STORED)
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _docx(styles_xml: str | None = None) -> bytes:
    body = (
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Quarterly report'
        "</w:t></w:r></w:p><w:p><w:r><w:t>The revenue was 1200 million.</w:t></w:r></w:p>"
    )
    styles = styles_xml or (
        f'<?xml version="1.0"?><w:styles {_W_NS}><w:style w:styleId="Heading1" '
        f'w:type="paragraph"><w:name w:val="heading 1"/></w:style></w:styles>'
    )
    return _zipped(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": _rels("word/document.xml"),
            "word/document.xml": f'<?xml version="1.0"?><w:document {_W_NS}><w:body>'
            f"{body}</w:body></w:document>",
            "word/styles.xml": styles,
            "word/numbering.xml": f'<?xml version="1.0"?><w:numbering {_W_NS}/>',
        }
    )


def _xlsx() -> bytes:
    rows = "".join(
        f'<row r="{n}">'
        + "".join(
            f'<c r="{chr(65 + i)}{n}" t="inlineStr"><is><t>{value}</t></is></c>'
            for i, value in enumerate(cells)
        )
        + "</row>"
        for n, cells in enumerate([["region", "revenue"], ["EMEA", "1200"]], start=1)
    )
    return _zipped(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            "_rels/.rels": _rels("xl/workbook.xml"),
            "xl/workbook.xml": f'<?xml version="1.0"?><workbook {_SHEET_NS} {_REL_NS}>'
            f'<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns='
            '"http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
            'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/styles.xml": f'<?xml version="1.0"?><styleSheet {_SHEET_NS}/>',
            "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet {_SHEET_NS}>'
            f'<dimension ref="A1:B2"/><sheetData>{rows}</sheetData></worksheet>',
        }
    )


def _ods() -> bytes:
    office = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
    table = 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
    text = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    body = "".join(
        "<table:table-row>"
        + "".join(
            f'<table:table-cell office:value-type="string"><text:p>{value}</text:p>'
            f"</table:table-cell>"
            for value in cells
        )
        + "</table:table-row>"
        for cells in [["region", "revenue"], ["EMEA", "1200"], ["APAC", "890"]]
    )
    return _zipped(
        {
            "META-INF/manifest.xml": '<?xml version="1.0"?><manifest:manifest xmlns:'
            'manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type='
            '"application/vnd.oasis.opendocument.spreadsheet"/></manifest:manifest>',
            "content.xml": f'<?xml version="1.0"?><office:document-content {office} '
            f"{table} {text}><office:body><office:spreadsheet>"
            f'<table:table table:name="Sheet1">{body}</table:table>'
            f"</office:spreadsheet></office:body></office:document-content>",
        },
        first=("mimetype", b"application/vnd.oasis.opendocument.spreadsheet"),
    )


def _fat_styles(megabytes: int) -> str:
    """A ``styles.xml`` that declares `megabytes` MB and deflates to nothing.

    An honest bomb rather than a forged one: the bytes really are there, which is
    what makes the RSS measurement in the module docstring meaningful.
    """
    filler = '<w:style w:styleId="S" w:type="paragraph"><w:name w:val="body"/></w:style>'
    return (
        f'<?xml version="1.0"?><w:styles {_W_NS}>'
        + filler * ((megabytes * 1024**2) // len(filler))
        + "</w:styles>"
    )


def _declare_size(raw: bytes, member: str, size: int) -> bytes:
    """Rewrite the uncompressed size a ZIP's central directory *claims* for `member`.

    The guard reads declared sizes and never inflates -- that is the whole point of
    checking before reading -- so forging the claim is the honest way to test it, and
    it keeps a fixture that stands for 3 GB down to a few hundred bytes.
    """
    out = bytearray(raw)
    at = 0
    while True:
        at = out.find(b"PK\x01\x02", at)
        if at < 0:  # pragma: no cover - a fixture bug, not a code path
            raise AssertionError(f"{member} is not in the central directory")
        length = struct.unpack_from("<H", out, at + 28)[0]
        if bytes(out[at + 46 : at + 46 + length]) == member.encode():
            struct.pack_into("<I", out, at + 24, size)
            return bytes(out)
        at += 46


def _refusal(payload: bytes, name: str) -> diceo.DiceoError:
    with pytest.raises(diceo.DiceoError) as caught:
        list(diceo.chunk(io.BytesIO(payload), name=name))
    return caught.value


def _report(payload: bytes, name: str) -> tuple[list, Diagnostics]:
    report = Diagnostics()
    pieces = list(diceo.chunk(io.BytesIO(payload), name=name, diagnostics=report))
    return pieces, report


# --------------------------------------------------------------------------- #
# 1. the parts we read whole, which the ceilings above were never sized for
# --------------------------------------------------------------------------- #


def test_an_oversized_styles_part_is_refused() -> None:
    payload = _docx(styles_xml=_fat_styles(63))
    assert len(payload) < 300_000, "the fixture is not actually a bomb"

    exc = _refusal(payload, "fat.docx")

    assert isinstance(exc, CorruptDocument)
    assert "word/styles.xml" in str(exc), "the message must name the part"
    assert "fat.docx" in str(exc), "and the file it is about"


def test_the_refusal_says_what_to_do_next() -> None:
    message = str(_refusal(_docx(styles_xml=_fat_styles(63)), "fat.docx")).lower()

    assert "convert" in message or "re-save" in message


@pytest.mark.parametrize("part", ["word/styles.xml", "word/numbering.xml", "_rels/.rels"])
def test_every_part_read_whole_is_measured(part: str) -> None:
    """Not only ``styles.xml``. ``numbering.xml`` and the relationship graph are read
    the same way, and the one that is 400 MB is the one nobody thought to check."""
    exc = _refusal(_declare_size(_docx(), part, 400 * 1024**2), "fat.docx")

    assert part in str(exc)


def test_a_spreadsheet_style_part_counts_too() -> None:
    """``xl/styles.xml`` is read whole to find the date formats, so it has the same
    ceiling -- the guard is about how a part is read, not which format it came from."""
    exc = _refusal(_declare_size(_xlsx(), "xl/styles.xml", 400 * 1024**2), "fat.xlsx")

    assert "xl/styles.xml" in str(exc)


def test_a_large_styles_part_within_the_ceiling_still_reads() -> None:
    """The control. The largest ``styles.xml`` in the 137-package fixture corpus is
    750 KB (a 300-page IPBES report); 8 MB is ten times that and must be read."""
    pieces, report = _report(_docx(styles_xml=_fat_styles(8)), "big-styles.docx")

    assert "".join(piece.text for piece in pieces)
    assert not report.lost_data


def test_a_streamed_part_keeps_its_own_much_higher_ceiling() -> None:
    """The control that matters most: a real government workbook holds a 72 MB
    worksheet and a real report a 10.8 MB ``document.xml``. Both are streamed, so the
    16 MB ceiling for eagerly-read parts must not touch them."""
    payload = _declare_size(_docx(), "word/document.xml", 63 * 1024**2)

    pieces, _ = _report(payload, "long.docx")

    assert "Quarterly report" in "".join(piece.text for piece in pieces)


def test_an_ordinary_document_is_unaffected() -> None:
    pieces, report = _report(_docx(), "plain.docx")

    assert "1200 million" in "".join(piece.text for piece in pieces)
    assert report.format == "docx"


# --------------------------------------------------------------------------- #
# 2. the one ZIP-based format that had no guard
# --------------------------------------------------------------------------- #


def test_an_ods_declaring_a_giant_member_is_refused() -> None:
    exc = _refusal(_declare_size(_ods(), "content.xml", 3 * 1024**3), "bomb.ods")

    assert isinstance(exc, CorruptDocument)
    assert "content.xml" in str(exc)
    assert "bomb.ods" in str(exc)


def test_the_ods_is_treated_exactly_like_the_docx() -> None:
    """The defect stated as a comparison, which is how it was found: the same forged
    member, refused in one container and indexed in the other."""
    docx = _refusal(_declare_size(_docx(), "word/document.xml", 3 * 1024**3), "bomb.docx")
    ods = _refusal(_declare_size(_ods(), "content.xml", 3 * 1024**3), "bomb.ods")

    assert type(docx) is type(ods)
    assert "per-part ceiling" in str(docx) and "per-part ceiling" in str(ods)


def test_an_ods_naming_a_part_twice_is_refused_as_well() -> None:
    """The duplicate-part check sat behind the same early return, so an .ods could
    also carry two ``content.xml`` members and mean whichever one a reader picked."""
    buffer = io.BytesIO(_ods())
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("content.xml", b"<office:document-content/>")

    exc = _refusal(buffer.getvalue(), "doubled.ods")

    assert "more than once" in str(exc)


def test_a_healthy_ods_still_reads() -> None:
    """The control: routing .ods through the guard must not cost it its detection."""
    pieces, report = _report(_ods(), "sheet.ods")

    assert report.format == "ods"
    assert "EMEA" in "".join(piece.text for piece in pieces)


# --------------------------------------------------------------------------- #
# 2b. ...and the fallback tier that read a member before the guard ran
# --------------------------------------------------------------------------- #
#
# The two paths above answer from metadata: the ODF magic is read out of the head, and
# the OOXML formats are named from the central directory. Neither touches a member. The
# tier below them exists for a package whose `mimetype` is not stored first -- and it
# called `archive.read("mimetype")[:120]`, inflating the whole member and *then*
# slicing, in front of `_check_zip_bomb`. So the one guard whose entire purpose is to
# refuse a member before it costs anything ran after the only unbounded read in the
# package. `read(120)` on the open member instead: `ZipExtFile` bounds the compressed
# bytes it pulls and caps what the decompressor may produce from them, so the cost is
# a property of the call rather than of the file.


def _mimetype_bomb(filler: int) -> bytes:
    """An .ods whose `mimetype` member really does inflate to `filler` bytes.

    Deflated rather than stored, which is what puts it on the fallback path: the
    literal mimetype string is no longer in the file's head, so the detector has to
    open the member to find it. An honest bomb rather than a forged declaration --
    a forged one proves nothing here, because the pre-fix code would refuse it just
    as fast without ever inflating anything.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "mimetype", b"application/vnd.oasis.opendocument.spreadsheet" + b"\0" * filler
        )
        archive.writestr("content.xml", b"<office:document-content/>")
    return buffer.getvalue()


def test_a_mimetype_member_declaring_a_giant_size_is_refused() -> None:
    payload = _mimetype_bomb(70 * 1024**2)
    assert len(payload) < 200_000, "the fixture is not actually a bomb"

    exc = _refusal(payload, "bomb.ods")

    assert isinstance(exc, CorruptDocument)
    assert "mimetype" in str(exc)
    assert "zip bomb" in str(exc)


def test_it_is_refused_without_being_inflated_first() -> None:
    """The half the refusal cannot show. Both before and after the fix this file is
    rejected -- the difference is whether 70 MB passed through the process on the way,
    which on a crafted 3 GB member is the difference between a refusal and the OOM
    the guard exists to prevent. Measured with `tracemalloc`, which sees the
    decompressor's own buffers: 147.5 MB before, 0.1 MB after."""
    import tracemalloc

    payload = _mimetype_bomb(70 * 1024**2)  # built before the meter starts

    tracemalloc.start()
    try:
        _refusal(payload, "bomb.ods")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 8 * 1024**2, f"{peak / 1024**2:.1f} MB allocated to refuse a 70 MB member"


def test_a_deflated_mimetype_is_still_detected() -> None:
    """The control the bounded read has to keep passing: 120 bytes is enough to name
    the format, and this tier is the only thing that can name a package written this
    way at all."""
    buffer = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(_ods())) as source,
        zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        for info in source.infolist():
            archive.writestr(info.filename, source.read(info.filename))

    pieces, report = _report(buffer.getvalue(), "deflated.ods")

    assert report.format == "ods"
    assert "EMEA" in "".join(piece.text for piece in pieces)


# --------------------------------------------------------------------------- #
# 3. the name promises a container and the bytes are text
# --------------------------------------------------------------------------- #

_CSV = b"region,revenue,units\nEMEA,1200,34\nAPAC,890,21\nAMER,1450,55\nLATAM,300,9\n"


def test_a_csv_renamed_xlsx_is_read_as_a_sheet() -> None:
    pieces, report = _report(_CSV, "export.xlsx")

    assert report.format == "csv"
    assert report.rows == 5, "read as rows, not as one paragraph of prose"


def test_the_disagreement_is_reported() -> None:
    """The half a caller cannot recover on their own. The chunks look healthy either
    way; only this note says the export step is writing .xlsx over CSV."""
    _, report = _report(_CSV, "export.xlsx")

    notes = " ".join(report.notes)
    assert "extension_disagrees_with_content" in notes
    assert ".xlsx" in notes


def test_prose_under_a_container_name_stays_text_and_is_still_reported() -> None:
    """The sniff decides the *reader*; the disagreement is reported either way."""
    prose = b"Dear board,\n\nRevenue rose to 1200 million this quarter.\n" * 4

    _, report = _report(prose, "memo.docx")

    assert report.format == "text"
    assert any("extension_disagrees_with_content" in note for note in report.notes)


def test_a_real_workbook_gets_no_note() -> None:
    """The control. A diagnostic that fires on ordinary documents is noise a caller
    learns to ignore, and this one must fire only on a renamed file."""
    _, report = _report(_xlsx(), "quarterly.xlsx")

    assert report.format == "xlsx"
    assert not any("extension_disagrees" in note for note in report.notes)


def test_a_csv_that_is_named_csv_gets_no_note() -> None:
    _, report = _report(_CSV, "export.csv")

    assert report.format == "csv"
    assert not any("extension_disagrees" in note for note in report.notes)


# --------------------------------------------------------------------------- #
# 4. "Save as Web Page, complete" -- a MIME archive wearing an .html name
# --------------------------------------------------------------------------- #

_MHTML = (
    b"From: <Saved by Windows Internet Explorer>\r\n"
    b"Subject: Quarterly results\r\n"
    b"Date: Tue, 29 Jul 2026 10:00:00 +0200\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/related; type="text/html"; boundary="----=_NextPart_01"\r\n'
    b"\r\n"
    b"------=_NextPart_01\r\n"
    b"Content-Location: http://example.com/report.htm\r\n"
    b"Content-Type: text/html; charset=utf-8\r\n\r\n"
    b"<html><body><h1>Quarterly results</h1><p>Revenue rose to 1200 million.</p>"
    b"</body></html>\r\n"
    b"------=_NextPart_01\r\n"
    b"Content-Location: http://example.com/logo.png\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"Content-Type: image/png\r\n\r\n"
    + b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42g==\r\n" * 6
    + b"------=_NextPart_01--\r\n"
)


def test_an_mhtml_archive_is_read_as_mail() -> None:
    pieces, report = _report(_MHTML, "page.html")

    assert report.format == "eml"
    text = "".join(piece.text for piece in pieces)
    assert "Revenue rose to 1200 million." in text


def test_the_mime_envelope_is_no_longer_indexed() -> None:
    """The defect itself: boundary strings, ``Content-Type`` and base64 went into the
    index as prose, and the two real sentences went in with them."""
    pieces, _ = _report(_MHTML, "page.html")

    text = "".join(piece.text for piece in pieces)
    assert "multipart/related" not in text
    assert "NextPart_01" not in text
    assert "iVBORw0KGgo" not in text


def test_the_archive_is_named_in_the_diagnostics() -> None:
    _, report = _report(_MHTML, "page.html")

    assert any("mhtml" in note.lower() for note in report.notes)


def test_the_image_is_reported_rather_than_dropped() -> None:
    """Rule 3: the picture is not indexed, and that is a decision the caller can see."""
    _, report = _report(_MHTML, "page.html")

    assert report.lost_data
    assert any("attachment" in entry for entry in report.truncated)


def test_an_ordinary_html_page_is_untouched() -> None:
    """The control, and the one that guards the published html digest."""
    page = (
        b"<!doctype html><html><head><title>Quarterly</title></head><body>"
        b"<h1>Quarterly results</h1><p>Revenue rose to 1200 million.</p></body></html>"
    )

    pieces, report = _report(page, "page.html")

    assert report.format == "html"
    assert not any("mhtml" in note.lower() for note in report.notes)
    assert "1200 million" in "".join(piece.text for piece in pieces)


def test_a_page_that_merely_mentions_mime_headers_is_still_html() -> None:
    """A tutorial page about MIME is not a MIME archive. The signature is only
    believed in a header block that starts at byte 0."""
    page = (
        b"<!doctype html><html><body><pre>MIME-Version: 1.0\n"
        b"Content-Type: multipart/related; boundary=x</pre>"
        b"<p>How to read a MIME archive.</p></body></html>"
    )

    _, report = _report(page, "tutorial.html")

    assert report.format == "html"


def test_a_plain_email_is_still_a_plain_email() -> None:
    """The other control on the same sniff: single-part mail has no boundary, so the
    new branch must not claim it."""
    mail = (
        b"From: analyst@example.com\r\nTo: board@example.com\r\n"
        b"Subject: Quarterly results\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Revenue rose to 1200 million.\r\n"
    )

    pieces, report = _report(mail, "note.eml")

    assert report.format == "eml"
    assert not any("mhtml" in note.lower() for note in report.notes)
    assert "1200 million" in "".join(piece.text for piece in pieces)


def test_a_text_file_holding_a_pasted_message_is_still_text() -> None:
    """The ordering control. The MHTML sniff runs where the extension promises a page
    and nowhere else -- a `.md` whose first lines are a pasted MIME header is the
    surprise the "extension leads" rule was written to prevent."""
    pasted = (
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/related; boundary="----=_NextPart_01"\r\n\r\n'
        b"Notes on the message above: revenue rose to 1200 million.\r\n"
    )

    _, report = _report(pasted, "notes.md")

    assert report.format == "text"


def test_unclosed_relationships_do_not_backtrack() -> None:
    """A fifth defect, from the 2026-08-03 audit, and the only one here that is a
    denial of service rather than a memory or identity bug.

    ``main_part`` used to read ``_rels/.rels`` with two regexes carrying two
    ``[^>]*`` runs apiece. On ``<Relationship `` repeated with no closing ``>``,
    every start position rescanned the rest of the buffer: 28 KB cost 0.11 s and
    each doubling cost four times the last, so the 16 MB this eager part is allowed
    ran for hours. The package below is **1 KB on disk** and took 13.2 s.

    It backtracked inside the C regex engine, where no Python executes, so
    `Limits` could not reach it -- and it ran before the first block was yielded.
    The bound here is loose on purpose: the fixed path takes ~7 ms, and anything
    that has gone quadratic again blows a whole second long before it trips.
    """
    package = _zipped(
        {
            "[Content_Types].xml": _CONTENT_TYPES,
            # No conventional main part, so the relationship lookup is forced --
            # which is what Word Online's `document2.xml` does in the wild.
            "_rels/.rels": "<Relationship " * 16000,
            "word/document2.xml": f'<?xml version="1.0"?><w:document {_W_NS}><w:body>'
            "<w:p><w:r><w:t>The revenue was 1200 million.</w:t></w:r></w:p>"
            "</w:body></w:document>",
        }
    )

    start = time.perf_counter()
    chunks = list(diceo.chunk(package, name="hostile.docx"))
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"relationship parsing took {elapsed:.1f}s -- backtracking is back"
    # Tier two still finds the document the relationships failed to name.
    assert "1200 million" in "".join(chunk.embed_text for chunk in chunks)


def test_members_under_every_ceiling_still_add_up_to_a_bomb() -> None:
    """The per-member ceilings compose into a hole.

    `MAX_MEMBER_BYTES` (2 GB), `MAX_EAGER_MEMBER_BYTES` (16 MB) and the ratio test
    above `BOMB_MIN_BYTES` (64 MB) are each per-member, so an archive can sit one
    byte under all three in 300 separate parts and still declare **17.6 GB**.
    Measured on a real 18 MB .pptx of that shape: accepted, 2.8M chunks in 33 s,
    peak RSS 311 MB. Bounded memory held; the caller's index would not have.
    """
    from diceo.source import _check_zip_bomb

    def members(count: int, size: int, compressed: int) -> list[zipfile.ZipInfo]:
        out = []
        for index in range(count):
            info = zipfile.ZipInfo(f"ppt/slides/slide{index}.xml")
            info.file_size, info.compress_size = size, compressed
            out.append(info)
        return out

    with pytest.raises(CorruptDocument, match="across its members"):
        _check_zip_bomb(members(300, 63 * 1024**2, 60 * 1024), "bomb.pptx")


def test_a_package_with_many_ordinary_parts_is_not_a_bomb() -> None:
    """The control, and the one that matters more. A 300-slide deck carries
    thousands of parts once rels and media are counted, and refusing it would be a
    false positive -- which this guard's own comment calls worse than a miss."""
    from diceo.source import _check_zip_bomb

    infos = []
    for index in range(2000):
        info = zipfile.ZipInfo(f"ppt/slides/slide{index}.xml")
        info.file_size, info.compress_size = 250 * 1024, 50 * 1024
        infos.append(info)

    _check_zip_bomb(infos, "real.pptx")  # 488 MB across 2,000 parts: a big deck
