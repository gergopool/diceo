"""`csv.field_size_limit` belongs to the host process, not to us.

The CSV reader needs a ceiling higher than the stdlib's 128 KB, because a cell holding a
pasted document is unusual and not corrupt. The knob for that is process-global, and two
successive attempts at borrowing it politely both failed in ways only measurement showed:

* set at import time and never put back -- the caller's guard gone for the life of the
  process;
* raised around the *generator* -- which holds its scope open across every ``yield``, so
  a caller stepping through the chunks of one CSV measured the limits
  ``{131072, 2147483647}`` between them, and three threads chunking CSVs concurrently
  left ``2147483647`` behind permanently, because each enter saved the already-raised
  value as "the caller's".

Both were reproduced on 2026-08-03 before the fix, and the tests below are those two
repros plus the branches the fix added. The property they defend is narrow and total: a
caller holding a diceo iterator sees their own limit at every point where they get
control, and a document with one enormous cell is still read.
"""

from __future__ import annotations

import csv
import io
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

import diceo
from diceo import delimited

#: Comfortably over the stdlib's 131,072-character default, so a row carrying one of
#: these forces the ceiling up; small enough that a few hundred of them stay fast.
_OVERSIZED = 150_000


@pytest.fixture(autouse=True)
def _restore_the_limit() -> Iterator[None]:
    """No test in this file may be the reason a later one measures the wrong number."""
    before = csv.field_size_limit()
    try:
        yield
    finally:
        csv.field_size_limit(before)


def _plain_csv(rows: int = 400) -> bytes:
    lines = ["region,revenue,units,note"]
    for index in range(rows):
        lines.append(f"EMEA-{index},{1200 + index},{index % 40},note number {index}")
    return ("\n".join(lines) + "\n").encode()


def _csv_with_oversized_cells(cells: int = 3, rows: int = 200) -> bytes:
    """A file whose big cells sit well past the 50-line sniffing sample.

    Deliberately: the sample is parsed under its own short-lived raise, and putting the
    big cell inside it would test that path instead of the metered one.
    """
    lines = ["region,revenue,note"]
    for index in range(rows):
        if index and index % (rows // (cells + 1)) == 0 and index > 60:
            lines.append(f'EMEA-{index},{1200 + index},"{"x" * _OVERSIZED}"')
        else:
            lines.append(f"EMEA-{index},{1200 + index},note number {index}")
    return ("\n".join(lines) + "\n").encode()


def test_the_callers_limit_is_untouched_at_every_yield(tmp_path: Path) -> None:
    """The first repro: every point where the caller gets control, not just the end.

    Asserting only after the iterator is exhausted is what let the generator-wide scope
    look correct for as long as it did.
    """
    path = tmp_path / "plain.csv"
    path.write_bytes(_plain_csv())
    before = csv.field_size_limit()

    observed = set()
    for _chunk in diceo.chunk(path):
        observed.add(csv.field_size_limit())

    assert observed == {before}, f"caller saw {sorted(observed)}, their own is {before}"
    assert csv.field_size_limit() == before


def test_the_callers_limit_is_untouched_across_a_cell_that_needs_the_ceiling(
    tmp_path: Path,
) -> None:
    """Same property on the file that actually makes us raise it.

    Without this the test above passes trivially, because an ordinary file never takes
    the raising branch at all.
    """
    path = tmp_path / "huge_cells.csv"
    path.write_bytes(_csv_with_oversized_cells())
    before = csv.field_size_limit()

    rows = delimited.iter_rows(io.BytesIO(path.read_bytes()))
    assert {csv.field_size_limit() for _row in rows} == {before}

    observed = {csv.field_size_limit() for _chunk in diceo.chunk(path)}
    assert observed == {before}
    assert csv.field_size_limit() == before


def test_concurrent_readers_leave_the_limit_exactly_as_they_found_it(tmp_path: Path) -> None:
    """The second repro. Threads are a supported way to chunk a CSV -- ``PDFIUM_LOCK``
    serialises PDFs and nothing else -- so two readers overlapping is ordinary use.

    The files carry oversized cells so the readers really are nested inside each other's
    raised windows; on plain files the fix is never exercised and the old code passed.
    """
    payload = _csv_with_oversized_cells()
    paths = []
    for index in range(3):
        path = tmp_path / f"worker{index}.csv"
        path.write_bytes(payload)
        paths.append(path)

    before = csv.field_size_limit()
    start = threading.Barrier(len(paths))
    failures: list[BaseException] = []

    def read(path: Path) -> None:
        try:
            start.wait()
            for _ in range(15):
                for _chunk in diceo.chunk(path):
                    pass
        except BaseException as error:  # noqa: BLE001 - re-raised on the main thread
            failures.append(error)

    workers = [threading.Thread(target=read, args=(path,)) for path in paths]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert not failures, failures
    assert csv.field_size_limit() == before


def test_a_cell_over_the_ceiling_is_read_rather_than_raising(tmp_path: Path) -> None:
    """Scoping the raise must not amount to removing it. A cell holding a pasted
    contract is a cell, and dropping the document -- or raising `_csv.Error` at the
    caller -- would be rule 3's failure wearing a fix's clothes."""
    cell = "y" * _OVERSIZED
    path = tmp_path / "pasted.csv"
    path.write_bytes(f'name,note\r\nrow,"{cell}"\r\n'.encode())

    text = "".join(chunk.text for chunk in diceo.chunk(path))
    assert cell[:2000] in text


def test_a_nested_hold_restores_the_original_and_not_the_raised_value(tmp_path: Path) -> None:
    """The mechanism the thread test can only observe by luck.

    The old code saved `csv.field_size_limit()` on every enter, so the inner one saved
    2147483647 and handed *that* back as the caller's value.
    """
    before = csv.field_size_limit()

    delimited._enter_raised()
    delimited._enter_raised()
    delimited._leave_raised()
    assert csv.field_size_limit() == delimited._FIELD_LIMIT, "released while still held"

    delimited._leave_raised()
    assert csv.field_size_limit() == before


def test_an_ordinary_file_never_writes_the_process_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strongest form of the property, and the one the metering buys: for a file
    whose cells are cells, the knob is read and never written, so there is no window at
    all -- not even one another thread could look into."""
    path = tmp_path / "plain.csv"
    path.write_bytes(_plain_csv())

    real = csv.field_size_limit
    writes: list[int] = []

    def watched(*value: int) -> int:
        if value:
            writes.append(value[0])
        return real(*value)

    monkeypatch.setattr(csv, "field_size_limit", watched)
    assert list(diceo.chunk(path))
    assert writes == []


def test_a_caller_who_set_a_higher_ceiling_keeps_it(tmp_path: Path) -> None:
    """Reaching *down* into the host is the same trespass as reaching up. A caller who
    has already allowed more than we would is left alone."""
    csv.field_size_limit(delimited._FIELD_LIMIT + 1)
    theirs = csv.field_size_limit()
    path = tmp_path / "huge_cells.csv"
    path.write_bytes(_csv_with_oversized_cells())

    observed = {csv.field_size_limit() for _chunk in diceo.chunk(path)}
    assert observed == {theirs}
    assert csv.field_size_limit() == theirs


def test_a_source_that_dies_mid_row_still_puts_the_ceiling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one path where the raise can outlive the parse it was taken for: the ceiling
    goes up inside `next(reader)`, and the line source raises before that call returns.
    A network share disappearing mid-file is the ordinary way to get here, and leaking
    on it would be permanent -- there is no later row to hand the value back."""

    def dying_lines(handle, report=None) -> Iterator[str]:
        yield "region,note\n"
        for index in range(60):
            yield f"EMEA-{index},note number {index}\n"
        yield '"' + "z" * _OVERSIZED + "\n"
        raise OSError("the share went away mid-row")

    monkeypatch.setattr(delimited, "text_lines", dying_lines)
    before = csv.field_size_limit()

    with pytest.raises(OSError, match="mid-row"):
        list(delimited.iter_rows(io.BytesIO(b"")))

    assert csv.field_size_limit() == before
