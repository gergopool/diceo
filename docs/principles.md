# The five rules

Every non-obvious decision in this library was settled by one of these. They are written down
because a principle that lives only in someone's head loses to the next convenient exception.

## 1. Licence purity is a hard gate, not a preference

**No GPL, AGPL or SSPL anywhere in the default install.** That is the whole reason this project
exists rather than `pip install pymupdf4llm`: the fastest tools in this space are AGPL, which for a
great many organisations means they cannot be used at all.

A dependency that fails the gate cannot be used *even temporarily* "just to get a number" — it may
appear only in a clearly-marked benchmark extra, never in a code path that ships.

CI enforces it. `just license-check` builds a virtualenv holding only the default dependencies,
reads the declared licence of everything in it, **and reads the licence and notice files those
wheels bundle** — because a binary wheel can carry copyleft text its own metadata never mentions.
Every copyleft mention found has to be individually audited and accounted for, or the gate fails.

## 2. Stream, never materialize

Readers are generators yielding blocks; the chunker consumes that iterator lazily. **A 1,000-page
PDF or a million-row sheet must never exist as one Python string.**

If you find yourself writing `"".join(...)` over a whole document, stop. Memory that grows with
document size is a bug that only shows up on the one document a user cared about.

## 3. Never lose data silently

Truncation, skipped parts, pages with no text layer, unparseable sections — all of it is counted,
and the caller can read the counts.

`chunk()` returns an *iterator*, so there is no result object to hang a report on. You hand one in
and read it after the iterator is drained:

```python
report = diceo.Diagnostics()
chunks = list(diceo.chunk(path, diagnostics=report))

if report.lost_data:
    log.warning("incomplete: %s", report.as_dict())
```

Use a fresh `Diagnostics()` per document — it is a report on one call, and one you reuse keeps
counting.

**A document that silently isn't in the index is the worst failure mode in this domain.** It is
undetectable from the outside: retrieval just quietly gets worse and nobody knows which documents to
blame. Every extraction tool we have measured has this failure somewhere — including a widely-used
library that opens a spreadsheet, reports a page count, and returns zero characters without raising.

A caller sees an error, or they see chunks. Never a third thing.

## 4. Retrieval quality is the only quality metric

Not markdown fidelity. Not visual plausibility. Not how good the output looks pasted into a
terminal.

A change ships when the retrieval harness says it helps, or says it is neutral and the change is
faster. **Prettier output that does not move retrieval is not an improvement**, and this project has
rejected several of its own changes on exactly that ground.

## 5. Measure before believing

Every published number is either measured — saying where, and on what — or explicitly labelled
inherited and unverified. No vibes-based performance claims.

When a published figure turned out to have been patched by arithmetic across two different corpora
rather than re-measured, it was withdrawn and re-run. When a metric turned out to score line
wrapping as data loss, the claim built on it was withdrawn in public even though it flattered us.

Benchmarks are reproducible or they are not benchmarks: every table in
[benchmarks.md](./benchmarks.md) carries a **Provenance** line naming the script that produced it,
the fixture corpus it ran on, and the experiment that ran it. The harness itself lives in the parent
research repository, which carries this one as a submodule — a library you have to download a gigabyte of corpora to build is not a small
library, whatever its wheel says.
