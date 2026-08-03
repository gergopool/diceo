"""The spreadsheets that are not zipped XML: ``.xls``, ``.xlsb``, ``.ods``.

Three formats, one adapter, because ``iter_sheet_chunks`` takes a foreign row
iterator -- so all the header-carrying, profile-detecting chunking logic that
D6 settled for ``.xlsx`` applies to a 1998 workbook
without a line of it being duplicated.

The reader is ``python-calamine`` (MIT, Rust), which was already a declared
dependency and, until now, only used by the benchmark harness. It is the right
tool for these three: BIFF8 (``.xls``) and the binary ``.xlsb`` are record
formats whose Python implementations are otherwise slow, unmaintained, or both.

**One honest limitation, stated in the diagnostics rather than in a footnote.**
calamine materialises a whole sheet to hand it over -- there is no row-streaming
entry point in the Python binding -- so these three formats are the one place
diceo breaks rule 2 (stream, never materialise). For ``.xls`` that is bounded by
the format itself: BIFF8 cannot exceed 65,536 rows x 256 columns, so the worst
case is fixed and small. For ``.xlsb`` and ``.ods`` it is not bounded, so
``Limits.max_rows`` is enforced and a note records that peak memory scaled with
the sheet. A caller indexing untrusted workbooks should set ``max_rows``.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO
from xml.parsers import expat

from diceo._zip_parts import NOT_OUR_VERDICT as _NOT_OUR_VERDICT
from diceo.errors import CorruptDocument, EncryptedDocument
from diceo.plaintext import count_controls
from diceo.sheets import Row, SheetDiagnostics

# BIFF8 caps a sheet at 65,536 rows, which is why .xls needs no memory guard of its
# own: the format bounds it. Stated as a comment because nothing reads it as a value.

#: How much of ``content.xml`` the termination check holds at once. The part is fed
#: to the parser in blocks rather than read, so the check costs a fixed 64 KB on a
#: 400 MB sheet (rule 2).
_CHECK_BLOCK = 1 << 16

#: expat verdicts that are about the *encoding* rather than about the structure. A
#: part declaring `windows-1250` is one expat refuses to parse at all and one
#: quick-xml may well read, so it is not evidence of the shape this guard exists to
#: catch and must not be turned into a refusal -- the whole file would leave the
#: caller's index over a check that never even ran.
_ENCODING_VERDICTS = frozenset(
    {
        expat.errors.codes[expat.errors.XML_ERROR_UNKNOWN_ENCODING],
        expat.errors.codes[expat.errors.XML_ERROR_INCORRECT_ENCODING],
    }
)


#: ``<table:table …>``, ``</table:table>`` and ``<table:table/>`` -- and deliberately
#: *not* ``<table:table-row>``, which is why the name is followed by a required
#: delimiter. The prefix is captured rather than fixed: the namespace may be bound to
#: any prefix, and the loop this detects is in the reader rather than in the name.
_TABLE_TOKEN = re.compile(
    rb"<(?P<close>/?)(?P<name>(?:[A-Za-z0-9_.\-]+:)?table)(?P<rest>|\s[^<>]*|/)>"
)
#: Bytes carried between blocks so a tag straddling a block boundary is still seen
#: whole. The longest token of interest is a start tag with attributes, and only its
#: first ~40 bytes matter; 512 is slack, and it is a constant we chose rather than
#: one the file chose.
_TOKEN_TAIL = 512


def _refuse_doctype(name: str, system: str | None, public: str | None, internal: bool) -> None:
    """Expat's ``StartDoctypeDeclHandler``. See :func:`_parse_content`.

    A :class:`CorruptDocument` rather than an ``ExpatError``, deliberately. An
    ``ExpatError`` raised here would be caught by the encoding triage below, which
    reads ``exc.code`` -- an attribute only pyexpat's own errors carry -- and a
    hand-built one leaks an ``AttributeError`` through a reader whose contract is
    that every failure is a `DiceoError`. Raising the verdict directly also says
    the right thing: unlike a malformed part, a declaration is not damage to read
    leniently past.
    """
    raise CorruptDocument(
        "content.xml declares a document type -- no ODF producer writes one, and its "
        "entity expansion is unbounded by anything diceo can measure"
    )


def _parse_content(source: str | Path | IO[bytes]) -> None:
    """Feed ``content.xml`` to expat in blocks; raise whatever it raises.

    No handlers are registered, which is the point: expat is then a C scan with no
    per-element Python call, and "nothing is still open at EOF" is exactly what
    well-formedness means. Blocks rather than a read, so a 400 MB part costs 64 KB
    (rule 2).
    """
    parser = expat.ParserCreate()
    # The one handler worth the per-document cost of setting it, and it fires at most
    # once per part: a document type declaration is where entity amplification comes
    # from, and expat's own limiter only bounds it as a *ratio* of input (see
    # `_zip_parts.declares_dtd`). No ODF producer writes one. Raising here refuses the
    # file as malformed, which is what the caller of this function already expects.
    parser.StartDoctypeDeclHandler = _refuse_doctype
    if not isinstance(source, str | Path):
        source.seek(0)
    try:
        with zipfile.ZipFile(source) as archive, archive.open("content.xml") as stream:
            while True:
                block = stream.read(_CHECK_BLOCK)
                if not block:
                    break
                parser.Parse(block, False)
            parser.Parse(b"", True)
    finally:
        if not isinstance(source, str | Path):
            source.seek(0)


def _table_left_open(source: str | Path | IO[bytes]) -> bool:
    """Does a table start with no ``</table:table>`` anywhere after it?

    Counted as **tokens** and **per spelling**, not as nesting, because that is what
    the reader on the other side sees: quick-xml does not check that an end tag
    matches its start, so calamine's table loop leaves on a ``</table:table>`` event
    and hangs only when no such event ever arrives. Both halves of that are load
    bearing. Asking a validating parser instead was the version of this guard that
    refused 237 of 992 damaged-but-readable parts; counting ``</anything:table>`` as
    an end was the version that let 5 of the 14 hangs through, because damaging the
    *prefix* of the closing tag leaves a token that looks like an end and does not
    end calamine's loop.

    Only ever run over a part already proved malformed, so its cost never lands on
    a healthy file and its imprecision (a token inside a comment, say) can only
    change the answer for a document that is broken either way.
    """
    opened: dict[bytes, int] = {}
    leftover = b""
    base = 0
    counted = -1
    try:
        if not isinstance(source, str | Path):
            source.seek(0)
        with zipfile.ZipFile(source) as archive, archive.open("content.xml") as stream:
            while True:
                block = stream.read(_CHECK_BLOCK)
                if not block:
                    break
                window = leftover + block
                for match in _TABLE_TOKEN.finditer(window):
                    where = base + match.start()
                    if where <= counted:
                        continue  # already seen in the previous window's tail
                    counted = where
                    name = match.group("name")
                    if match.group("close"):
                        opened[name] = opened.get(name, 0) - 1
                    elif not match.group("rest").endswith(b"/"):
                        opened[name] = opened.get(name, 0) + 1
                    # `<table:table/>` opens and closes at once: no entry either way.
                keep = min(len(window), _TOKEN_TAIL)
                base += len(window) - keep
                leftover = window[len(window) - keep :]
    except _NOT_OUR_VERDICT:
        return False
    finally:
        if not isinstance(source, str | Path):
            source.seek(0)
    return any(count > 0 for count in opened.values())


def _check_ods_termination(
    source: str | Path | IO[bytes], report: SheetDiagnostics, name: str = ""
) -> None:
    """Refuse an ``.ods`` whose XML leaves a table open; report one that is merely broken.

    calamine's table reader loops on ``read_event`` with no arm for ``Eof``, so a
    ``content.xml`` that runs out while a table is open **spins a core forever**.
    Measured on the fixture in ``tests/test_sheet_termination.py``: of the 992
    single-byte edits to a healthy part, the 14 that damage the final
    ``</table:table>`` hang ``CalamineWorkbook.from_path``, and so does truncating
    the part after any ``</table:table-row>``.

    It hangs *inside the Rust extension*, and that is the whole reason this is a
    guard rather than a bound: no Python executes while it spins, so the
    ``Limits(max_seconds=...)`` check -- which lives between two yielded chunks --
    is never reached again, and a budget that is silently not applied is worse than
    no budget because the caller sized their pipeline around it. The last moment we
    can still act is before the handover.

    Well-formedness is the cheap first question and it is *not* the verdict. Of the
    992 single-byte edits, 241 break the part badly enough that calamine refuses it
    on its own, 237 leave a part it reads perfectly well, and 14 hang it. Refusing
    everything malformed would trade 237 readable documents for those 14, which
    rule 3 does not allow, so a malformed part is only refused when the token scan
    says the loop's precondition is actually met; otherwise it is read and the
    damage is a note, because half a document in the index beats none of it.

    The guard cannot itself be turned into the attack: expat 2.4+ carries its own
    input-amplification limit, so an entity-bomb ``content.xml`` is refused here
    rather than expanded.
    """
    try:
        _parse_content(source)
        return
    except expat.ExpatError as exc:
        if exc.code in _ENCODING_VERDICTS:
            # A part declaring an encoding expat has no decoder for is one quick-xml
            # may well read. Not evidence about structure, so not a verdict.
            return
        broken = exc
    except _NOT_OUR_VERDICT:
        # The check could not run at all -- no ``content.xml``, an archive that will
        # not open, an encrypted or undecodable member. None of that is evidence about
        # the loop, and calamine has a better message for every one, so the file goes
        # on to it unjudged.
        return

    if _table_left_open(source):
        raise CorruptDocument(
            f"content.xml leaves a table open ({broken}) -- reading it would put "
            f"calamine into a loop that no timeout can interrupt, because it spins "
            f"inside the extension where no Python runs",
            source=_name(source, name),
        ) from broken
    report.notes.append(
        f"content.xml is not well-formed XML ({broken}); the reader is lenient and "
        f"read it anyway, so what came out may be missing rows or cells"
    )


def iter_rows(
    source: str | Path | IO[bytes],
    *,
    kind: str,
    diagnostics: SheetDiagnostics | None = None,
    max_rows: int | None = None,
    name: str = "",
) -> Iterator[Row]:
    """Stream a legacy workbook as :class:`~diceo.sheets.Row` values.

    Sheets are yielded contiguously and in workbook order, which is the contract
    ``iter_sheet_chunks`` requires of any row source.
    """
    report = diagnostics if diagnostics is not None else SheetDiagnostics()
    try:
        from python_calamine import CalamineWorkbook
    except ImportError as exc:  # pragma: no cover - a declared dependency
        raise CorruptDocument(
            f"reading {kind} needs python-calamine, which is a required dependency "
            f"but is not importable ({exc})"
        ) from exc

    if kind == "ods":
        # Before the handover, because after it there is no "before" left: see
        # `_check_ods_termination`. .xls and .xlsb are record formats with no
        # equivalent cheap structural check, and neither has been observed to hang.
        _check_ods_termination(source, report, name)

    try:
        if isinstance(source, str | Path):
            book = CalamineWorkbook.from_path(str(source))
        else:
            # `from_filelike` exists, so a caller who has bytes rather than a file
            # is not a second-class citizen here -- every other format accepts both.
            source.seek(0)
            book = CalamineWorkbook.from_filelike(source)
    except Exception as exc:
        # calamine raises its own error types; the message is the useful part, and
        # the two cases a caller must tell apart are locked and broken.
        text = str(exc).lower()
        if "password" in text or "encrypt" in text:
            raise EncryptedDocument(
                f"password-protected workbook ({exc})", source=_name(source, name)
            ) from exc
        raise CorruptDocument(
            f"unreadable {kind} workbook ({exc})", source=_name(source, name)
        ) from exc

    if kind != "xls":
        # Emitted unconditionally, and it no longer recommends `max_rows`. It used
        # to fire only when `max_rows` was None and read "set Limits.max_rows to
        # bound memory on untrusted files" -- which is false, and the note a caller
        # worrying about hostile input is most likely to act on. `to_python()`
        # below materialises the entire sheet before the first row is seen, so
        # `max_rows` bounds what is *emitted* and cannot bound what is *allocated*.
        # Setting it also silenced the warning, so the caller who took the advice
        # was the one who stopped being told.
        report.notes.append(
            f"{kind} is read whole per sheet (no streaming reader exists), so peak "
            f"memory is set by the largest sheet; Limits.max_rows bounds the rows "
            f"emitted, not that allocation"
        )

    # Not `name`: that is the caller's name for the *document*, and rebinding it here
    # would leave any later refusal message calling the file after its last sheet.
    for sheet in book.sheet_names:
        report.sheets += 1
        try:
            grid = book.get_sheet_by_name(sheet).to_python(skip_empty_area=False)
        except Exception as exc:
            # One unreadable sheet must not lose the other twelve (rule 3).
            report.sheets_without_part.append(f"{sheet}: {exc}")
            continue

        emitted = 0
        for index, cells in enumerate(grid, start=1):
            if max_rows is not None and emitted >= max_rows:
                report.truncated.append(("max_rows", max_rows, -1))
                break
            text_cells = ["" if cell is None else _format(cell, report) for cell in cells]
            if not any(cell.strip() for cell in text_cells):
                continue
            emitted += 1
            report.rows += 1
            report.cells += len(text_cells)
            yield Row(sheet=sheet, number=index, cells=text_cells)
        if not emitted:
            report.sheets_without_rows.append(sheet)


def _name(source: str | Path | IO[bytes], given: str = "") -> str:
    """What to call this document in a refusal.

    A stream has no path, so the fallback used to be the literal ``<stream>`` -- and
    ``diceo.chunk(response.content, name="report.xlsx")`` is the documented way to
    read a download, so the caller who most needs the filename in the message was the
    one who could not get it. `given` is the name they supplied.
    """
    if isinstance(source, str | Path):
        return str(source)
    return str(getattr(source, "name", "") or given or "<stream>")


def _format(cell: object, report: SheetDiagnostics | None = None) -> str:
    """Render a calamine cell the way the ``.xlsx`` reader renders the same value.

    Cleaned of control characters for the same reason `unescape_cell` is, and by
    the same set: BIFF8 stores strings as binary, so a ``.xls`` can hold a literal
    NUL where a ``.xlsx`` has to spell it ``_x0000_``. Agreement across backends is
    the rule here, and that has to include what gets removed.

    Agreement across backends matters more than prettiness: a value must produce
    the same chunk text whether it arrived as ``.xls`` or ``.xlsx``, or the same
    workbook saved twice retrieves differently. Integral floats lose the trailing
    ``.0`` for that reason -- calamine parses every numeric cell as a float, and
    ``42.0`` in an index where the user typed ``42`` is a retrieval miss
    (a gate in the research repository holds this to cell-for-cell agreement).
    """
    if isinstance(cell, bool):
        return "TRUE" if cell else "FALSE"
    if isinstance(cell, float):
        if cell.is_integer():
            return str(int(cell))
        return repr(cell)
    cleaned, removed = count_controls(str(cell))
    if removed and report is not None:
        report.control_chars_removed += removed
    return cleaned
