"""The .ods that never comes back, and the guard that is allowed to refuse it.

**One changed byte in a spreadsheet pins a core forever, and
``Limits(max_seconds=...)`` cannot stop it.** Damage any of the fourteen bytes of the
final ``</table:table>`` in an ``.ods``, or truncate ``content.xml`` after any
``</table:table-row>``, and ``CalamineWorkbook.from_path`` never returns:
quick-xml keeps handing calamine's table loop an ``Eof`` event that the loop has no
arm for. It spins *inside the Rust extension*, so no Python executes -- and diceo's
time budget is checked between two yielded chunks, which never arrive. A budget that
is silently not applied is worse than no budget, because the caller sized their
pipeline around it.

Measured here over all 992 single-byte edits of a healthy ``content.xml``, each in a
child process with a hard timeout (no benchmark harness involved: the numbers are
counts, not times):

| what calamine does | edits | before | after |
|---|---|---|---|
| reads it | 732 | chunks | chunks, unchanged |
| refuses it | 241 | ``CorruptDocument`` | ``CorruptDocument`` |
| reads it, 0 rows | 5 | **0 chunks, no exception** | ``CorruptDocument`` |
| **never returns** | 14 | **hangs forever** | ``CorruptDocument`` |

The last two rows are the fix. The first row is why the guard is not simply "refuse
anything that is not well-formed XML": that version refused 237 of the 992, including
files quick-xml reads perfectly, and losing 237 readable documents to save 14 is not a
trade rule 3 allows. So a malformed part is refused only when a token scan says a
table is left open -- the loop's actual precondition -- and is otherwise read, with
the damage reported as a note.

Every hostile case here runs in a **child process that can be killed**. A test for a
hang must not call the thing that hangs: if the guard regresses, an in-process call
takes the whole suite with it and CI reports a timeout on a file nobody can name.

Every fixture is built from the standard library, so the file runs on a fresh clone
with no corpus.
"""

from __future__ import annotations

import io
import multiprocessing as mp
import zipfile

import pytest

import diceo
from diceo import Diagnostics, Limits

_OFFICE = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"'
_TABLE = 'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
_TEXT = 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
_MANIFEST = (
    '<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:'
    'opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" '
    'manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
    '<manifest:file-entry manifest:full-path="content.xml" '
    'manifest:media-type="text/xml"/></manifest:manifest>'
)
_ROWS = [["region", "revenue"], ["EMEA", "1200"], ["APAC", "890"]]

#: The closing tag whose fourteen bytes are the whole defect.
_CLOSE = "</table:table>"

#: How long a child gets before the test calls it a hang. Generous: the fixtures are
#: under a kilobyte and the healthy read is thousands of times faster than this, so a
#: child still running at 15 s is looping, not slow.
_BUDGET = 15.0

#: `fork` where it exists, `spawn` where it does not. The child runs a function
#: defined in this module and the point is to be able to kill it; fork gives both for
#: free, and this was `mp.get_context("fork")` unconditionally -- which is a call at
#: import time, so on Windows it did not fail a test, it failed *collection*, and took
#: the other 894 tests with it. `spawn` re-imports this module in the child, which is
#: safe because module level here only builds constants, and `_child` is top-level
#: with picklable arguments. Measured equivalent: 35 passed either way.
_CONTEXT = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn")


def _content(rows: list[list[str]] | None = None) -> str:
    body = "".join(
        "<table:table-row>"
        + "".join(
            f'<table:table-cell office:value-type="string">'
            f"<text:p>{value}</text:p></table:table-cell>"
            for value in cells
        )
        + "</table:table-row>"
        for cells in (rows or _ROWS)
    )
    return (
        f'<?xml version="1.0"?><office:document-content {_OFFICE} {_TABLE} {_TEXT}>'
        f"<office:body><office:spreadsheet>"
        f'<table:table table:name="Sheet1">{body}</table:table>'
        f"</office:spreadsheet></office:body></office:document-content>"
    )


def _ods(content: str) -> bytes:
    """An OpenDocument spreadsheet, ``mimetype`` stored first as the spec requires."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        stored = zipfile.ZipInfo("mimetype")
        stored.compress_type = zipfile.ZIP_STORED
        archive.writestr(stored, b"application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("META-INF/manifest.xml", _MANIFEST)
        archive.writestr("content.xml", content)
    return buffer.getvalue()


def _xlsx(body: str | None = None) -> bytes:
    """A minimal .xlsx. ``body`` replaces the worksheet's content when given.

    Carries a ``styles.xml`` whose second format is builtin 14 (a date), because the
    reader only looks at a cell's ``s`` attribute when the workbook declares a date
    format anywhere -- so a fixture without one cannot reach that code at all.
    """
    sheet_ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    rows = "".join(
        f'<row r="{n}">'
        + "".join(
            f'<c r="{chr(65 + i)}{n}" t="inlineStr"><is><t>{value}</t></is></c>'
            for i, value in enumerate(cells)
        )
        + "</row>"
        for n, cells in enumerate(_ROWS, start=1)
    )
    parts = {
        "xl/styles.xml": f'<?xml version="1.0"?><styleSheet {sheet_ns}><cellXfs '
        f'count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>',
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.'
        'openxmlformats.org/package/2006/content-types"><Default Extension="xml" '
        'ContentType="application/xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook {sheet_ns} {rel_ns}>'
        f'<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://'
        'schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?><worksheet {sheet_ns}>'
        f'<dimension ref="A1:B3"/>{body or f"<sheetData>{rows}</sheetData>"}</worksheet>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _child(payload: bytes | str, name: str, seconds: float | None, queue: object) -> None:
    limits = Limits(max_seconds=seconds) if seconds is not None else Limits()
    source = payload if isinstance(payload, str) else io.BytesIO(payload)
    try:
        pieces = list(diceo.chunk(source, name=name, limits=limits))
        queue.put(("chunks", str(len(pieces))))  # type: ignore[attr-defined]
    except diceo.DiceoError as exc:
        queue.put((type(exc).__name__, str(exc)))  # type: ignore[attr-defined]
    except BaseException as exc:  # noqa: BLE001 - a leak is the failure we test for
        queue.put(("leaked", f"{type(exc).__name__}: {exc}"))  # type: ignore[attr-defined]


def _outcome(payload: bytes | str, name: str, seconds: float | None = None) -> tuple[str, str]:
    """``(verdict, detail)`` from a child process that is killed if it does not answer.

    ``payload`` is the document's bytes, or a path to it. ``verdict`` is the exception
    class name, or ``"chunks"``. A child still alive at the budget fails the test *as
    a hang*, which is the only way this suite can say "the loop is back" instead of
    stopping.
    """
    queue = _CONTEXT.Queue()
    process = _CONTEXT.Process(target=_child, args=(payload, name, seconds, queue))
    process.start()
    process.join(_BUDGET)
    if process.is_alive():
        process.kill()
        process.join()
        pytest.fail(
            f"{name}: still running after {_BUDGET}s. The reader is looping again -- "
            f"see _check_ods_termination in src/diceo/legacy_sheets.py"
        )
    assert not queue.empty(), f"{name}: the child died without answering"
    verdict, detail = queue.get()
    assert verdict != "leaked", (
        f"{name}: leaked {detail}. Every failure must be a DiceoError -- "
        f"see src/diceo/errors.py"
    )
    return verdict, detail


# --------------------------------------------------------------------------- #
# 1. the hang itself
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("offset", range(len(_CLOSE)))
def test_every_byte_of_the_closing_table_tag_is_refused_rather_than_looped_on(
    offset: int,
) -> None:
    """All fourteen. Each one of these hung `from_path` forever before the guard.

    Parametrised rather than looped so a regression names the byte: the failure modes
    are not the same underneath -- damaging the ``<`` deletes the token, damaging the
    ``/`` turns it into a *start* tag, and damaging the prefix leaves something that
    looks like an end tag to us and is not one to calamine.
    """
    healthy = _content()
    at = healthy.rindex(_CLOSE) + offset
    broken = healthy[:at] + "X" + healthy[at + 1 :]
    assert len(broken) == len(healthy)
    verdict, detail = _outcome(_ods(broken), "one-byte.ods")
    assert verdict == "CorruptDocument", detail
    assert "table" in detail


def test_the_refusal_names_the_file(tmp_path: object) -> None:
    """A quarantine handler logs the message and nothing else. It has to say which.

    Both ways in, because they used to differ. A crawl passes a path and always got the
    name; a caller who hands over bytes -- ``diceo.chunk(response.content,
    name="report.xlsx")``, which the public docstring gives as *the* way to read a
    download -- got the literal ``<stream>``, because this reader was handed the
    handle and never the caller's ``name=``. The one who had to say what the file was
    called was the one who could not.
    """
    broken = _ods(_content().replace(_CLOSE, "<Xtable:table>"))
    path = tmp_path / "quarterly-report.ods"  # type: ignore[operator]
    path.write_bytes(broken)

    verdict, detail = _outcome(str(path), "quarterly-report.ods")
    assert verdict == "CorruptDocument", detail
    assert "quarterly-report.ods" in detail

    verdict, detail = _outcome(broken, "downloaded-report.ods")
    assert verdict == "CorruptDocument", detail
    assert "downloaded-report.ods" in detail, "the name the caller supplied, not <stream>"
    assert "<stream>" not in detail


@pytest.mark.parametrize("cut", ["after-a-row", "after-the-table-start"])
def test_a_part_that_simply_stops_inside_the_table_is_refused(cut: str) -> None:
    """Truncation, which is what a failed download and a full disk both produce."""
    healthy = _content()
    at = (
        healthy.index("</table:table-row>") + len("</table:table-row>")
        if cut == "after-a-row"
        else healthy.index("<table:table-row>")
    )
    verdict, detail = _outcome(_ods(healthy[:at]), f"truncated-{cut}.ods")
    assert verdict == "CorruptDocument", detail


def test_the_time_budget_is_not_what_saves_us() -> None:
    """`max_seconds` is checked between two yielded chunks, and none ever arrive.

    The budget is *silently* not applied, which is the part that makes this worse
    than having no budget at all: a caller who sets one has sized their pool around
    a worker that always comes back. So the file must be refused whatever the
    budget says -- including a generous one, which is the setting that used to hang.
    """
    broken = _content().replace(_CLOSE, "<Xtable:table>")
    verdict, detail = _outcome(_ods(broken), "budgeted.ods", seconds=60.0)
    assert verdict == "CorruptDocument", detail


def test_a_table_that_never_opens_is_refused_rather_than_silently_empty() -> None:
    """The other five: damage the *start* tag and calamine returns zero rows.

    No exception, no diagnostic, ``lost_data`` False -- a whole document absent from
    the caller's index with nothing anywhere saying so, which rule 3 calls the worst
    failure mode in this domain. It is the same structural fault as the hang and is
    refused by the same check.
    """
    broken = _content().replace('<table:table table:name="Sheet1">', "<Xable:table>")
    verdict, detail = _outcome(_ods(broken), "no-table.ods")
    assert verdict == "CorruptDocument", detail


def test_the_scan_survives_a_part_larger_than_one_read_block() -> None:
    """The token scan streams in 64 KB blocks, so a tag can straddle a boundary.

    A fixture with 6,000 rows is several blocks long, and the check has to carry
    enough of each block into the next to still see a tag that was cut in half.
    """
    big = _content([[f"row{n}", str(n)] for n in range(6_000)])
    assert len(big) > 3 * (1 << 16)
    at = big.rindex("</table:table-row>") + len("</table:table-row>")
    verdict, detail = _outcome(_ods(big[:at]), "big-truncated.ods")
    assert verdict == "CorruptDocument", detail


def test_an_entity_bomb_is_refused_rather_than_expanded() -> None:
    """The guard must not become the denial of service it prevents.

    Adding an XML parse to the front of a reader is exactly how a billion-laughs
    file gets a second chance, so this case is pinned: expat 2.4+ enforces its own
    input-amplification limit, and the part is rejected instead of expanded.
    """
    entities = "".join(
        f'<!ENTITY {chr(98 + n)} "' + f"&{chr(97 + n)};" * 10 + '">' for n in range(7)
    )
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE office:document-content ['
        '<!ENTITY a "aaaaaaaaaa">'
        + entities
        + "]>"
        + _content()
        .split("?>", 1)[1]
        .replace("<text:p>region</text:p>", "<text:p>&h;</text:p>")
    )
    verdict, detail = _outcome(_ods(bomb), "laughs.ods")
    assert verdict == "CorruptDocument", detail
    # Refused by the doctype handler now, on the declaration itself, before a single
    # entity is expanded -- see `_refuse_doctype`. It used to reach calamine, which
    # does not expand entities either; relying on that left the amplification bounded
    # only by expat's *ratio* limiter, which padding defeats. What is pinned either
    # way is that nothing expanded 10^7 characters out of 800 bytes.
    assert "leaves a table open" not in detail
    assert "declares a document type" in detail


# --------------------------------------------------------------------------- #
# 2. the controls: what the guard must never do
# --------------------------------------------------------------------------- #


def test_an_ordinary_ods_is_untouched() -> None:
    """The control. A guard that fires on healthy input is noise within a day."""
    report = Diagnostics()
    pieces = list(
        diceo.chunk(io.BytesIO(_ods(_content())), name="ordinary.ods", diagnostics=report)
    )
    assert [piece.text for piece in pieces]
    assert any("EMEA" in piece.text and "1200" in piece.text for piece in pieces)
    assert not [note for note in report.notes if "content.xml" in note]
    assert not report.lost_data


def test_an_ordinary_ods_read_from_a_path_is_untouched(tmp_path: object) -> None:
    """And from a path, because the check has to rewind a stream and not a file."""
    path = tmp_path / "ordinary.ods"  # type: ignore[operator]
    path.write_bytes(_ods(_content()))
    report = Diagnostics()
    pieces = list(diceo.chunk(path, diagnostics=report))
    assert any("APAC" in piece.text for piece in pieces)
    assert not [note for note in report.notes if "content.xml" in note]


def test_an_ordinary_xlsx_never_reaches_the_guard() -> None:
    """The .xlsx path has its own reader and must be unchanged by any of this."""
    report = Diagnostics()
    pieces = list(diceo.chunk(io.BytesIO(_xlsx()), name="ordinary.xlsx", diagnostics=report))
    assert any("EMEA" in piece.text for piece in pieces)
    assert not [note for note in report.notes if "content.xml" in note]


def test_damage_the_reader_can_absorb_is_read_and_reported_not_refused() -> None:
    """quick-xml does not check that an end tag matches its start, so a damaged
    *root* name costs it nothing -- and refusing that file would lose a document we
    can still read. It is read, and the damage is a note (rule 3: visible, not fatal).
    """
    damaged = _content().replace("<office:document-content", "<Xffice:document-content", 1)
    report = Diagnostics()
    pieces = list(
        diceo.chunk(io.BytesIO(_ods(damaged)), name="damaged.ods", diagnostics=report)
    )
    assert any("EMEA" in piece.text for piece in pieces)
    assert any("content.xml is not well-formed" in note for note in report.notes)


def test_a_self_closed_empty_table_is_not_mistaken_for_an_open_one() -> None:
    """``<table:table/>`` opens and closes at once, and the token scan has to know it.

    Reached only through a part that is malformed for some *other* reason, because
    that is the only way the scan runs at all -- which is the point: it never sees a
    healthy file.
    """
    empty = _content().replace(
        '<table:table table:name="Sheet1">', '<table:table table:name="Sheet1"/>'
    )
    empty = empty.replace(_CLOSE, "")
    empty = empty.replace("<office:document-content", "<Xffice:document-content", 1)
    verdict, detail = _outcome(_ods(empty), "self-closed.ods")
    assert verdict == "chunks", detail


def test_an_ods_with_no_content_part_still_gets_calamines_own_message() -> None:
    """The guard has no verdict on an archive it cannot open, and must not invent one.

    calamine's message for a workbook with no ``content.xml`` is the better one, so
    the file goes to it unjudged rather than being refused by a checker that never ran.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        stored = zipfile.ZipInfo("mimetype")
        stored.compress_type = zipfile.ZIP_STORED
        archive.writestr(stored, b"application/vnd.oasis.opendocument.spreadsheet")
        archive.writestr("META-INF/manifest.xml", _MANIFEST)
    verdict, detail = _outcome(buffer.getvalue(), "no-content.ods")
    assert verdict == "CorruptDocument", detail
    assert "leaves a table open" not in detail


# --------------------------------------------------------------------------- #
# 3. the same promise for the .xlsx reader: an attribute must not delete the sheet
# --------------------------------------------------------------------------- #
#
# The other half of a loop that ends on the document's terms. Three `int()` calls in
# the row loop took their argument straight from an attribute -- a row's `r`, a
# cell's `s`, a merge range's row numbers -- and a `ValueError` from any of them
# escaped the reader mid-stream. It reached the caller as
# `reader_failed_after_1_chunks: ValueError: ... use sys.set_int_max_str_digits()`,
# which is advice about CPython's internals for a document they cannot open, and it
# cost them every row after the offending one. `lost_data` was True, which is the one
# mercy: the loss was at least visible.

_HUGE = "9" * 5000

_BAD_ATTRIBUTES = {
    # `r` is a row number, until a producer writes something else there.
    "lettered row number": '<row r="abc">'
    '<c r="A99" t="inlineStr"><is><t>AFTER</t></is></c></row>',
    # More digits than CPython will convert from a string (4,300 since 3.11).
    "5000-digit row number": f'<row r="{_HUGE}">'
    f'<c r="A99" t="inlineStr"><is><t>AFTER</t></is></c></row>',
    # `s` indexes the style table, and the reader only reads it to find dates.
    "lettered style index": '<row r="99"><c r="A99" s="zz"><v>45292</v></c></row>',
    "5000-digit style index": f'<row r="99"><c r="A99" s="{_HUGE}"><v>45292</v></c></row>',
}


def _rows_and_notes(body: str) -> tuple[list[str], list[str], bool]:
    report = Diagnostics()
    pieces = list(diceo.chunk(io.BytesIO(_xlsx(body)), name="attrs.xlsx", diagnostics=report))
    return [piece.text for piece in pieces], list(report.notes), report.lost_data


@pytest.mark.parametrize("case", sorted(_BAD_ATTRIBUTES))
def test_an_unreadable_attribute_does_not_delete_the_rows_after_it(case: str) -> None:
    """Every row still arrives, and the attribute becomes a count instead of a stop."""
    healthy = "".join(
        f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>value{n}</t></is></c></row>'
        for n in range(1, 40)
    )
    body_xml = f"<sheetData>{healthy}{_BAD_ATTRIBUTES[case]}</sheetData>"
    texts, notes, lost = _rows_and_notes(body_xml)
    body = "\n".join(texts)
    assert "value39" in body, "the rows before the bad attribute"
    assert "AFTER" in body or "45292" in body, "and the row carrying it"
    assert any("malformed_attributes=1" in note for note in notes), notes
    assert not lost


def test_a_merge_range_with_an_unreadable_row_number_does_not_stop_the_sheet() -> None:
    """`<mergeCells>` is metadata, read on the way past for a count -- and it arrives
    *after* every row, so a `ValueError` there discarded whatever group was pending.
    A decorative element deleting real content is the worst version of this defect.
    """
    healthy = "".join(
        f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>value{n}</t></is></c></row>'
        for n in range(1, 40)
    )
    texts, _, lost = _rows_and_notes(
        f"<sheetData>{healthy}</sheetData>"
        f'<mergeCells count="1"><mergeCell ref="A1:B{_HUGE}"/></mergeCells>'
    )
    assert "value39" in "\n".join(texts)
    assert not lost


def _workbook_of_sheets(names: list[str], parts: list[str]) -> bytes:
    """A workbook whose sheets point at ``parts`` -- the same target twice if asked.

    ``parts`` is one relationship target per sheet, so passing the same one twice is
    how a 5 KB file asks for a worksheet to be read twice.
    """
    sheet_ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel_ns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    declared = "".join(
        f'<sheet name="{name}" sheetId="{n}" r:id="rId{n}"/>'
        for n, name in enumerate(names, start=1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="{target}"/>'
        for n, target in enumerate(parts, start=1)
    )
    members = {
        "[Content_Types].xml": '<?xml version="1.0"?><Types xmlns="http://schemas.'
        'openxmlformats.org/package/2006/content-types"><Default Extension="xml" '
        'ContentType="application/xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
        'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook {sheet_ns} {rel_ns}>'
        f"<sheets>{declared}</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns="http://'
        f'schemas.openxmlformats.org/package/2006/relationships">{relationships}'
        "</Relationships>",
    }
    for target in set(parts):
        stem = target.rsplit("/", 1)[-1].removesuffix(".xml")
        rows = "".join(
            f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>{stem}-value{n}</t></is></c></row>'
            for n in range(1, 21)
        )
        members[f"xl/{target}"] = (
            f'<?xml version="1.0"?><worksheet {sheet_ns}>'
            f"<sheetData>{rows}</sheetData></worksheet>"
        )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for member, payload in members.items():
            archive.writestr(member, payload)
    return buffer.getvalue()


def test_a_part_named_by_two_hundred_sheets_is_read_once() -> None:
    """How often a worksheet is parsed must not be the document's decision.

    Two hundred ``<sheet>`` elements naming one part made a 5,098-byte file emit
    40,000 rows and 800 chunks, and each element is 47 near-identical bytes, so the
    multiplier is bounded only by the archive's compression ratio -- the zip-bomb
    shape, in a place the bomb guard cannot see because no member is large.
    """
    many = _workbook_of_sheets(
        [f"Sheet{n}" for n in range(1, 201)], ["worksheets/sheet1.xml"] * 200
    )
    one = _workbook_of_sheets(["Sheet1"], ["worksheets/sheet1.xml"])
    report = Diagnostics()
    repeated = list(diceo.chunk(io.BytesIO(many), name="many.xlsx", diagnostics=report))
    single = list(diceo.chunk(io.BytesIO(one), name="one.xlsx"))
    assert len(repeated) == len(single)
    assert any("repeated_sheet_parts=199" in note for note in report.notes), report.notes
    assert "Sheet2" in "".join(report.notes), "the tab names that were dropped"


def test_two_sheets_with_two_parts_are_both_still_read() -> None:
    """The control. Deduplicating by *part* must not deduplicate real sheets."""
    workbook = _workbook_of_sheets(
        ["First", "Second"], ["worksheets/sheet1.xml", "worksheets/sheet2.xml"]
    )
    report = Diagnostics()
    pieces = list(diceo.chunk(io.BytesIO(workbook), name="two.xlsx", diagnostics=report))
    body = "\n".join(piece.text for piece in pieces)
    assert "sheet1-value20" in body and "sheet2-value20" in body
    assert not [note for note in report.notes if "repeated_sheet_parts" in note]
    assert report.sheets == 2


def test_ordinary_attributes_are_read_exactly_as_before() -> None:
    """The control, and it has to be specific: the guards sit on the paths that read
    a date format and a merge range, so both must still work. A date still renders as
    a date rather than as its serial, the merge is still counted, and no file that is
    merely *ordinary* produces a `malformed_attributes` note.
    """
    body = (
        '<sheetData><row r="1">'
        '<c r="A1" t="inlineStr"><is><t>when</t></is></c>'
        '<c r="B1" t="inlineStr"><is><t>what</t></is></c></row>'
        '<row r="2"><c r="A2" s="1"><v>45292</v></c>'
        '<c r="B2" t="inlineStr"><is><t>EMEA</t></is></c></row>'
        '<row r="3"><c r="A3" s="1"><v>45293</v></c>'
        '<c r="B3" t="inlineStr"><is><t>APAC</t></is></c></row>'
        "</sheetData>"
        '<mergeCells count="1"><mergeCell ref="A2:A3"/></mergeCells>'
    )
    texts, notes, lost = _rows_and_notes(body)
    assert "2024-01-01" in "\n".join(texts), "the date must not reach the index as 45292"
    assert not [note for note in notes if "malformed_attributes" in note]
    assert any("vertical_merges=1" in note for note in notes), notes
    assert not lost
