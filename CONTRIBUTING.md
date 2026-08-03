# Contributing to diceo

## Setup

```bash
git clone https://github.com/gergopool/diceo && cd diceo
uv sync                       # uv, never pip -- the `dev` group comes with it
just check                    # tests, lint, format, licence gate -- must be green
just all-fixtures             # generated documents, for the measurement-heavy tests
```

The robustness and reader suites build their own documents from the standard library in
`tests/fixtures.py`, so a fresh clone can prove the contract holds before it generates
anything. `just all-fixtures` writes the rest into `data/`, which is gitignored; it downloads
one public arXiv paper as the PDF seed, verified against a pinned digest, and generates
everything else locally. Tests that need the evaluation corpus skip by name; they, and the
benchmarks, run in the parent research repository that carries this one as a submodule.

**Citations in comments.** `experiment 030, docx mechanism` and bare decision ids like `D12`
point into that research repository, which is not public. They are there so a claim in a
docstring can be traced rather than argued with, and you are not expected to have read them
to work here — if one is load-bearing for a review, ask and it gets quoted into the thread.

## The five rules

The long version is [docs/principles.md](docs/principles.md). One line each:

1. **Licence purity is a hard gate.** No GPL/AGPL/SSPL in the default install, enforced in CI.
2. **Stream, never materialise.** No document ever exists as one Python string.
3. **Never lose data silently.** Anything dropped is counted in `Diagnostics`.
4. **Retrieval quality is the only quality metric.** Prettier output that does not move
   `gold@k` is not an improvement.
5. **Measure before believing.** A number is measured here, saying where and on what, or
   labelled inherited and unverified.

## What a good pull request looks like

- **A bug fix** comes with the test that would have caught it.
- **A new format reader** adds a builder to `tests/fixtures.py` and an entry to
  `every_format()`, which subscribes it to the whole robustness suite. A reader that is not
  in there is not covered, however many tests it has of its own.
- **A performance change** comes with a measurement from a quiet box, reported as median and
  min rather than mean. The speed harness lives in the parent repository.
- **A retrieval change** comes with a run against the held-out corpora and a p-value, from the
  harness in the parent repository, with the comparison pre-registered before it runs.

The last two need corpora that are not public, so **you are not expected to produce them**.
Open the PR with what you can measure and say what you think it should move; the maintainer
runs the private gate and posts the numbers into the thread, win or lose. A change that turns
out neutral-and-faster still ships — rule 4 — and one that loses gets the losing table too.

## Things that get sent back

- A new public name without a decision recorded for it. The surface is small on purpose.
- Anything that makes the library import a benchmark dependency. Those include AGPL packages,
  which is why they live in the parent repository.
- A new default dependency. Two pure wheels are the whole runtime; argue a third in an issue
  first.
- A guess presented as a fact. "This should be faster" belongs in an issue, not a docstring.
- Catching `Exception` in a reader. `src/diceo/api.py` names the exceptions that mean "these
  bytes are not what they claim", and deliberately leaves out the ones that are our own bugs.
- Silent truncation of any kind, including a `top-N` in a script. If a limit applied, say what
  was dropped.

## Style

Python 3.11+, `uv` for everything, `just` for task entry points, `ruff` for lint and format at
96 columns. Type hints on everything public, and `py.typed` is shipped, so an IDE teaches the
API. Comments explain *why* generously and *what* almost never, because the what is in the
code.
