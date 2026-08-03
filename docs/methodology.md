# Methodology

Two facts a benchmark is least often given — **when it was run** and **against which versions** —
are here, because without them you cannot tell a current comparison from a stale one, and this
field ships weekly.

[benchmarks.md](benchmarks.md) is the results. This page is the method behind the headline
comparison: the run of eight arms over seven tools on 44 held-out PDFs that produces the chart on
the README and the ranked table beside it. Every other table in `benchmarks.md` carries its own
provenance note naming the script and the fixture that produced it.

---

## Hardware and environment

One box: 6 cores, 30 GB RAM, one RTX 5070 Ti, Linux. Every arm runs as a single process, one
document at a time, with nothing else scheduled against it. The GPU exists for the embedding model.
No arm except MinerU uses it, which is why MinerU's cost figures are the ones to distrust — see
*Cost* below.

## Corpus

44 **synthetic** PDFs, 1,026 pages, **776 queries**. Rendered from seeds 23 and 41
(`data/holdout-23` + `data/holdout-41` in the research repository) and never used to develop
anything.

Generated from unseen seeds is not the same as real documents withheld, and the difference shows:
on real PDFs Diceo comes out **behind**, 0.833 against 0.867 for three other tools. That is a much
smaller study — nine public documents, 120 probes, an effective sample size of 33 — and it is in
[benchmarks.md](benchmarks.md#real-documents) with its caveats.

Queries are labelled by what the answer is carried in — a table cell, a footnote, a heading, a list
item, and five more — so the aggregate can be split by carrier. That split is the most informative
table this project produces, and it is on the README.

## Why these arms

Six of the eight were added because the older four-tool field could not have falsified the headline.
Two of the four were the same AGPL family and were the only two that beat Diceo on either axis;
the other two were architectures that could not have come out ahead on cost whatever they scored.
The permissive set capable of beating us had **two members, neither capable of it**. The six added
are the fast ones, the permissive ones, and the one people ask about by name.

## Quality

Each tool's text is chunked, embedded with `nvidia/Nemotron-3-Embed-1B-BF16`, and queried. A chunk
is *gold* when it contains the answer; `gold@5` is the fraction of the 776 queries whose gold chunk
lands in the top five.

**Extraction and scoring run in separate environments.** Each rival pins its own native core —
MinerU wants `torch==2.13`, Xberg and LiteParse ship Rust extensions, Unstructured resolves 139
distributions — and installing any of them beside the embedder would change which torch the embedder
loads. So extraction writes plain text to disk from an isolated virtualenv and one scorer reads
those directories. That separation is the only reason every `gold@5` in this project is comparable
with every other.

## Chunking

**Each tool uses its own chunker where it ships one**, configured to the same 600-character target
and to zero overlap — ours defaults to zero, and overlap buys recall by growing the index. Xberg,
Kreuzberg and Unstructured ship a chunker; MinerU, Extractous and LiteParse do not and were chunked
by ours. That asymmetry is real and it is not in our favour.

The ranked table shows each tool on its **better** arm, which for all three that ship a chunker is
ours rather than theirs: Kreuzberg 0.760 → 0.813, Xberg 0.786 → 0.817, Unstructured 0.816 → 0.820.
The chart uses the stricter reading — each tool on its own chunker, at its own cost. That reading is
worse for the rivals, not better, and both are published.

## Cost

Warm CPU-seconds from `getrusage`, user + system, one process per tool, the first document dropped
as warm-up. Documents per CPU-second, because an indexing pipeline runs one worker per document on a
box that is already busy, and there what you pay is CPU rather than wall clock.

Diceo's row is the only one with a replicate: **44.88 and 43.37** documents per CPU-second in two
sessions on the same day, 3.4% apart. That is why ratios derived from this table are quoted as ranges
rather than to three figures. The rivals were timed once, in the earlier of the two sessions.

Two costs are generous to the tool rather than harsh, and both are stated because neither is
recoverable from the number itself:

- **Both MinerU arms ran on the RTX 5070 Ti, and `getrusage` cannot see GPU time.** Both throughput
  figures are therefore better than a caller without a GPU would ever see, by an amount nobody has
  measured. `pipeline` also averages 2.63 cores, so its wall-clock throughput is 2.6× its cost.
- **Docling's core count was never measured**, so wherever it is placed on a per-CPU-second axis it
  is credited with 1.0 cores — the most generous reading available to it — and its cost is derived
  from one PDF.

## Statistics

Exact two-sided McNemar on `gold@5`, **paired per query**: every arm answers the same 776 questions,
so each comparison is a paired test rather than two independent proportions.

Eleven rival arms were compared, so the Bonferroni threshold is 0.05 / 11 = **0.00455**. Applied,
it removes most of the leads: the four closest arms are ahead of nothing.

Chunk-size targets 1800 and 600 were **pre-registered** before the run, which fixed a threshold of
0.025 for that pair in advance. The 4000-character target was added afterwards and is reported as
exploratory, because a third target moves the threshold to 0.0167 and any p-value between those
bounds changes meaning depending on when the target was declared.

Where a result depends on nested observations rather than independent ones — fifteen questions about
one PDF are not fifteen independent trials — intervals come from a document-clustered bootstrap.
That correction is what took the real-document margin from p = 7e-05 to **p = 0.020**, and the
uncorrected figure is withdrawn.

## Versions and dates

The ranked run: **2026-08-01**, one box, one session. Each version is the newest release on PyPI on
the day of the run except where noted.

| tool | version | released |
|---|---|---|
| Diceo | 0.1.0.dev0 | this tree |
| Unstructured | 0.25.0 | 2026-07-31 |
| Xberg | 1.0.7 | 2026-07-31 |
| LiteParse | 2.10.1 | 2026-07-29 |
| Kreuzberg | 4.10.2 | 2026-07-12 — the last release under that name; it is now Xberg |
| MinerU | 3.4.4 | 2026-07-10 |
| Extractous | 0.3.0 | **2024-12-21** — its newest release; that project has not shipped in nineteen months |

The per-format table adds MarkItDown 0.1.6, Docling 2.116.0 and PyMuPDF / PyMuPDF4LLM 1.28.0, all
measured in earlier sessions. MarkItDown 0.1.7 and Docling 2.117.0 landed one and two days before
the run and are **not** what was measured.

## Reproducing it

The harness lives in the research repository that carries this one as a submodule — a package whose
selling point is a two-wheel dependency tree should not ask you to clone a gigabyte of corpora to
build it. `bench/extract_rivals.py` and `bench/extract_mineru.py` produce the text,
`bench/run_derived.py` scores it into `data/results/derived_arms.json`, and `bench/speed_formats.py`
produces the cost table. Every table in [benchmarks.md](benchmarks.md) names the script and the
fixture behind it.
