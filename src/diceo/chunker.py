"""One chunker, for every format.

There were two, one per format family, and each independently failed to split a unit
larger than the whole target -- once turning 20 PDFs into 20 chunks
(experiment 017, competitor retrieval) and once leaving 23 of 33
chunks oversized at a target of 600
(experiment 020, structure beats the incumbent). The same defect twice, in two
places, is the argument for one implementation: packing decisions are format-blind,
so a second copy buys nothing and can only drift.

The three rules that carry the measured win, each a finding rather than a preference:

* **a table travels with its header.** A run of ``table_row`` blocks is one unit, and
  when it must be split the header is repeated into every group. Without it
  ``wide_table_cell`` scores 0.065; with it, 0.538
  (experiment 016, recovering boundaries blind).
* **a heading never ends a chunk.** It is carried into the next one, because a
  stranded heading is both a bad chunk and a lost answer -- 12.8% of facts live in
  headings (experiment 002, does structure pay).
* **a heading only starts a new chunk once the current one is worth keeping**
  (``target // 3``). Flushing on every heading produced a median of 880 characters
  against a target of 1800.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from time import monotonic

from diceo.types import Block, Chunk, Diagnostics, Limits


def _split_long(text: str, target: int) -> list[str]:
    """Cut a block bigger than the whole target into sentence-sized pieces.

    Prefers a sentence end, falls back to a word boundary, and splits mid-word only
    when a single word exceeds the target.

    Walks the string with an **index** rather than re-slicing the remainder. The
    obvious ``rest = rest[cut + 1:]`` copies everything still to come on every cut, so
    the cost was quadratic in the block's length: measured at exponent **2.77**, with
    8 M characters taking 4.0 s and each doubling costing about seven times as much.
    A block that long is not hypothetical -- a spreadsheet cell holding a pasted
    document, or a PDF page whose text layer is one unbroken run -- and it is the
    caller's whole pipeline that stops while it finishes. Same cuts, same output.
    """
    if len(text) <= target:
        return [text]
    pieces: list[str] = []
    floor = max(target // 4, 1)
    start = 0
    end = len(text)
    while end - start > target:
        window = text[start : start + target]
        cut = max(
            window.rfind(". "),
            window.rfind("! "),
            window.rfind("? "),
            # CJK sentence ends take no space after them.
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
        )
        if cut < floor:
            # A newline is a word boundary too: a list of URLs has no spaces at all.
            cut = max(window.rfind(" "), window.rfind("\n"))
        if cut < floor:
            cut = target - 1
        # Belt to `Limits.__post_init__`'s braces. Every branch above can leave `cut`
        # negative, and a negative cut consumes nothing, so the loop would run forever
        # on a caller who built the limits some other way. Progress is the invariant;
        # assert it here rather than trusting five branches to preserve it.
        cut = max(cut, 0)
        piece = text[start : start + cut + 1].strip()
        if piece:
            pieces.append(piece)
        start += cut + 1
        while start < end and text[start].isspace():  # what `.lstrip()` did
            start += 1
    if start < end:
        pieces.append(text[start:end])
    return pieces


class _Packer:
    """Accumulates blocks and emits chunks. Holds at most one chunk's worth."""

    def __init__(
        self,
        doc_id: str,
        limits: Limits,
        diagnostics: Diagnostics,
        extra: Mapping[str, object] | None = None,
    ):
        self.doc_id = doc_id
        self.extra = extra
        self.target = limits.target_chars
        self.table_target = limits.resolved_table_chars()
        self.pending: list[Block] = []
        #: ``(level, text)`` of the open headings. By level, not position: a document
        #: that starts at H2 made its first H2 the title of every sibling after it.
        self.trail: list[tuple[int, str]] = []
        #: The trail before the trailing run of held headings. Those headings move
        #: to the next chunk, so the chunk being flushed must not be labelled by them.
        self.trail_before_heads: list[tuple[int, str]] = []
        self.table_header: Block | None = None
        self.table: list[str] = []
        self.table_chars = 0
        self.table_budget = 0
        self.table_caption: str | None = None
        self.table_one_row = False
        #: Consecutive `list_item` blocks, buffered when grouping is requested.
        # ponytail: a whole list run; stream groups if this optional mode needs bounds.
        self.list_run: list[Block] = []
        self.list_group = max(0, limits.list_group_size)
        self.offset = 0
        self.index = 0
        self.overlap = max(0, limits.overlap_chars)
        # Tail of the last emitted chunk, prepended to the next one. Kept as a
        # string rather than a Block so it never re-enters the size accounting and
        # cannot compound across chunks.
        self.carry = ""
        #: ``sum(len(b.text) for b in self.pending)``, maintained rather than
        #: recomputed. Every block asks for it at least once to decide whether it
        #: fits, and asking walked the whole pending list -- so packing one chunk out
        #: of *n* blocks cost O(n^2) length lookups. Kept in step by `_hold`,
        #: `_drop_last` and the three places that empty `pending`, which are the only
        #: ways the list changes.
        self.pending_chars = 0

    @property
    def size(self) -> int:
        return self.pending_chars

    def _hold(self, block: Block) -> None:
        """Add one block to the pending chunk, keeping `pending_chars` true."""
        self.pending.append(block)
        self.pending_chars += len(block.text)

    def _drop_last(self) -> Block:
        """Take the last pending block back off, keeping `pending_chars` true."""
        block = self.pending.pop()
        self.pending_chars -= len(block.text)
        return block

    def _build(self) -> Chunk:
        joined = "\n".join(b.text for b in self.pending)
        body = f"{self.carry}\n{joined}" if self.carry else joined
        if self.overlap:
            self.carry = joined[-self.overlap :].lstrip()
        chunk = Chunk(
            text=body,
            index=self.index,
            doc_id=self.doc_id,
            title=self.trail[0][1] if self.trail else "",
            breadcrumb=" > ".join(text for _, text in self.trail[1:]),
            kinds=tuple(dict.fromkeys(b.kind for b in self.pending)),
            locator=self.pending[0].locator,
            char_start=self.offset,
            char_end=self.offset + len(body),
            extra=self.extra,
        )
        self.offset += len(body)
        self.index += 1
        self.pending = []
        self.pending_chars = 0
        return chunk

    def flush(self) -> Iterator[Chunk]:
        if self.pending and any(b.text.strip() for b in self.pending):
            yield self._build()
        else:
            self.pending = []
            self.pending_chars = 0

    def _flush_carrying_headings(self) -> Iterator[Chunk]:
        """Flush, but keep any trailing headings for the next chunk."""
        carried: list[Block] = []
        while self.pending and self.pending[-1].is_heading:
            carried.insert(0, self._drop_last())
        if carried:
            trail, self.trail = self.trail, self.trail_before_heads
            yield from self.flush()
            self.trail = trail
        else:
            yield from self.flush()
        self.pending = carried
        self.pending_chars = sum(len(b.text) for b in carried)

    def close_table(self) -> Iterator[Chunk]:
        """Finish the current group, retaining the one-row table special case."""
        header = self.table_header
        if header is None:
            return
        self.table_header = None
        if self.table:
            self._append_group(header, self.table, self.table_caption)
            self.table = []
            self.table_chars = 0
        else:
            # Wait for a second row before interpreting the first as a header:
            # repeating a single detected row would duplicate its data.
            if self.pending and self.size + len(header.text) > self.target:
                yield from self._flush_carrying_headings()
            self._hold(header)

    def _take_caption(self) -> str | None:
        """Remove and return the table's caption, if the block before it is one.

        A caption is a short label; a *sentence* is not. Replicating a sentence into
        every row group would put unrelated prose in every chunk, which 030 measured
        at roughly 0.10 of cosine on narrow tables -- so sentence-final punctuation
        disqualifies a candidate. That is conservative in the right direction: a
        caption written with a full stop is merely not carried, while prose is never
        replicated. It is also language-neutral, which a ``Table N:`` pattern is not.

        Headings are excluded because the trail machinery already carries them.
        """
        if not self.pending:
            return None
        candidate = self.pending[-1]
        if candidate.is_heading or candidate.kind == "slide_title":
            return None
        if candidate.kind in ("table_row", "sheet_row"):
            # A one-row table leaves its row in `pending`, and adopting it as the *next*
            # table's caption replicated another table's data into every row group --
            # with the wrong locator attached. A caption is prose, never a row.
            return None
        text = candidate.text.strip()
        if not text or len(text) > self.table_target // 3:
            return None
        if "\n" in text or text[-1] in ".!?":
            return None
        self._drop_last()
        return text

    def _append_group(
        self, header: Block, group: list[str], caption: str | None = None
    ) -> None:
        """Add one header-carrying row group to the pending chunk.

        Appending and then flushing on overflow -- rather than flushing *first* when
        the group will not fit -- is deliberate and measured. Flushing first looks
        tidier: it stops a group's header being the last thing in a chunk with its
        rows in the next one. But it also cuts chunks earlier, and on held-out PDFs
        that cost **-1.8pp** (0.845 to 0.827) for 83 extra chunks, because more
        chunks means more distractors competing for the same query -- the effect
        experiment 016 (recovering boundaries blind) measured.
        Tidier output, worse retrieval; rule 4 decides.
        """
        prefix = f"{caption}\n{header.text}" if caption else header.text

        # Two groups of the same table can land in one chunk, and repeating the
        # prefix in that chunk is pure waste: measured over the held-out DOCX, header
        # text was 58.2% of every wide-table chunk and 28.1% of that was a second
        # copy. Merging into the previous group keeps the same rows in the same chunk
        # and the same chunk count -- only the redundant prefix goes.
        if self.pending:
            last = self.pending[-1]
            if prefix and last.kind == "table_row" and last.text.startswith(prefix):
                merged = "\n".join([last.text, *group])
                self.pending[-1] = last._replace(text=merged)
                self.pending_chars += len(merged) - len(last.text)
                return

        self._hold(
            Block(
                "table_row",
                "\n".join([prefix, *group]) if prefix else "\n".join(group),
                0,
                header.locator,
            )
        )

    def close_list_run(self) -> Iterator[Chunk]:
        """Emit buffered list items in groups of at most ``list_group``.

        The rule `table_row` has had since D6, applied to the kind that measurably
        needs it. Experiment 047 measured the mechanism in **both** diceo's output
        and Unstructured's and got the same curve, which is what makes it a property
        of the retriever rather than of either tool: a chunk holding one bullet scores
        1.000, three scores 0.857, five or more scores 0.550. Every sibling bullet is
        a near-shaped distractor pulling the chunk vector toward another site, metric
        and quarter.

        **Off by default** (`list_group_size = 0`), and that is not timidity. The gain
        was found on the same corpus that produced the deficit, and capping the group
        makes more, smaller chunks -- which experiments 023 and 025 established is not
        comparable at a fixed nominal target. 048 registers the three arms that settle
        it, including the size-matched control the first draft of that file lacked.
        """
        if not self.list_run:
            return
        run, self.list_run = self.list_run, []
        for start in range(0, len(run), self.list_group):
            group = run[start : start + self.list_group]
            if self.pending and self.size + sum(len(b.text) for b in group) > self.target:
                yield from self._flush_carrying_headings()
            elif start and self.pending:
                # Force the boundary between groups; without this the groups are
                # merely appended and the run reassembles itself inside one chunk.
                yield from self._flush_carrying_headings()
            self.pending.extend(group)
            self.pending_chars += sum(len(b.text) for b in group)

    def add(self, block: Block) -> Iterator[Chunk]:
        text = block.text.strip()
        if not text:
            return

        if block.kind in ("table_row", "sheet_row"):
            if self.list_run:
                yield from self.close_list_run()
            if block.locator.row == 0 and self.table_header is not None:
                # Adjacent tables can have no nonempty paragraph between them.
                yield from self.close_table()
            # A leading tab or ` | ` is an empty first cell: keep its column.
            row_text = block.text.strip(" \r\n")
            header = self.table_header
            if header is None:
                self.table_header = block._replace(text=row_text)
                return
            if not self.table:
                # PDF row zero is a recovered header. An unresolved first DATA
                # row belongs in the group once, without becoming a repeated label.
                if header.locator.page >= 0 and header.locator.row < 0:
                    self.table = [header.text]
                    self.table_chars = len(header.text)
                    header = header._replace(text="")
                    self.table_header = header
                self.table_one_row = _wants_one_row_per_group(header.text, self.table_target)
                # Wide rows need their short caption in every group (experiment 030).
                self.table_caption = self._take_caption() if self.table_one_row else None
                overhead = len(header.text) + (
                    len(self.table_caption) + 1 if self.table_caption else 0
                )
                self.table_budget = max(self.table_target - overhead - 1, len(header.text))
            if self.table and (
                self.table_one_row or self.table_chars + len(row_text) > self.table_budget
            ):
                # The next row closes the previous group, exactly as the buffered
                # algorithm did. Append before flushing to preserve retrieval cuts.
                self._append_group(header, self.table, self.table_caption)
                self.table = []
                self.table_chars = 0
                if self.size >= self.target:
                    yield from self.flush()
            self.table.append(row_text)
            self.table_chars += len(row_text)
            return
        yield from self.close_table()

        if self.list_group:
            if block.kind == "list_item":
                self.list_run.append(block._replace(text=text))
                return
            # A bullet that wraps arrives as `list_item` followed by a `paragraph`
            # carrying the rest of the sentence, so a run reads
            # `list_item, list_item, paragraph, list_item, paragraph`. Treating that
            # paragraph as the end of the run makes the whole rule inert: measured
            # over both held-out corpora, only **8% of list items** sit in a
            # consecutive run longer than two, against the 4.12 bullets per gold
            # chunk experiment 047 measured. The continuation is what breaks them up,
            # so it has to be absorbed into the item above it rather than ending the
            # run -- which is what 048 registered and what this restores.
            if (
                self.list_run
                and block.kind == "paragraph"
                and not self.list_run[-1].text.rstrip().endswith((".", "?", "!", ":", ";"))
            ):
                last = self.list_run[-1]
                self.list_run[-1] = last._replace(text=f"{last.text} {text}")
                return
            yield from self.close_list_run()

        if block.is_heading:
            if self.size >= self.target // 3:
                yield from self.flush()
            level = max(block.level, 1)
            if not (self.pending and self.pending[-1].is_heading):
                self.trail_before_heads = list(self.trail)
            while self.trail and self.trail[-1][0] >= level:
                self.trail.pop()
            self.trail.append((level, text))
            self._hold(block._replace(text=text))
            return

        for piece in _split_long(text, self.target):
            if self.pending and self.size + len(piece) > self.target:
                yield from self._flush_carrying_headings()
            self._hold(block._replace(text=piece))

    def finish(self) -> Iterator[Chunk]:
        yield from self.close_list_run()
        yield from self.close_table()
        yield from self.flush()


#: A cell boundary a reader put there on purpose. OOXML and HTML tables have real
#: cells, so their rows arrive delimited; a PDF "table row" is a line of text whose
#: columns were *inferred* from whitespace and has no delimiter at all.
_CELL_SEPARATORS = (" | ", "\t")
#: Below this many cells, a header is not wide enough for granularity to matter.
_WIDE_TABLE_CELLS = 5


def _wants_one_row_per_group(header: str, target: int) -> bool:
    """Should a wide table emit one data row per chunk?

    A wide table's header is itself most of a chunk, so packing two data rows behind
    it makes the chunk half about the row nobody asked for. Measured on held-out
    DOCX (025): our chunk for a 14-column table held the caption, the header, *two*
    sites' rows and a leaked header, and scored 0.346 on wide-table questions
    against MarkItDown's 0.654 -- whose chunk held the header and exactly one row.
    MarkItDown does not do that on purpose; its pipe-and-dashes markup inflates each
    row until only one fits. The granularity is what pays, so this takes the
    granularity without the markup: **+0.8pp on held-out DOCX**.

    The delimiter condition is not cosmetic, it is the whole reason this is a
    function. Applied to every table, the same rule cost **-1.8pp on held-out PDF**,
    because a PDF has no cell boundaries: rows are lines whose columns were inferred
    from whitespace, and a wide table's header cells wrap onto separate lines, so
    ``table[0]`` is a *fragment* of a header. Splitting one row per group there
    repeats a partial header and separates rows that belonged together. Requiring
    real delimiters restricts the change to the formats whose tables are structure
    rather than a guess -- which is the same distinction 020 drew when it found
    recovered structure worth +8pp and inferred geometry worth much less.
    """
    if len(header) <= target // 3:
        return False
    cells = max(header.count(separator) for separator in _CELL_SEPARATORS)
    return cells >= _WIDE_TABLE_CELLS


def chunk_blocks(
    blocks: Iterable[Block],
    *,
    doc_id: str = "",
    limits: Limits | None = None,
    diagnostics: Diagnostics | None = None,
    extra: Mapping[str, object] | None = None,
) -> Iterator[Chunk]:
    """Turn a lazy ``Block`` stream into a lazy ``Chunk`` stream.

    Honours ``limits.max_chars`` and ``limits.max_seconds``, recording the truncation
    rather than performing it quietly.

    The time budget is checked **per block, before the work**, not per chunk: a
    document can spend a long time producing one chunk (a 300-page table), and a
    check that only ran on emit would overshoot by exactly the pathological case it
    exists to bound. What is already produced is still yielded, and the partial read
    is flushed -- a budget that bit should cost you the tail of a document, not the
    whole of it.
    """
    limits = limits or Limits()
    diagnostics = diagnostics if diagnostics is not None else Diagnostics()
    packer = _Packer(doc_id, limits, diagnostics, extra)
    emitted_chars = 0
    deadline = None if limits.max_seconds is None else monotonic() + limits.max_seconds

    # A truncation is recorded when a chunk past the limit actually arrives, not the
    # moment the limit is reached: a document that fits exactly lost nothing.
    cap = limits.max_chars
    for block in blocks:
        if deadline is not None and monotonic() > deadline:
            diagnostics.truncate(f"max_seconds={limits.max_seconds}")
            break
        diagnostics.blocks += 1
        for chunk in packer.add(block):
            if cap is not None and emitted_chars >= cap:
                diagnostics.truncate(f"max_chars={cap}")
                return
            emitted_chars += len(chunk.text)
            diagnostics.chunks += 1
            diagnostics.chars = emitted_chars
            yield chunk
    for chunk in packer.finish():
        if cap is not None and emitted_chars >= cap:
            diagnostics.truncate(f"max_chars={cap}")
            return
        emitted_chars += len(chunk.text)
        diagnostics.chunks += 1
        diagnostics.chars = emitted_chars
        yield chunk


__all__ = ["chunk_blocks"]
