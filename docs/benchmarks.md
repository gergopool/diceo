# Benchmarks

Every number here was measured. Where a figure was superseded, the old one is struck through rather
than deleted — a benchmark history you cannot audit is marketing.

The harness that produces these lives in the parent research repository, along with the full
experiment record. This page is the outcome; that repository is the working.

## 0.1.1 refresh — 2026-09-30

The README hero now uses one pinned embedding checkpoint/runtime and freshly compatible
vectors on the same 44 held-out PDFs / 776 queries at three chunk-size aims. Cached vectors
from older runtimes are not mixed with newly encoded text. Diceo CPU is remeasured; comparison
CPU costs remain explicitly carried measurements. Four older comparison tools whose cached
texts are unavailable are omitted from this refresh; their historical tables below remain.

| Tool | Mean `gold@5`, 600/1800/4000 | PDF docs/CPU-s |
|---|---:|---:|
| Diceo 0.1.1 | 0.767 | 44.59 |
| Xberg | 0.745 | 13.09 |
| LiteParse | 0.723 | 29.49 |
| Unstructured | 0.719 | 0.601 |
| MinerU | 0.714 | 0.0218 |
| Extractous | 0.694 | 17.53 |

**Change checks.** Relative to the frozen pre-update working tree, DOCX/PPTX/XLSX/HTML
retrieval scores are unchanged at all three aims. PDF shifts are +0.26, −0.39 and −0.52
percentage points; a strict universal PDF retrieval gain is not established. A separate
36-real-document / 556-query check retains all hit, coverage and association results.

**Cost checks.** Paired isolated measurements first identified CSV and legacy-sheet
regressions, which were optimized before acceptance. Final focused public XLSX CPU is
3.76% lower (90% paired interval: 3.14–3.89% lower); XLSB is 16.88% lower. CSV is effectively
unchanged (−0.38% point estimate, interval includes zero). Large ODS peak current-process
memory is 149.1 → 119.2 MiB. Measurements describe these corpora, not every document.

**HTML comparison.** On twelve real public HTML documents, final Diceo chunking consumes
1.1747 CPU seconds / 26.0 MiB peak current-process memory, compared with 4.724 CPU seconds
for Trafilatura default TXT plus splitting: 4.02× faster on this sample. Twenty-seven
selected substantive anchors and 114 selected authored math alternatives are retained.
Trafilatura remains a benchmark tool, not a runtime dependency.

**Provenance.** Research experiment 057; `scripts/bench057_improvements.py`, the paired
retrieval harness and pinned checkpoint/cache namespace in `bench/embed.py`, public source
hash manifest `bench/public/audit-056.jsonl`, and `scripts/plot057.py`. Original and candidate
source snapshots, all accepted/rejected timing cells, raw rankings and chart inputs remain
in the research record. GPU use is confined to that retrieval evaluation.

**How to read this page.** Nothing below is an average across formats, because averaging across
formats hides exactly the cases where a tool falls over. Losses are included. A benchmark table with
no losses in it is read as marketing, and correctly discounted.

---

## Retrieval quality

The only quality metric this project accepts. A change ships when retrieval says it helps —
markdown fidelity and visual plausibility are not measured because they do not predict anything.

**Method.** Documents are chunked by each tool, embedded with Nemotron-3-Embed-1B, and queried. A
chunk is *gold* when it contains the answer; `gold@5` is the fraction of queries whose gold chunk
appears in the top five. Corpora are held out — generated from seeds never used during development.
Comparisons are pre-registered before the run, and paired, because every arm answers the same
queries.

### Synthetic held-out corpora, `gold@5`, 600-character target

| format | docs | queries | **diceo** | markitdown | pymupdf4llm | pymupdf | docling |
|---|---:|---:|---|---|---|---|---|
| pdf | 44 | 776 | 0.845 | 0.805 | **0.854** | 0.796 | 0.827 |
| docx | 24 | 476 | **0.855** | 0.830 | 0.807 | 0.653 | 0.828 |
| pptx | 16 | 300 | **0.987** | 0.980 | 0.967 | 0.900 | **0.987** |
| xlsx | 16 | 316 | **0.528** | 0.449 | **0 chars** | **0 chars** | 0.506 |
| html | 56 | 1,092 | **0.789** | 0.747 | not run | not run | not run |

**Provenance.** `bench/run_formats.py` in the research repository, over the held-out corpora
`data/holdout-23` and `data/holdout-41` — rendered from seeds never used during development — at a
600-character target, embedded with Nemotron-3-Embed-1B. The PDF, PPTX and XLSX rows, and the
MarkItDown, PyMuPDF4LLM and PyMuPDF columns of the DOCX row, are experiment 022 (all formats, and
overlap); *our* DOCX cell is experiment 030 (docx mechanism), which moved DOCX alone and left PDF,
PPTX and XLSX byte-identical; the Docling column is experiment 026 (docling); the HTML row is
experiment 034 (html boilerplate), which added the arm.

DOCX was our one loss for most of this project's life — ~~0.792, behind MarkItDown's 0.830 and
Docling's 0.828~~ — and it was closed by finding the cause rather than by tuning: a wide table's
caption reached only the *first* chunk of a long table, so every subsequent row group was an
unlabelled grid of numbers. Carrying the caption into every row group took it to **0.855**, ahead of
both, for 1.8% *fewer* characters. The hypothesis that was tested first — duplicated header rows —
scored 0/4 and is recorded as wrong.

### Six more extractors, added because the first five were not a fair field

The table above compares five tools, and two of them — PyMuPDF and PyMuPDF4LLM — are the same
AGPL family and were the only two that beat diceo on either axis. The other two were a
pdfminer-lineage converter and a layout-model pipeline: architectures that could not have come
out ahead on cost whatever they scored. So the permissive field that could have beaten us had
**two members, neither capable of it**. Six arms were added to fix that, chosen for being fast,
permissive, or the tool people ask about by name.

Same 44 held-out PDFs, same queries, same embedder, same 600-character target. Eleven rival
arms, so the Bonferroni threshold is 0.05 / 11 = **0.00455**.

| arm | licence | `gold@5` | diceo leads by | p | survives correction |
|---|---|---|---|---|---|
| **diceo** | Apache-2.0 | **0.845** | — | — | — |
| **MinerU** (default backend) | *see below* | **0.829** | +1.7pp | **0.24** | no |
| Unstructured | Apache-2.0 | 0.820 | +2.6pp | 0.045 | no |
| Xberg | MIT | 0.817 | +2.8pp | 0.008 | no |
| Kreuzberg | MIT | 0.813 | +3.2pp | 0.010 | no |
| LiteParse | Apache-2.0 | 0.808 | +3.7pp | 0.003 | yes |
| Extractous | Apache-2.0 | 0.753 | +9.3pp | <0.0001 | yes |

**diceo leads every arm on the point estimate and beats seven of thirteen significantly.
Against the six closest it is ahead but statistically indistinguishable** — and the nearest,
MinerU on its default `hybrid-engine`, is at p = 0.24, nowhere near significance. That is the
claim these numbers support, and it is much weaker than "diceo wins".

**Where rivals ship a chunker, theirs was used, not ours** — and it made them worse, which is
the opposite of what the obvious objection predicts. Kreuzberg's own chunker scores 0.760 against
0.813 through ours; Xberg's 0.786 against 0.817; Unstructured's `chunk_by_title` 0.816 against
0.820. The scores above are the better of the two for each tool.

**The aggregate is not uniform, and this matters more than the aggregate.** Split by what the
query asks for, diceo is beaten on **six of nine carriers** by some tool. The rival column is the
best score **any measured arm** produced on that carrier, which is the reading least flattering to
us — several of these are a tool's second arm rather than the one in the table above:

| carrier | n | diceo | best rival, any arm |
|---|---:|---|---|
| footnote | 104 | 0.981 | 0.990 Kreuzberg |
| list_item | 104 | 0.654 | **0.817** Unstructured (`chunk_by_title`) |
| prose_sentence | 100 | 1.000 | 1.000 (tie) |
| figure_caption | 92 | 1.000 | 1.000 (tie) |
| section_heading | 92 | 0.935 | **1.000** Xberg (own chunker; 0.967 through ours) |
| key_values | 92 | 1.000 | 1.000 (tie) |
| slide_table | 88 | 0.955 | **1.000** Kreuzberg ~~0.989 LiteParse~~ |
| table_cell | 52 | 0.462 | **0.712** MinerU `hybrid-engine` ~~0.635 MinerU~~ |
| wide_table_cell | 52 | 0.154 | **0.404** MinerU `hybrid-engine` (pipe-corrected) |

**Two of those rivals are corrected upward against us.** The struck-through figures were each
tool's *headline* arm rather than its best: `table_cell` 0.635 is MinerU `hybrid-engine` with our
pipe-table pre-correction applied, and without it the same backend scores **0.712** — the best
figure any arm produced on that carrier. `slide_table` 0.989 was LiteParse, but Kreuzberg's text
through `split_smart` reaches **1.000**. Both corrections come from experiment 047, which found
them by re-reading 046's own table. Also worth stating plainly, because it was published the other
way round here and in the landscape survey: **Unstructured does not win `table_cell`** — its
`chunk_by_title` scores 0.4615, identical to ours, and the 0.635 credited to it was its elements
through *our* chunker.

Two permissive, model-free arms beat us on the table carriers as well, which is the more
uncomfortable comparison than MinerU's vision model: LiteParse scores **0.500** on `table_cell`
and **0.346** on `wide_table_cell`.

diceo leads on aggregate by **never collapsing**, not by dominating: Xberg scores 0.000 on
`wide_table_cell` and Extractous 0.398 on `slide_table`, while diceo's worst carrier is 0.154.
Carrier counts are near-uniform, so nothing here is an artefact of one carrier being large.

**And the lead depends on two carriers.** Leave-one-carrier-out: drop `slide_table` and the lead
over Unstructured falls to +0.3pp. **Drop `footnote` and diceo is no longer first** — MinerU
leads by 0.7pp, because we score 0.981 on footnotes against its 0.808 and those 104 queries are
what put us ahead. On a corpus with no decks and no footnotes, expect no gap; on a table-heavy
one, expect to lose.

**Where we lose outright.** `wide_table_cell` and `table_cell`. Both are open backlog items, not
surprises.

#### Cost, same arms, same box, same session

Documents per CPU-second, warm, `getrusage` user + system, one process each, first document
dropped as warm-up. Higher is cheaper. Where a tool ships a chunker, both arms are listed, because
the chunker's cost is part of what a caller pays.

| arm | docs / CPU-s | docs / wall-s | mean cores |
|---|---:|---:|---:|
| **diceo** | **44.88** | 44.89 | 1.00 |
| LiteParse (flat text) | 35.89 | 36.35 | 1.01 |
| LiteParse (markdown) | 29.49 | 29.86 | 1.01 |
| Extractous | 17.53 | 15.78 | 0.90 |
| Xberg | 13.09 | 13.32 | 1.02 |
| Xberg (own chunker) | 12.71 | 12.91 | 1.02 |
| Kreuzberg | 10.08 | 10.20 | 1.01 |
| Kreuzberg (own chunker) | 9.91 | 10.01 | 1.01 |
| Unstructured (`fast`) | 0.601 | 0.601 | 1.00 |
| Unstructured (own chunker) | 0.597 | 0.597 | 1.00 |
| MinerU `pipeline` | 0.0412 | 0.108 | 2.63 |
| MinerU `hybrid-engine` | 0.0218 | 0.023 | 1.06 |

**The gap to the nearest permissive rival is 1.25×, not an order of magnitude**, and that is the
correction this run made to the cost story rather than a confirmation of it. LiteParse's flat-text
mode is 35.89 against our 44.88, and it scores 0.744 in that mode against 0.808 in markdown, which
is the 29.49 row.

**diceo's row is the only one with a replicate.** It was re-timed by the same harness later on
2026-08-01 at **43.37** docs/CPU-second — 3.4% apart, the same order as the run-to-run spread a
busy box produces, and the reason ratios derived from this table are quoted as ranges rather than
to three figures. 44.88 was the figure plotted for experiment 046; the rivals were timed once,
in the earlier of the two sessions. The current 0.1.1 chart uses the refresh above.

**Both MinerU rows exclude GPU time entirely.** They ran on an RTX 5070 Ti and `getrusage` cannot
see any of it, so both figures are *generous* to MinerU, not harsh. `pipeline` also averages 2.63
cores, so its wall-clock throughput is 2.6× its cost.

#### The same field at three chunk-size targets

| target | diceo | best rival | which | delta | p | status |
|---:|---|---|---|---|---|---|
| 4000 | 0.695 | **0.702** | Xberg | **−0.8pp** | 0.73 | **exploratory** |
| 1800 | **0.765** | 0.732 | MinerU `pipeline` | +3.4pp | 0.062 | registered |
| 600 | **0.845** | 0.829 | MinerU `hybrid-engine` | +1.7pp | 0.24 | registered |

**4000 is exploratory and was added after the registered pair had been reported.** Experiment 021
pre-registered exactly two targets, 1800 and 600, which fixed a Bonferroni threshold of 0.025 in
advance; adding a third moves it to 0.0167, and a p-value between those bounds means something
different depending on when its target was declared. It exists because chunk size has dominated
this metric every time it has been examined, and a third point says whether the curve is still
climbing below 600 or turning. **The answer it gives runs against us**: at 4000 the lead is gone
and Xberg is ahead on the point estimate, by less than a point at p = 0.73, with the six closest
arms inside 2.8 points of one another.

Read across a row, not down the column — the caveat under the five-size sweep below applies here
too, and `chars@5` was not recorded for these arms, so the correction that makes small targets
comparable to large ones is only available for the PyMuPDF4LLM sweep.

**On MinerU's licence,** because everyone repeats the wrong one: it is not AGPL any more and it is
not Apache-2.0 either. Releases up to 3.0.9 were AGPL-3.0; from 3.1.0 it ships a custom
`LicenseRef-MinerU-Open-Source-License` — Apache-2.0 plus a commercial-licence requirement above
100M MAU or USD 20M monthly revenue, plus mandatory attribution for online services. It is not
OSI-approved, so an SPDX allowlist rejects it. Separately, the weights its CPU-capable `pipeline`
backend downloads come from a HuggingFace repo whose model card declares `license: agpl-3.0`.

**Provenance.** `bench/extract_rivals.py`, `bench/extract_mineru.py` and `bench/run_derived.py` in
the research repository, over `data/holdout-23` + `data/holdout-41` — the same 44 documents and 776
queries as the PDF row above — embedded with `nvidia/Nemotron-3-Embed-1B-BF16`, exact two-sided
McNemar paired per query. Experiment 046 for the arms, the licences and the carriers; experiment
047 for the two carrier corrections above. Every figure on this page's three tables comes out of
one `data/results/derived_arms.json`, written by that run; the 4000-character column was added to
`run_derived.py`'s `TARGETS` after 046 had been written up, which is why it is marked exploratory.

### Every format, one average

2,644 queries over 140 documents. Each format weighted equally — query counts differ by 3.6×, so a
query-weighted mean would be a PDF-and-HTML number wearing an "all formats" label. Only arms
measurable on **all four** formats are averaged.

| arm | pdf | docx | pptx | html | **average** @600 | @1800 | @4000 |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Diceo** | 84.5 | **85.5** | 98.7 | 78.9 | **86.9** | **82.1** | **75.1** |
| Xberg | 81.7 | 84.2 | 92.3 | 73.6 | 83.0 | 72.6 | 73.6 |
| Kreuzberg | 81.3 | 85.1 | 91.0 | 73.6 | 82.7 | 71.6 | 73.5 |
| Unstructured | 82.0 | 79.4 | 96.3 | 42.8 | 75.1 | 67.7 | 67.3 |

The exclusions matter as much as the ranking. **Extractous beats Diceo on HTML, 0.842 to 0.789
at p = 1.5×10⁻⁶, and is excluded** only because it produced zero-byte DOCX output for 12 of 12
files — methodologically right, and it also removes our strongest challenger on our weakest
format. LiteParse and MinerU `hybrid-engine` are excluded for the same structural reason (no
Office output; an arm that never ran is not an arm that scored zero). On PPTX the top five arms
sit inside 0.6pp with every p ≥ 0.5 — `gold@5` has run out of resolution there. XLSX is measured
but kept internal: 16 synthetic workbooks with a known prose-contamination defect. Six formats —
xls, xlsb, ods, csv, eml, text — have no retrieval corpus and are covered by the robustness sweep
instead. Provenance: `scripts/aggregate_formats.py` over `data/results/derived_arms-*.json` in the
research repository, drawn as `positioning-combined.svg`.

### Harder questions than "find this exact sentence"

The held-out PDF corpus carries **4,541 more queries in six kinds**, each scored on the metric it
needs (600-character target, Diceo arm):

| question kind | n | `gold@5` | MRR |
|---|---:|---:|---:|
| paraphrased | 377 | 0.878 | 0.745 |
| conversational | 1,017 | 0.834 | 0.718 |
| noisy — typos, missing accents, wrong case | 384 | 0.844 | 0.728 |
| vague — *"something about the delays last year"* | 1,685 | **0.770** | 0.615 |
| comparative — needs **both** facts in the window | 216 | **0.505** | — |
| unanswerable — the corpus has no answer | 862 | separability AUC **0.629** | — |

The bottom two rows are weaknesses. Comparative questions half-fail: both required chunks arrive
in the top five only 50.5% of the time (61.6% at k=10) — chunking optimised for one answer per
query is not chunking optimised for a question that needs two. And unanswerable questions are
barely separable from answerable ones, AUC 0.629 against a 0.5 coin flip — a property of embedding
retrieval rather than of any extractor, but one every RAG pipeline inherits and no comparison in
this field reports. "Noisy" means two keystroke edits plus lowercasing and stripped accents — a
mild perturbation; robustness to heavily mangled input is untested. Provenance:
`bench/run_query_mix.py`, `data/results/query-mix-pdf.json`, both in the research repository.

### What it costs to index

Tokens a caller pays per document, `cl100k_base`, same held-out corpus:

| | tokens / doc (PDF) | vs Diceo | `gold@5` |
|---|---:|---:|---:|
| **Diceo** | 10,433 | — | **0.845** |
| Kreuzberg | 9,853 | **0.94×** | 0.813 |
| Xberg | 10,086 | 0.97× | 0.817 |
| Unstructured | 10,883 | 1.04× | 0.820 |
| MinerU | 13,684 | 1.31× | 0.793 |

Diceo is not the cheapest — Kreuzberg emits 6% fewer tokens. What the table supports is the
field's best recall at an ordinary token bill, and that MinerU's 1.31× buys nothing. A token count
alone rewards dropping content, so it is only ever printed beside the recall it bought.
Provenance: `bench/run_tokens.py` in the research repository, experiment 051.

### The same PDFs at five chunk-size targets

600 characters is one operating point, and it is the one where PyMuPDF4LLM leads. Swept over
five targets on the same held-out PDFs, paired within each size, the point estimate reverses
above 600 and stays reversed:

| target | **diceo** | `chars@5` | pymupdf4llm | `chars@5` | delta | p |
|---:|---|---:|---|---:|---|---|
| 300 | 0.847 | 1,120 | **0.869** | 1,411 | −2.2pp | 0.057 |
| 600 | 0.845 | 2,178 | **0.854** | 2,672 | −0.9pp | 0.543 |
| 1000 | **0.822** | 3,510 | 0.770 | 4,382 | **+5.2pp** | 0.0012 |
| 1800 | **0.765** | 6,215 | 0.719 | 7,861 | **+4.6pp** | 0.0118 |
| 3000 | **0.760** | 9,351 | 0.712 | 13,647 | **+4.8pp** | 0.0101 |

**Read this table down a column, not across it.** `gold@5` is not comparable between chunk
sizes: a top-5 at 600 spends 2,178 characters of context and a top-5 at 1800 spends 6,215, so a
smaller target gets more attempts inside a smaller budget and its recall is flattered by the
metric. What each row supports is the comparison between **two tools at the same size**, which
is paired and valid. The column does not support "600 beats 1800", and the experiment behind it
disowns that reading. `chars@5` is printed for the same reason: at every size, including the one
we lose, we reach our recall with 18–31% less text than PyMuPDF4LLM.

The largest delta is at 1000, not at the shipping default of 1800. That default is inherited
rather than chosen by measurement, and settling it across more than one corpus is open work.

**Provenance.** `bench/run_sizes.py` in the research repository, over the held-out PDFs
`data/holdout-23` and `data/holdout-41` — the same 44 documents and 776 queries as the PDF row
above — embedded with Nemotron-3-Embed-1B, paired McNemar within each size, so that only the
chunker's target differs. Experiment 023 (chunk size). One format and one rival: the per-format
picture is the table above, at 600 only.

### Chunk overlap

Repeating the last N characters of each chunk at the start of the next is standard advice in
every RAG guide. Measured against the same arm with overlap off, at a 600-character target,
`Limits(overlap_chars=200)` costs:

| format | queries | overlap 200, against overlap off |
|---|---:|---|
| pdf | 776 | **−8.1pp** (p < 0.0001) |
| pptx | 300 | **−4.3pp** (p = 0.0002) |
| docx | 476 | −0.2pp (p = 1.000) |
| xlsx | 316 | +0.0pp (p = 1.000) |

Not one positive result, which is why it is off by default. The mechanism is not the obvious
one: overlap does not add chunks, it makes each one about 39% longer at 200/600 by repeating its
neighbour's tail. The index gains near-duplicate text rather than coverage, every embedding is
diluted by content that belongs to its neighbour, and neighbouring chunks compete for the same
query. The problem overlap exists to solve — a fact cut in half by a boundary — is already
handled by not cutting mid-unit. It is implemented and callers who want it can have it; they
should know what it cost here.

**Provenance.** `bench/run_formats.py` in the research repository, over the same held-out
corpora `data/holdout-23` and `data/holdout-41` at a 600-character target, embedded with
Nemotron-3-Embed-1B, paired McNemar against our own arm with overlap off. Experiment 022 (all
formats, and overlap), result 2. One overlap value and one target were tested, so this is "never
helped here" rather than a proof about overlap in general.

### Markdown syntax

The field optimises markdown fidelity, and the public benchmarks score it. We have not found a
published measurement of whether an embedding model retrieves better from `## Heading` than from
the heading on a line of its own. Measured here with extraction held perfect and the chunker
held constant — one markdown oracle, chunked once by authored blocks, then rendered six ways, so
the only variable is the representation:

| representation | `gold@5` | against `flat` | win/loss | p |
|---|---|---|---:|---|
| `flat` — markers stripped, heading text kept | 0.786 | — | — | — |
| `markup` — markdown markers present | 0.791 | +0.5pp | 9/6 | 0.607 |
| `both` | 0.773 | −1.3pp | 26/33 | 0.435 |
| `breadcrumb` — title and heading path prepended | 0.753 | −3.3pp | 20/38 | 0.025 |
| `stripped_breadcrumb` | 0.722 | −6.4pp | 26/61 | 0.0002 |
| `stripped` — heading blocks removed | 0.681 | **−10.4pp** | 11/68 | <0.0001 |

**The syntax is worth nothing and the heading text is worth 10.4pp.** Removing the markers costs
a null result; removing the heading blocks costs a tenth of retrieval, because 12.8% of the
planted facts live in heading text. That is the whole argument for emitting structure without
markup, and it is one measurement on one corpus.

The README quotes this null as ~~+0.2pp at p = 1.000~~, which is the original session's figure,
7 wins against 6 losses. The re-measured run in the table above is the one to quote: **+0.5pp,
9/6, p = 0.607**. Both are null and nothing downstream moves, but p = 1.000 reads stronger than
p = 0.607 and the two are not interchangeable. At a 600-character target the sign flips — `flat`
0.868 against `markup` 0.863 — as point estimates only: that re-run's significance tests were
lost to a CUDA out-of-memory.

**Provenance.** `bench/run_retrieval.py` in the research repository, in its oracle-only mode at
an 1800-character target, over the earlier `data/corpus` build — 28 documents, 546 scored
queries, 734 chunks at a median of 1,328 characters — embedded with Nemotron-3-Embed-1B, paired
McNemar. Experiment 002 (does structure pay), appendix, which re-measured the original session
and reproduces it to within 0.5pp on every arm. This is the oldest and the most synthetic corpus
on this page.

### Real documents

Nine public documents nobody involved in this project wrote, 120 probes, authored from **blinded
extractions rotated across all five tools** and admitted only when the answer appears in at least
one tool's output. Zero probes are answerable by diceo alone.

| tool | probes | `gold@5` |
|---|---:|---|
| **diceo** | 120 | **0.892** |
| docling | 105 | 0.838 |
| markitdown | 120 | 0.825 |
| pymupdf | 120 | 0.708 |
| pymupdf4llm | 120 | 0.692 |

**Provenance.** `bench/real_docs.py` — extraction, blinded probe authoring, mechanical admission and
scoring in one harness — over nine public documents under `data/fixtures/public-office` and
`data/fixtures/public`. The interval below is `bench/clustered.py`, re-estimated from the per-probe
hits that run writes to `data/derived/real/per_probe.json`. Experiment 039 (retrieval on real
documents), with the matcher corrected by experiment 040 (metric audit) and every p-value
re-estimated by experiment 042 (clustered inference).

**Read the caveats before quoting this.** They are the reason it is trustworthy.

- The margin over PyMuPDF4LLM is **+20.0pp, 95% CI [+4.0, +38.0], p = 0.020** — a document-clustered
  bootstrap, not a per-probe test. Fifteen questions about one PDF are not fifteen independent
  trials; treating them as such gave p = 7e-05, and that figure is withdrawn.
- For the same reason, **120 probes behave like an effective sample of 33**. This is a small study.
- A claimed significant win over MarkItDown **was withdrawn**. The original matcher required a
  verbatim substring, which charged the tools that do *not* join wrapped lines for their line
  breaks. Under a whitespace-folded matcher MarkItDown rises from 0.733 to 0.825 and the difference
  is +6.7pp, not significant. Our own score did not move.
- Docling was excluded from the 585-page document (hours of layout models), so its 105 probes are
  not the same set. Unpaired, and not comparable with the rest.
- The probes were authored by a language model against real extractions. A domain expert would ask
  different questions.
- **A fact no tool extracted cannot be asked about.** Every score here overstates absolute coverage
  by an unknown amount. This is a fair comparison, not an absolute one, and no design fixes that.

### Where we are behind

- **Narrow tables.** Unrelated prose shares the chunk with table rows, and it costs us. Measured
  three independent times, in the same direction, on three different corpora. Unfixed. On the
  held-out DOCX corpus, recall on the `table_cell` queries is **0.462 against MarkItDown's
  0.769** — experiment 030 (docx mechanism), result 1, and the largest single component of the
  DOCX gap at the time. **That 0.462 is the pre-fix build**: it was recovered from experiment
  026's embedding cache and belongs to the arm that scored 0.792 overall on DOCX. DOCX has
  since moved to 0.855 and `table_cell` has *not* been re-measured, so this is the last
  measurement rather than the current state. 030 predicts 0.462 rising to 0.75–0.95 once
  foreign prose is kept out of a chunk carrying table rows — a prediction, not a number.
- **PDF on real documents**, 0.833 against 0.867 for three other tools — one probe of thirty, and it
  disagrees with the synthetic corpus where PDF is our strongest format.

---

## Speed

Held-out corpus, documents per second, one process per tool on a 6-core box, best of three, with the
1-minute load average gated below 3.0 **before every cell**. Median reported, never mean: garbage
collection and page-cache noise are one-sided, so a mean is a number no run produced.

| tool | pdf | docx | pptx | xlsx | cores | peak RSS |
|---|---|---|---|---|---|---|
| PyMuPDF *(AGPL)* | **68.63** | 41.15 | **64.41** | 0 chars | 1.0 | 55 MB |
| **diceo** | 44.69 | **50.44** | 41.23 | **43.17** | **1.0** | **28 MB** |
| MarkItDown | 1.44 | 3.78 | 10.14 | 1.46 | 1.4–3.6 | 196 MB |
| PyMuPDF4LLM *(AGPL)* | 0.26 | 0.21 | 0.36 | 0 chars | 5.5–5.9 | 622 MB |
| Docling | 0.076 | 1.12 | 4.50 | 3.76 | 1.0–3.8 | 4,351 MB |

**Provenance.** `bench/speed_formats.py`, one child process per cell, over the same held-out corpora
— `data/holdout-23` + `data/holdout-41`, 44 PDF, 24 DOCX, 16 PPTX, 16 XLSX — experiment 054
(speed formats remeasured), 2026-08-19, all 20 cells in one sitting, zero process failures. The
table is wall-clock documents per second; diceo uses 1.00 cores so wall and CPU-seconds agree.
~~The previous table mixed three sittings: diceo and MarkItDown from 036, PyMuPDF from 031,
PyMuPDF4LLM from 024.~~ MarkItDown's peak RSS was published as ~~210 MB~~ then ~~197 MB~~ and is
**196 MB** on PDF here (285 MB on PPTX, the high-water mark of the sitting).

**PyMuPDF is faster than us at flat text and we are not going to pretend otherwise.** It returns
text with no headings, no table structure and no diagnostics; against its *structured* output the
two are level. Recovering structure is worth +8.0pp of retrieval, measured, and that is the trade.

**Wall clock is not what you pay.** PyMuPDF4LLM took 167 seconds of wall time on those 44 PDFs and
**923 CPU-seconds**, because it quietly averages 5.53 cores. On an idle laptop that flatters it; an
indexing pipeline runs one worker per document on a box that is already busy, and there what you pay
is CPU. Per CPU-second it processed **937× fewer documents** than we did, in 22× the memory
(622 MB against 28 MB). Both sides of that ratio are experiment 054, same sitting —
~~879×~~ from 024 and ~~≈1,030×~~ from dividing 036 by 024 are superseded. The historical
`docs/assets/positioning.svg` plots those 054 cells; the current README hero uses the 0.1.1
refresh above, which omits tools whose cached text was unavailable for consistent rescoring.

**MarkItDown is measured on the same axis, and this is the ratio a batch caller pays.**
Documents per CPU-second, ours over theirs, by format:

| format | diceo : MarkItDown, per CPU-second |
|---|---|
| pdf | **42.6×** |
| xlsx | **40.7×** |
| docx | 26.5× |
| pptx | 14.6× |

Over the mixed corpus — all 100 documents, in the proportions the corpus itself has — it is
**33.95×**: 2.219 CPU-seconds against 75.331. **The headline stays a mixed-corpus figure with
that table beside it**, because the per-format spread runs from 14.6× to 42.6× and a reader who
took a single per-format number away from this page would be wrong about DOCX and PPTX — a
stated-scope decision, recorded in experiment 031 (throughput remeasured) and again in 036.

**Provenance.** The same 054 sitting as the wall-clock table above. Both arms were measured in
the same session, so this is a paired measurement. MarkItDown's core count per cell runs from
1.37 on PDF to 3.59 on PPTX, and that is the whole difference between these ratios and the
wall-clock table above.

Docling costs **591× our wall clock per document** on the 44 held-out PDFs — 13.2 s against
0.022 s, experiment 054, same sitting — and its default configuration downloads and initialises
OCR models unasked. Per CPU-second it is **0.020 docs/CPU-s at 3.80 cores**, peak RSS **4.35 GB**.
~~0.064 docs/CPU-s at assumed 1.0 cores~~ is withdrawn: that inverted 026's 15.7 s/document and
credited it with a core count nobody had measured.

Our PDF figure has been published three times before and all are superseded: ~~40.6~~, then
~~47.67~~, then ~~47.26~~, now **44.69** documents per CPU-second (experiment 054), which
reproduces 046's same-session re-time of 44.88 to 0.4%. An interim **53.2 doc/s was withdrawn
before publication** because it was arithmetic across two different corpora rather than a
measurement.

**Memory does not grow with document size.** Peak RSS is bounded by the reopen window, not by the
page count — PDFium caches every indirect object it parses for the document's lifetime and offers no
public purge, so closing and reopening every N pages is the only lever, and it is the shipping
default at N = 100:

| fixture | size | pages | peak RSS, `reopen_every=100` | peak RSS, window off |
|---|---:|---:|---|---|
| `ipcc-ar6-wg1` | 404 MB | 2,409 | **502 MB** | 1,163 MB |
| `paper-huge` | 208 MB | 1,500 | **173 MB** | 1,395 MB |

**Provenance.** `ipcc-ar6-wg1` is the real IPCC AR6 WG1 full report, downloaded by
`scripts/fetch_public_pdfs.py --only ipcc` to `data/fixtures/public/`; `paper-huge` is
`data/fixtures/paper-huge.pdf`, built by `scripts/make_pdf_fixtures.py` from one seed paper repeated
100 times. The growth curve is `bench/probe_memory.py`, and the table above was re-measured through
`diceo.pdf.extract.lines(reopen_every=N)` on an idle box in experiment 015 (bounded memory at
scale). The window costs **2–16%** of throughput, and *negative* on `paper-huge` at N = 300 — a
smaller object cache pays for itself in locality. The benefit is file-dependent: 8× on `paper-huge`,
2.2× on IPCC, which carries three times the text per window.

~~217 MB flat across 2,409 pages~~ — withdrawn. File sizes above are as experiment 015 prints them,
and they are MiB: the same two files are **217 MB** and **423 MB** in decimal MB. 217 MB is
therefore the **file size of the 1,500-page fixture**, transcribed into a peak-RSS claim about the
2,409-page one. No run produced it as an RSS figure, and the two documents it conflated have peaks
of 173 MB and 502 MB respectively.

---

## Robustness

Neither a speed nor a retrieval measurement, and **nothing in this section was timed** — every
figure here is a count, a character length or a peak resident set size. The questions are
whether anything crashes, hangs or raises something a caller cannot catch, and whether anything
disappears without a diagnostic.

**930 source documents and 565 deliberately broken ones.** Each was chunked to exhaustion in its
own subprocess, with a 300-second timeout and an 8 GiB address-space cap, so that a segfault, a
hang, a memory ceiling and an ordinary exception are distinguishable from one another and none
of them can take the sweep down with it. The mutants are built from **32 public-only seeds** —
truncated at five fractions, hit with 1, 16 and 512 random bit flips, zeroed in spans, stripped
of their magic bytes, spliced into themselves, and given seven lying extensions each, so that a
PDF arrives as `.docx` and a workbook as `.html`.

**Zero crashes, zero hangs, zero non-`DiceoError` exceptions.** On the real documents there
were no exceptions of any kind. Of the 565 mutants, 290 raised — 242 `CorruptDocument`, 48
`UnsupportedFormat` — and the remaining 275 returned partial output carrying a diagnostic that
says where reading stopped.

Four adversarial inputs were built by hand. Each was refused by an explicit guard that names
what it is refusing, rather than by exhausting a resource and surviving by luck, which is the
only version of this result worth reporting:

| input | outcome | peak RSS |
|---|---|---:|
| 4 GiB zip bomb | refused: declares 4.0 GB uncompressed, over a 2 GB per-part ceiling | 18 MB |
| billion laughs | refused: input amplification factor over the limit | 74 MB |
| XXE against `file:///etc/passwd` | undefined entity, not resolved, nothing leaked | — |
| 200,000-deep unclosed HTML | one chunk, no recursion error | 42 MB |

Data loss was then checked against the containers rather than against our own diagnostics: PPTX
slide parts counted straight out of the ZIP against `diagnostics.pages` (**0 mismatches across
141 decks**), `pypdfium2` page counts against the same field on every PDF (**0 mismatches**),
and every `w:t` and `a:t` run pulled out by zipfile and regex, summed, and compared against the
emitted character count across **295 DOCX and PPTX** — exactly one file fell below 0.75 recall,
**and that file set `lost_data=True`**, which is the contract working. The 404 MB IPCC PDF
peaked within a megabyte of the memory table above, from a completely different harness.

**Provenance.** Experiment 045 (corpus robustness sweep). **It names no script, and that is a
defect in it**: the harness was written in a scratchpad and deliberately not committed, so this
is the one result on this page that is reproducible only by rebuilding the thing that produced
it. The corpus census is re-derivable from `data/` in the research repository; the mutant seeds
are public fixtures and public benchmark documents only, and no client document was copied,
opened for mutation or written to the scratchpad.

**Read these caveats before quoting it.**

- **Six of the eleven supported formats have no real file in the corpus** — xls, xlsb, ods, csv,
  htm and eml were exercised by fixtures and by mutants alone. Three of those six are the
  formats most likely to arrive from an old system, and the memory finding below is a CSV
  finding derived entirely from synthetic input.
- The 930 sit alongside 1,627 derived text artifacts — other extractors' `.txt` output — which
  were run too. The larger total is not quoted here, because two-thirds of it is one code path
  and the number sounds stronger than it is.
- The mutants are synthetic. Bit flips and truncations are not a substitute for files produced
  by real broken software, which fails in structured, plausible-looking ways that random
  corruption never generates.
- **A single-row CSV is the input shape on which bounded memory does not hold.** Reading rows
  alone peaks at **6.46×** the file size, because the giant row exists five times at once, and
  `Limits.max_chars` is enforced after the first chunk is emitted, so a caller cannot bound it
  today. Left open deliberately: bounding a row group is real data loss, so it belongs behind a
  limit that is opted into rather than behind a default.
- The sweep found three defects of ours, two of them silent, all fixed the same day. Three more
  were found later by reading this project's own claims back against the source, which 3,122
  subprocesses did not find. A corpus sweep proves the code survives the documents you have.

---

## Licence

The reason this project exists. The default install is checked in CI, in a throwaway virtualenv
holding only the runtime dependencies, and the check reads the licence and NOTICE files the wheels
**bundle** — not merely their declared metadata, because a binary wheel can carry copyleft text its
own metadata never mentions.

The current default install is **6 distributions**, from which **33 bundled notice files** are read
and **2 copyleft mentions** are individually audited and accounted for. Both are the GNU-licensed
autotools plumbing inside ICU4C's notices (`aclocal.m4`, `config.guess`), both carry the Autoconf
exception, and neither file is installed. That audit is keyed by section, so a future build that
introduced a real copyleft component could not pass silently.

| tool | licence | usable in a closed-source product |
|---|---|---|
| **diceo** | **Apache-2.0** | **yes** |
| MarkItDown | MIT | yes |
| Docling | MIT | yes, at 600× the CPU |
| PyMuPDF / PyMuPDF4LLM | **AGPL-3.0** | no, without a commercial licence |

**Provenance.** The counts above are produced by `scripts/license_check.py` in this repository, run
as `just license-check` against a throwaway virtualenv built from the default dependency set — no
fixture corpus is involved, the wheels *are* the fixture. The licence column is each project's own
declared licence.

---

## What we will not claim

- **Not the fastest.** PyMuPDF is faster at flat text.
- **Not the most accurate on every format.** PyMuPDF4LLM edges us on synthetic PDF; three tools tie
  ahead of us on real PDFs by one probe.
- **Not a large study.** The real-document result has an effective sample size of 33.
- **Not proven on the documents you have.** Open corpora skew institutional — government, standards
  bodies, academia. Invoices, contracts and internal decks are under-represented, and those are what
  a commercial pipeline mostly sees.

The claim we do make is narrower and, we think, the useful one: **near the top on both speed and
retrieval quality at once, under a licence you can ship, with bounded PDF windows and incremental
chunk packing, and it tells you what it could not read.** Legacy spreadsheet native allocation
still depends on workbook size; large single rows also require memory proportional to that row.
