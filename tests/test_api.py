"""The public API's contract tests.

Until this file existed, ~3,900 lines of `src/diceo` were exercised by nothing: every
test in the suite reached in through the benchmark harness instead. So these tests
deliberately go through the *public* surface only -- `diceo.chunk`, `diceo.extract`,
`Limits`, `Diagnostics`, `sniff` -- because that is what a user gets and therefore what
has to hold.

The load-bearing test is :func:`test_no_planted_fact_is_lost`: it asserts that every
fact the corpus generator planted in a document's text layer comes back out through
`chunk()`. That is rule 3 as an assertion rather than an aspiration.
"""

from __future__ import annotations

import json
import subprocess
import sys
from itertools import islice
from pathlib import Path

import pytest

from diceo import Diagnostics, Limits, chunk, extract, sniff

CORPUS = Path("data/corpus")

# The skip is per test, not on the module. A module-level `pytestmark` used to turn
# this whole file off on any clone without the generated corpus -- including the four
# tests that never touch it, among them the one asserting a format we cannot read is
# refused and the one holding the import-cost budget. A fresh clone ran none of the
# public API's contract tests and said so in twenty-four skip lines, which is the
# shape of a hole rather than of a decision.
_NO_CORPUS = (
    "corpus not built -- `just all-fixtures` builds data/fixtures; "
    "data/corpus is generated in the research repository"
)


def _one(pattern: str) -> Path:
    matches = sorted(CORPUS.glob(pattern))
    if not matches:
        pytest.skip(_NO_CORPUS if not CORPUS.is_dir() else f"no {pattern} in corpus")
    return matches[0]


@pytest.fixture(scope="module")
def manifest() -> dict:
    path = CORPUS / "manifest.json"
    if not path.exists():
        pytest.skip(_NO_CORPUS)
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# sniff
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.pdf", "pdf"),
        ("*.docx", "docx"),
        ("*.pptx", "pptx"),
        ("*.xlsx", "xlsx"),
        ("*.md", "text"),
    ],
)
def test_sniff_identifies_every_format(pattern, expected):
    assert sniff(_one(pattern)) == expected


def test_sniff_trusts_content_over_extension(tmp_path):
    """A .xlsx that is really a .docx is common in the wild; the name must not win."""
    real = _one("*.docx")
    liar = tmp_path / "actually-a-docx.xlsx"
    liar.write_bytes(real.read_bytes())
    assert sniff(liar) == "docx"


def test_sniff_refuses_rather_than_guesses(tmp_path):
    junk = tmp_path / "mystery.bin"
    junk.write_bytes(b"\x00\x01\x02not a document")
    with pytest.raises(ValueError, match="unrecognised"):
        sniff(junk)


# --------------------------------------------------------------------------- #
# chunk
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("pattern", ["*.pdf", "*.docx", "*.pptx", "*.xlsx", "*.md"])
def test_chunk_produces_bounded_non_empty_chunks(pattern):
    path = _one(pattern)
    limits = Limits(target_chars=800)
    chunks = list(chunk(path, limits=limits))

    assert chunks, f"{path.name} produced no chunks"
    assert all(c.text.strip() for c in chunks), "an empty chunk was emitted"
    assert [c.index for c in chunks] == list(range(len(chunks))), "indices not dense"

    # A chunker that returns one chunk per document is not a chunker -- this is the
    # regression that silently invalidated a whole benchmark run (experiment 017).
    if sum(len(c.text) for c in chunks) > 4 * limits.target_chars:
        assert len(chunks) > 1, "whole document collapsed into one chunk"

    # Table groups repeat their header, so a group can exceed the target; nothing
    # should exceed it by a wide margin.
    worst = max(len(c.text) for c in chunks)
    assert worst <= limits.target_chars * 3, f"chunk of {worst} chars vs target 800"


def test_smaller_target_yields_more_chunks():
    """The target has to actually bite -- 23 of 33 chunks once ignored it."""
    path = _one("*.pdf")
    big = list(chunk(path, limits=Limits(target_chars=1800)))
    small = list(chunk(path, limits=Limits(target_chars=400)))
    assert len(small) > len(big)
    median_small = sorted(len(c.text) for c in small)[len(small) // 2]
    assert median_small <= 400 * 1.6, f"median {median_small} ignores a 400 target"


def test_headings_are_in_the_text_not_only_metadata():
    """Experiment 002: heading *text* is worth 10.6-12.3pp because 12.8% of facts
    live in headings. If a heading only reaches `title`, the answer is not indexed."""
    path = _one("*.md")
    chunks = list(chunk(path))
    with_heading = [c for c in chunks if "heading" in c.kinds]
    assert with_heading, "no chunk carried a heading"
    sample = with_heading[0]
    assert sample.title
    assert sample.title.split()[0] in sample.text or sample.text.startswith(sample.title)


# --------------------------------------------------------------------------- #
# rule 3 -- the contract that matters
# --------------------------------------------------------------------------- #


def test_no_planted_fact_is_lost(manifest):
    """Every fact planted in a text layer must survive extraction *and* chunking.

    Chunking is included on purpose: a fact split across two chunks is a fact no
    retriever will find, so testing the extractor alone would pass while the product
    fails.
    """
    documents = {d["doc_id"]: d for d in manifest["documents"]}
    missing: list[str] = []
    checked = 0

    for suffix in ("pdf", "docx", "pptx", "md"):
        for path in sorted(CORPUS.glob(f"*.{suffix}"))[:3]:
            doc = documents.get(path.stem)
            if doc is None:
                continue
            haystack = "\n".join(c.text for c in chunk(path))
            for fact in doc["facts"]:
                if not fact["answerable_in_text"]:
                    continue
                checked += 1
                if fact["answer"] not in haystack:
                    missing.append(f"{path.name}:{fact['carrier']}:{fact['answer']!r}")

    assert checked > 50, f"only {checked} facts checked -- corpus may be wrong"
    assert not missing, f"{len(missing)}/{checked} facts lost: {missing[:8]}"


def test_image_only_facts_do_not_leak(manifest):
    """The control the retrieval benchmark depends on: facts planted only in figure
    pixels must appear in no text layer. If they leak, every measured ceiling is wrong.
    """
    documents = {d["doc_id"]: d for d in manifest["documents"]}
    leaked: list[str] = []
    for path in sorted(CORPUS.glob("*.pdf"))[:4]:
        doc = documents.get(path.stem)
        if doc is None:
            continue
        haystack = "\n".join(c.text for c in chunk(path))
        for fact in doc["facts"]:
            if not fact["answerable_in_text"] and fact["answer"] in haystack:
                leaked.append(f"{path.name}:{fact['answer']!r}")
    assert not leaked, f"image-only facts leaked into text: {leaked[:5]}"


# --------------------------------------------------------------------------- #
# limits and diagnostics
# --------------------------------------------------------------------------- #


def test_max_pages_is_honoured_and_reported():
    path = _one("*.pdf")
    report = Diagnostics()
    limited = list(chunk(path, limits=Limits(max_pages=2), diagnostics=report))
    full = list(chunk(path))
    assert len(limited) < len(full)
    assert report.truncated, "a limit bit without being recorded (rule 3)"
    assert report.lost_data


def test_max_chars_is_honoured_and_reported():
    path = _one("*.pdf")
    report = Diagnostics()
    chunks = list(chunk(path, limits=Limits(max_chars=1500), diagnostics=report))
    assert chunks
    assert sum(len(c.text) for c in chunks) < 6000
    assert any("max_chars" in note for note in report.truncated)


def test_diagnostics_counts_pages_and_blocks():
    report = Diagnostics()
    list(chunk(_one("*.pdf"), diagnostics=report))
    assert report.pages > 0
    assert report.blocks > 0
    assert report.chunks > 0
    assert report.as_dict()["pages"] == report.pages


def test_no_text_layer_is_reported_not_swallowed():
    """A scanned PDF returning nothing is the single worst silent failure in this
    domain -- every extractor surveyed 'succeeds' on one."""
    scanned = Path("data/fixtures/paper-scanned.pdf")
    if not scanned.exists():
        pytest.skip("scanned fixture not downloaded")
    report = Diagnostics()
    chunks = list(chunk(scanned, diagnostics=report))
    assert report.pages_without_text > 0, "silent failure on a scanned PDF"
    assert report.lost_data
    assert len(chunks) < 3


# --------------------------------------------------------------------------- #
# streaming and footprint
# --------------------------------------------------------------------------- #


def test_extract_is_lazy():
    """D1: islice must not read the whole document."""
    path = _one("*.pdf")
    report = Diagnostics()
    first = list(islice(extract(path, diagnostics=report), 3))
    assert len(first) == 3
    full = Diagnostics()
    list(extract(path, diagnostics=full))
    assert report.blocks < full.blocks, "generator was drained eagerly"


def test_extract_refuses_spreadsheets_with_a_reason():
    with pytest.raises(ValueError, match="chunk"):
        list(extract(_one("*.xlsx")))


def test_import_is_cheap():
    """D12: diceo is imported in thousands of worker processes, so the import must
    stay in the tens of milliseconds -- ~16 ms measured on the developer machine.

    The ceiling is 1 s rather than anything near that measurement on purpose. What
    this test can catch is a *class* change -- a module-scope `import pandas`, a model
    loaded at import, a network call -- and every one of those costs seconds. What it
    cannot catch is drift, because a cold, contended CI runner is entitled to be an
    order of magnitude slower than a warm laptop, and a threshold that treats a busy
    runner as a regression gets muted long before it ever catches one.
    """
    code = "import time;t=time.perf_counter();import diceo;print(time.perf_counter()-t)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    elapsed = float(out.stdout.strip())
    assert elapsed < 1.0, f"import diceo took {elapsed * 1000:.0f} ms"


def test_chunks_are_picklable():
    """D12: the caller owns parallelism, so chunks cross process boundaries."""
    import pickle

    chunks = list(islice(chunk(_one("*.pdf")), 3))
    restored = pickle.loads(pickle.dumps(chunks))
    assert [c.text for c in restored] == [c.text for c in chunks]
    assert restored[0].locator == chunks[0].locator
