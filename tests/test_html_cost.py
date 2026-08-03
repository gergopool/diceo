"""Two shapes a crawler meets where the *cost* was the defect, not the output.

Both were found by measurement rather than by a wrong chunk, and neither raised, logged
or truncated anything -- the document came back looking fine, some minutes and some
gigabytes later.

* **An unterminated tag made the chunked feed quadratic.** `html.parser` cannot emit a
  construct whose end it has not seen, so it keeps the whole of it and rescans it from
  the beginning on every `feed`. Fed in 64 KiB pieces, `<a ` followed by megabytes with
  no `>` therefore costs O(n^2 / 65536): measured at **exactly 4x per doubling -- 0.18s
  at 2 MB, 0.64s at 4 MB, 2.47s at 8 MB, 9.48s at 16 MB**, which is ~25 minutes at the
  200 MB single-page export `iter_html_blocks` advertises and ~10 hours at 1 GB. A
  piece that grows with the buffer makes the rescanning proportional to the document
  instead, and past `_MAX_PENDING_CHARS` the rest of the file is refused and counted.

* **Per-tag state was unbounded.** Nothing pops the ancestor stack until a matching end
  tag arrives, so a page that never closes anything holds one frame per tag -- and a
  `<table>` also saves a nine-field frame with three fresh containers. Measured in a
  clean process: **1,000,000 `<table>` took 6.7 MB of input to 498 MB of resident
  memory, and 1,000,000 `<b>` took 2.9 MB to 126 MB**, both with `truncated` empty. A
  100 MB crawled page of that shape is several GB. `_MAX_OPEN_ELEMENTS` stops the
  tracking rather than the document, and says so in the diagnostics.

Everything asserted here is a *shape*: characters rescanned, elements retained, peak
allocation across a doubling of the input. Wall-clock constants measured on a shared
box are not evidence and would fail for reasons that have nothing to do with this code.

The controls are half the file, and the reason is `_MAX_PENDING_CHARS`: the largest
buffer any of the 197 pages measured ever holds is a **legitimate** 695 KB inline
`<script>` on a nytimes.com front page. A ceiling that refused that would be worse than
the bug it fixes, so a big inline script, a deeply nested page and a page that omits
its optional end tags are all here to prove they still read.

    uv run pytest tests/test_html_cost.py -q
"""

from __future__ import annotations

import io
import tracemalloc

import pytest

from diceo import plaintext
from diceo.plaintext import _MAX_OPEN_ELEMENTS, _MAX_PENDING_CHARS, iter_html_blocks
from diceo.types import Diagnostics

_HEAD = b"<html><body><p>before</p>"
_TAIL = b"<p>after</p></body></html>"


def _read(raw: bytes, **kwargs: int) -> tuple[list[tuple[str, str]], Diagnostics]:
    report = Diagnostics()
    blocks = [(b.kind, b.text) for b in iter_html_blocks(io.BytesIO(raw), report, **kwargs)]
    return blocks, report


class _Watched(plaintext._HtmlBlocks):
    """Records the two quantities that were unbounded, per parser instance.

    `rescanned` is the exact cost the quadratic was made of: what `html.parser` is
    still holding at the moment it is asked to look at more, which it re-examines from
    the beginning. Counted rather than timed, for the reason the module docstring
    gives.
    """

    last: _Watched | None = None

    def __init__(self) -> None:
        super().__init__()
        self.rescanned = 0
        self.peak_pending = 0
        self.peak_open = 0
        _Watched.last = self

    def feed(self, data: str) -> None:
        self.rescanned += self.pending_chars
        super().feed(data)
        self.peak_pending = max(self.peak_pending, self.pending_chars)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        super().handle_starttag(tag, attrs)
        self.peak_open = max(self.peak_open, len(self._stack), len(self._frames))


@pytest.fixture
def watched(monkeypatch: pytest.MonkeyPatch) -> type[_Watched]:
    monkeypatch.setattr(plaintext, "_HtmlBlocks", _Watched)
    return _Watched


def _instrumented(raw: bytes, watched: type[_Watched], **kwargs: int) -> _Watched:
    report = Diagnostics()
    for _block in iter_html_blocks(io.BytesIO(raw), report, **kwargs):
        pass
    parser = watched.last
    assert parser is not None
    parser.report = report  # type: ignore[attr-defined]
    return parser


def _unterminated(size: int) -> bytes:
    """`size` bytes inside a start tag that never ends. The reported shape."""
    return _HEAD + b"<a " + b"x" * size + _TAIL


# --------------------------------------------------------------------------- #
# 1. an unterminated tag, and the rescanning it used to buy
# --------------------------------------------------------------------------- #


def test_an_unterminated_tag_is_no_longer_rescanned_per_piece(watched) -> None:
    """The headline. Old: 2.5x the document at 256 KB, 16.5x at 2 MB, and climbing by
    the same factor with every doubling. The bound is absolute so that it fails loudly
    if the growing read is ever taken back out."""
    for kilobytes in (256, 512, 1024, 2048):
        raw = _unterminated(kilobytes << 10)

        parser = _instrumented(raw, watched)

        assert parser.rescanned <= 4 * len(raw), (
            f"{kilobytes} KB rescanned {parser.rescanned:,} characters, "
            f"{parser.rescanned / len(raw):.1f}x the document"
        )


def test_four_times_the_document_costs_four_times_the_work(watched) -> None:
    """The exponent itself, since that is what the finding was about: 4x the input used
    to cost ~15x the rescanning, and 16x it at the sizes a crawler actually meets."""
    small = _instrumented(_unterminated(512 << 10), watched).rescanned
    large = _instrumented(_unterminated(2048 << 10), watched).rescanned

    assert large / small < 6, f"{small:,} -> {large:,} for 4x the document is not linear"


def test_the_parser_never_holds_more_than_the_ceiling(watched) -> None:
    """What bounds the memory: the buffer is checked after every piece, and the piece
    that crosses the ceiling is as large as the buffer, so twice it is the most that
    can accumulate before the feed stops."""
    parser = _instrumented(_unterminated(17 << 20), watched)

    assert parser.peak_pending <= 2 * _MAX_PENDING_CHARS


@pytest.fixture(scope="module")
def refused() -> tuple[list[tuple[str, str]], Diagnostics]:
    """One parse of a document past the ceiling, shared by the two tests below.

    17 MB, because the ceiling is 8 MiB and the piece that crosses it is as large as
    the buffer: nothing is left unread until the file is past twice the ceiling.
    """
    return _read(_unterminated(17 << 20))


def test_the_tail_that_was_not_read_is_reported(refused) -> None:
    """Rule 3: bounded *and* counted. Refusing to read on is a loss like any other, and
    `lost_data` is the boolean the API tells callers to branch on."""
    _, report = refused

    assert report.lost_data is True
    entry = " || ".join(report.truncated)
    assert "unterminated_markup" in entry
    assert "bytes after it were not read" in entry


def test_what_came_before_it_still_arrives(refused) -> None:
    """Refusing the tail must not cost the head: everything parsed before the ceiling
    is already in the output and stays there."""
    blocks, _ = refused

    assert ("paragraph", "before") in blocks


def test_a_document_that_ends_inside_the_buffer_reports_nothing() -> None:
    """CONTROL for the entry. The ceiling can be passed by the piece that finishes the
    file, and then every byte was fed and nothing is missing -- a truncation there
    would be a report of a loss that did not happen."""
    blocks, report = _read(_unterminated(10 << 20))

    assert report.truncated == []
    assert ("paragraph", "before") in blocks
    assert ("paragraph", "after") in blocks


# --------------------------------------------------------------------------- #
# 2. the controls for the ceiling: what a real page legitimately holds back
# --------------------------------------------------------------------------- #


def test_a_big_inline_script_is_read_to_the_end() -> None:
    """CONTROL, and the reason the ceiling is 8 MiB rather than tight. A `<script>`
    body is held for exactly the same reason a half-written tag is, and the largest
    buffer of the 197 pages measured is a legitimate 695 KB one. This is twice that."""
    page = _HEAD + b"<script>var x = '" + b"a" * (1_500_000) + b"';</script>" + _TAIL

    blocks, report = _read(page)

    assert report.truncated == []
    assert ("paragraph", "before") in blocks
    assert ("paragraph", "after") in blocks


def test_an_ordinary_page_is_still_read_in_ordinary_pieces(watched) -> None:
    """CONTROL for the growing read. Healthy markup holds nothing back between pieces,
    so the piece size must stay exactly what the caller asked for -- the read grows
    only for a document that has already gone wrong."""
    page = _HEAD + (b"<div><p>Revenue rose in every region.</p></div>" * 4000) + _TAIL
    sizes: list[int] = []

    class Recording(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            sizes.append(size)
            return super().read(size)

    report = Diagnostics()
    for _block in iter_html_blocks(Recording(page), report, chunk_bytes=1 << 16):
        pass

    assert len(page) > (1 << 16), "the fixture has to cross a piece boundary"
    assert set(sizes) == {1 << 16}
    assert report.truncated == []


# --------------------------------------------------------------------------- #
# 3. per-tag state, bounded and counted
# --------------------------------------------------------------------------- #


def _never_closed(tag: bytes, count: int) -> bytes:
    return b"<html><body>" + (b"<%s>" % tag) * count + b"text</body></html>"


def test_open_elements_are_bounded(watched) -> None:
    """1,000,000 `<table>` reached 498 MB of RSS through a 6.7 MB file, and the tags do
    not have to be ones the reader knows -- an unknown element is retained the same
    way. Both containers the frame lives in are pinned, because `_frames` is the
    expensive one and `_stack` is the one that lets it grow."""
    for tag in (b"table", b"b", b"nosuchtag"):
        parser = _instrumented(_never_closed(tag, 150_000), watched)

        assert parser.peak_open <= _MAX_OPEN_ELEMENTS
        assert len(parser._stack) <= _MAX_OPEN_ELEMENTS
        assert len(parser._frames) <= _MAX_OPEN_ELEMENTS


def test_the_state_stops_growing_with_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape of the memory, which is what the defect was: peak allocation used to
    be proportional to the file -- 45 MB, 90 MB and 179 MB for 100k, 200k and 400k
    `<table>` -- and is one number now, whatever the input.

    Traced allocation rather than RSS, so that the allocator's own high-water mark
    cannot decide the result. The ceiling is lowered for the same reason the counts are
    small: tracing every allocation of a 400,000-tag document costs seconds, and the
    claim is about the shape, which a 4,096-element ceiling makes exactly as well.
    """
    monkeypatch.setattr(plaintext, "_MAX_OPEN_ELEMENTS", 4096)
    peaks = []
    for count in (30_000, 120_000):
        raw = _never_closed(b"table", count)
        tracemalloc.start()
        try:
            report = Diagnostics()
            for _block in iter_html_blocks(io.BytesIO(raw), report):
                pass
            peaks.append(tracemalloc.get_traced_memory()[1] - len(raw))
        finally:
            tracemalloc.stop()

    assert peaks[1] < 1.3 * peaks[0], f"{peaks[0]:,} -> {peaks[1]:,} still tracks the input"
    # ~500 bytes per open `<table>` measured, so the shipped ceiling of 65,536 bounds
    # the same state at ~33 MB and this one at ~2 MB.
    assert peaks[1] < 4 << 20, f"peak {peaks[1]:,} bytes above the input"


def test_the_untracked_elements_are_reported() -> None:
    """Rule 3 again. The text is all still there; the structure around it is not, and
    that is the half a caller cannot see for themselves."""
    _, report = _read(_never_closed(b"table", 150_000))

    assert report.lost_data is True
    entry = " || ".join(report.truncated)
    assert "nesting_untracked=" in entry
    assert f"{_MAX_OPEN_ELEMENTS:,}" in entry


def test_the_text_of_such_a_document_still_comes_back() -> None:
    """Bounding the tracking must not drop the content: a browser renders this page and
    so do we."""
    blocks, _ = _read(_never_closed(b"b", 150_000))

    assert ("paragraph", "text") in blocks


# --------------------------------------------------------------------------- #
# 4. the controls the nesting ceiling is judged against
# --------------------------------------------------------------------------- #


def test_a_deeply_nested_page_is_untouched() -> None:
    """CONTROL. The deepest of 197 pages measured holds 36 elements open; this is
    1,000, and it must read exactly as it always did. A guard that fires on a real
    page is worse than the bug it was added for."""
    page = b"<html><body>" + b"<div>" * 1000 + b"Revenue rose." + b"</div>" * 1000 + b"</body>"

    blocks, report = _read(page)

    assert report.truncated == []
    assert report.lost_data is False
    assert blocks == [("paragraph", "Revenue rose.")]


def test_optional_end_tags_are_not_treated_as_an_attack() -> None:
    """CONTROL, and the reason the ceiling is 65,536 rather than a few hundred. This
    reader does not implement HTML5's optional end tags, so 20,000 `<li>` written
    without `</li>` -- legal markup a browser closes for itself -- holds 20,000 frames
    here and must still be read in full."""
    items = b"".join(b"<li>Item %d" % index for index in range(20_000))

    blocks, report = _read(b"<html><body><ul>" + items + b"</ul></body></html>")

    assert report.truncated == []
    assert len(blocks) == 20_000
    assert blocks[0] == ("list_item", "Item 0")
    # The last item is flushed by `</ul>` rather than by the next `<li>`, which is why
    # it is a paragraph. Unchanged by any of this, and asserted so that a future reader
    # does not take the count above for a rounding error.
    assert blocks[-1] == ("paragraph", "Item 19999")


def test_nested_tables_still_keep_their_frames() -> None:
    """CONTROL for the `<table>` half: the frame is skipped only past the ceiling, and
    a layout-nested table -- which is everywhere on the web -- must still recover the
    outer row, with its cells under the columns they started in."""
    rows = [
        text.split("\t")
        for kind, text in _read(
            b"<table><tr><td>OUTER</td><td><table><tr><td>INNER</td></tr></table></td>"
            b"<td>LAST</td></tr></table>"
        )[0]
        if kind == "table_row"
    ]

    assert rows == [["INNER"], ["OUTER", "", "LAST"]]


def test_a_healthy_page_gains_nothing_at_all() -> None:
    """The control both fixes are worth nothing without: an ordinary page must gain no
    note and no truncation entry from either of them."""
    blocks, report = _read(
        b"<html><head><title>Quarterly report</title></head><body>"
        b"<h1>Quarterly report</h1><p>Revenue rose in every region.</p>"
        b"<table><tr><th>Region</th><th>Q1</th></tr>"
        b"<tr><td>EMEA</td><td>1200</td></tr></table>"
        b"<ul><li>First</li><li>Second</li></ul></body></html>"
    )

    assert report.truncated == []
    assert report.lost_data is False
    assert [note for note in report.notes if not note.startswith("title=")] == []
    assert ("heading", "Quarterly report") in blocks
    assert ("table_row", "EMEA\t1200") in blocks
