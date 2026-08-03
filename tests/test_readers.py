"""Do the new readers get the *content* right, not merely fail to crash?

``test_robustness.py`` proves nothing explodes. This file proves the four formats
added around the original PDF/OOXML core -- CSV, HTML, email, legacy spreadsheets --
recover what a retriever needs, and that the two features a caller reaches for
first (metadata, the CLI) behave.

Every assertion here is about something that was measured or reasoned about
elsewhere and would silently regress:

- a semicolon-separated export must not become one column (half of Europe's Excel);
- an HTML table row must arrive as one block, not one block per cell (016);
- a mail's attachment must appear in ``diagnostics`` (rule 3);
- the header line must be repeated into every row group (D6, the 0.528 vs 0.449).
"""

from __future__ import annotations

import io
import json
import pathlib
import subprocess
import sys

import pytest

import diceo
from diceo import Diagnostics, Limits
from tests import fixtures

# --------------------------------------------------------------------------- #
# CSV and friends
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("delimiter", "label"),
    [(",", "comma"), (";", "semicolon"), ("\t", "tab"), ("|", "pipe")],
)
def test_delimiter_is_measured_not_assumed(delimiter, label):
    """A reader that assumes ``,`` turns a European export into one column.

    And reports success while doing it, which is the failure mode worth a test.
    """
    payload = fixtures.tiny_csv(delimiter)
    report = Diagnostics()
    pieces = list(diceo.chunk(payload, name=f"{label}.csv", diagnostics=report))
    text = "\n".join(piece.text for piece in pieces)
    assert "EMEA" in text and "1200" in text
    # Three columns recovered, so the delimiter was found: the row group renders
    # cells separated, never as one glued field.
    assert "region" in text and "revenue" in text and "units" in text
    # Three fields per row, so the delimiter was found rather than the row being
    # swallowed whole. (The renderer joins cells with " | ", so the delimiter
    # character itself may legitimately appear in the output.)
    summary = next(p for p in pieces if "sheet_summary" in p.kinds)
    assert "Columns (3)" in summary.text, summary.text


def test_csv_repeats_the_header_into_every_row_group():
    """D6's one deliberate duplication, and the reason we beat MarkItDown on
    tabular data (0.528 vs 0.449, experiment 022). A row group without its header
    is unreadable to an embedder."""
    rows = [["region", "revenue"], *[[f"country{i}", str(i * 10)] for i in range(200)]]
    payload = ("\n".join(",".join(row) for row in rows)).encode()
    pieces = list(diceo.chunk(payload, name="big.csv", limits=Limits(target_chars=300)))
    groups = [piece for piece in pieces if "sheet_row" in piece.kinds]
    assert len(groups) > 3, "the fixture did not produce several row groups"
    for group in groups:
        assert group.text.startswith("region"), "a row group arrived without its header line"


def test_csv_quoting_is_the_stdlib_s_job():
    payload = b'name,note\n"Smith, John","said ""hello"" twice"\n'
    pieces = list(diceo.chunk(payload, name="quoted.csv"))
    text = "\n".join(piece.text for piece in pieces)
    assert "Smith, John" in text
    assert 'said "hello" twice' in text


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #


def test_html_drops_script_and_style_but_keeps_structure():
    blocks = list(diceo.extract(fixtures.tiny_html(), name="page.html"))
    kinds = {block.kind for block in blocks}
    text = "\n".join(block.text for block in blocks)

    assert "hidden" not in text, "script contents reached the index"
    assert "color:red" not in text, "stylesheet reached the index"
    assert "heading" in kinds, "h1 did not become a heading"
    assert "table_row" in kinds, "the table row was not recognised"
    assert any(block.kind == "heading" and block.level == 1 for block in blocks)


def test_html_table_row_is_one_block_not_one_per_cell():
    """The unit that 016 showed matters: a row split per cell is unreadable, and a
    cell on its own has no column name attached to it."""
    blocks = [
        b for b in diceo.extract(fixtures.tiny_html(), name="p.html") if b.kind == "table_row"
    ]
    assert blocks, "no table rows"
    assert any("EMEA" in b.text and "1200" in b.text for b in blocks), (
        f"cells were separated across blocks: {[b.text for b in blocks]}"
    )


def test_html_title_is_reported():
    report = Diagnostics()
    list(diceo.chunk(fixtures.tiny_html(), name="p.html", diagnostics=report))
    assert any(note.startswith("title=") for note in report.notes)


def test_html_entities_are_decoded():
    payload = b"<html><body><p>caf&eacute; &amp; 5 &lt; 10</p></body></html>"
    text = "\n".join(p.text for p in diceo.chunk(payload, name="e.html"))
    assert "café & 5 < 10" in text


def test_html_is_detected_without_a_doctype():
    payload = b"<html><body><p>bare</p></body></html>"
    assert diceo.sniff(payload) == "html"


# --------------------------------------------------------------------------- #
# email
# --------------------------------------------------------------------------- #


def test_email_subject_becomes_a_heading_and_envelope_survives():
    blocks = list(diceo.extract(fixtures.tiny_eml(attachment=False), name="m.eml"))
    assert blocks[0].kind == "heading"
    assert "Budget approval" in blocks[0].text
    text = "\n".join(block.text for block in blocks)
    assert "a@example.com" in text and "1200" in text


def test_email_attachment_is_reported_as_lost_data():
    """The most common enterprise document is a mail whose content is its
    attachment. Returning the covering note and calling it success is exactly the
    silent loss rule 3 exists to prevent."""
    report = Diagnostics()
    list(diceo.chunk(fixtures.tiny_eml(attachment=True), name="m.eml", diagnostics=report))
    assert report.lost_data, "an attachment was dropped without being reported"
    assert any("attachment=budget.pdf" in item for item in report.truncated)
    assert any("attachments=1" in note for note in report.notes)


def test_email_without_attachments_is_not_flagged():
    report = Diagnostics()
    list(diceo.chunk(fixtures.tiny_eml(attachment=False), name="m.eml", diagnostics=report))
    assert not report.lost_data


def test_html_only_email_body_is_read_as_html():
    import email.message

    message = email.message.EmailMessage()
    message["Subject"] = "Report"
    message["From"] = "a@example.com"
    message.set_content(
        "<html><body><h1>Results</h1><p>Revenue 1200</p><script>x=1</script></body></html>",
        subtype="html",
    )
    text = "\n".join(p.text for p in diceo.chunk(message.as_bytes(), name="h.eml"))
    assert "Revenue 1200" in text
    assert "x=1" not in text, "script inside an HTML mail body reached the index"


# --------------------------------------------------------------------------- #
# legacy spreadsheets
# --------------------------------------------------------------------------- #


def test_ods_reads_through_the_same_chunker_as_xlsx():
    ods = [p.text for p in diceo.chunk(fixtures.tiny_ods(), name="a.ods")]
    xlsx = [p.text for p in diceo.chunk(fixtures.tiny_xlsx(), name="a.xlsx")]
    # Same rows, same chunking rules -- so the same values must come out, whatever
    # the container. A caller must not get different retrieval for `Save As`.
    assert any("EMEA" in text and "1200" in text for text in ods)
    assert len(ods) == len(xlsx)


def test_ods_notes_that_it_cannot_stream():
    """The one place diceo breaks rule 2. It says so rather than hiding it."""
    report = Diagnostics()
    list(diceo.chunk(fixtures.tiny_ods(), name="a.ods", diagnostics=report))
    assert any("no streaming reader" in note for note in report.notes)


# --------------------------------------------------------------------------- #
# caller metadata
# --------------------------------------------------------------------------- #


def test_caller_metadata_reaches_every_chunk():
    meta = {"tenant": "acme", "acl": ["team-a"], "crawled_at": "2026-07-29"}
    pieces = list(diceo.chunk(fixtures.tiny_pdf(), name="r.pdf", meta=meta))
    assert pieces
    for piece in pieces:
        assert piece.meta["tenant"] == "acme"
        assert piece.meta["acl"] == ["team-a"]


def test_caller_metadata_wins_over_ours():
    pieces = list(diceo.chunk(fixtures.tiny_pdf(), name="r.pdf", meta={"title": "Real Title"}))
    assert pieces[0].meta["title"] == "Real Title"


def test_doc_id_overrides_the_file_stem():
    pieces = list(diceo.chunk(fixtures.tiny_pdf(), name="r.pdf", doc_id="DOC-42"))
    assert all(piece.doc_id == "DOC-42" for piece in pieces)
    assert pieces[0].meta["doc_id"] == "DOC-42"


def test_metadata_is_held_by_reference_not_copied():
    """A million-row spreadsheet must cost one dict, not a million.

    Asserted by identity because the alternative -- copying -- is invisible until
    somebody indexes a big workbook and wonders where the memory went.
    """
    meta = {"tenant": "acme"}
    pieces = list(diceo.chunk(fixtures.tiny_xlsx(), name="a.xlsx", meta=meta))
    assert len(pieces) > 1
    assert all(piece.extra is meta for piece in pieces)


def test_chunks_are_picklable_with_metadata():
    import pickle

    pieces = list(diceo.chunk(fixtures.tiny_pdf(), name="r.pdf", meta={"a": 1}))
    restored = pickle.loads(pickle.dumps(pieces))
    assert restored[0].meta == pieces[0].meta


def test_meta_is_json_serialisable():
    pieces = list(diceo.chunk(fixtures.tiny_xlsx(), name="a.xlsx", meta={"n": 1}))
    json.dumps([piece.meta for piece in pieces])


# --------------------------------------------------------------------------- #
# scanned pages: the distinction the whole diagnostics story rests on
# --------------------------------------------------------------------------- #


def test_image_only_page_is_reported_as_needing_ocr():
    report = Diagnostics()
    list(diceo.chunk(fixtures.image_only_pdf(), name="scan.pdf", diagnostics=report))
    assert report.pages_image_only == 1
    assert report.needs_ocr
    assert report.lost_data
    assert any("OCR" in item for item in report.truncated)


def test_blank_page_is_not_mistaken_for_a_scan():
    """A separator page has nothing to OCR, and sending a caller's OCR budget at
    whitespace is the false positive that would make the flag useless."""
    report = Diagnostics()
    list(diceo.chunk(fixtures.blank_pdf(), name="blank.pdf", diagnostics=report))
    assert report.pages_without_text == 1
    assert report.pages_image_only == 0
    assert not report.needs_ocr


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #


def _cli(*args: str, expect: int | None = None) -> subprocess.CompletedProcess:
    done = subprocess.run(
        [sys.executable, "-m", "diceo", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if expect is not None:
        assert done.returncode == expect, f"{done.returncode}: {done.stderr[:400]}"
    return done


def test_cli_emits_one_json_object_per_chunk(tmp_path):
    path = tmp_path / "r.pdf"
    path.write_bytes(fixtures.tiny_pdf())
    done = _cli(str(path), expect=0)
    lines = [line for line in done.stdout.splitlines() if line.strip()]
    assert lines
    for line in lines:
        record = json.loads(line)
        assert "text" in record and "index" in record


def test_cli_reports_incomplete_with_exit_code_2(tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(fixtures.image_only_pdf())
    done = _cli(str(path), expect=2)
    assert "incomplete" in done.stderr


def test_cli_names_an_unsupported_format_and_exits_1(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
    done = _cli(str(path), expect=1)
    assert "Word" in done.stderr


def test_cli_sniff_names_the_format(tmp_path):
    path = tmp_path / "mystery"
    path.write_bytes(fixtures.tiny_docx())
    done = _cli(str(path), "--sniff", expect=0)
    assert "docx" in done.stdout


def test_cli_meta_is_attached(tmp_path):
    path = tmp_path / "r.pdf"
    path.write_bytes(fixtures.tiny_pdf())
    done = _cli(str(path), "--meta", "tenant=acme", "--meta", "n=7", expect=0)
    record = json.loads(done.stdout.splitlines()[0])
    assert record["tenant"] == "acme"
    assert record["n"] == 7, "a numeric value should stay a number in the index"


def test_cli_with_no_arguments_prints_help(tmp_path):
    done = _cli(expect=64)
    assert "usage" in done.stderr.lower()


@pytest.mark.parametrize(
    "args",
    [
        ("--target", "0", "report.pdf"),
        ("--meta", "missing-equals", "report.pdf"),
        ("--not-a-real-option",),
    ],
)
def test_cli_bad_usage_always_exits_64_without_a_traceback(args):
    done = _cli(*args, expect=64)
    assert done.stderr
    assert "Traceback" not in done.stderr


# --------------------------------------------------------------------------- #
# safe to parallelise -- the scale contract
# --------------------------------------------------------------------------- #


def test_chunking_works_inside_a_process_pool(tmp_path):
    """The deployment this library is designed for: one worker per document.

    Exercised for real rather than asserted, because "no global state" is easy to
    believe and easy to break with one module-level cache.
    """
    from concurrent.futures import ProcessPoolExecutor

    paths = []
    for index in range(6):
        path = tmp_path / f"doc{index}.pdf"
        path.write_bytes(fixtures.tiny_pdf([f"Document {index} revenue 1200"]))
        paths.append(str(path))

    with ProcessPoolExecutor(max_workers=3) as pool:
        counts = list(pool.map(_count_chunks, paths))
    assert all(count > 0 for count in counts)


def _count_chunks(path: str) -> int:
    return sum(1 for _ in diceo.chunk(path))


def test_chunking_works_from_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "r.pdf"
    path.write_bytes(fixtures.tiny_pdf())
    expected = [piece.text for piece in diceo.chunk(path)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: [p.text for p in diceo.chunk(path)], range(8)))
    assert all(result == expected for result in results), (
        "concurrent reads disagreed -- something is sharing state"
    )


def test_islice_really_stops_early(tmp_path):
    """Laziness with a stopwatch: reading 2 chunks of a 200-page document must not
    read 200 pages (D1). A generator that is lazy in shape but eager in practice
    is the most common way this promise is broken."""
    from itertools import islice

    path = tmp_path / "big.pdf"
    path.write_bytes(fixtures.tiny_pdf(pages=120))
    report = Diagnostics()
    got = list(islice(diceo.chunk(path, diagnostics=report), 2))
    assert len(got) == 2
    assert report.pages < 120, f"read {report.pages} pages to produce 2 chunks"


def test_extract_and_chunk_agree_on_content(tmp_path):
    payload = fixtures.tiny_docx(["Heading", "Body with 1200 in it."])
    blocks = " ".join(b.text for b in diceo.extract(payload, name="d.docx"))
    chunks = " ".join(c.text for c in diceo.chunk(payload, name="d.docx"))
    for word in ("Heading", "1200"):
        assert word in blocks and word in chunks


def test_stream_input_is_not_consumed_twice(tmp_path):
    """A handle handed to us twice must give the same answer both times."""
    path = tmp_path / "r.pdf"
    path.write_bytes(fixtures.tiny_pdf())
    with path.open("rb") as handle:
        first = [piece.text for piece in diceo.chunk(handle)]
        second = [piece.text for piece in diceo.chunk(handle)]
    assert first == second


def test_bytesio_position_does_not_matter():
    buffer = io.BytesIO(fixtures.tiny_pdf())
    buffer.seek(17)  # a caller who peeked
    pieces = list(diceo.chunk(buffer, name="r.pdf"))
    assert pieces


# --------------------------------------------------------------------------- #
# limits: a budget that bites must be reported, never performed quietly
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("budget", [0.05, 0.15])
def test_max_seconds_is_honoured_and_reported(tmp_path, budget):
    """The limit a caller draining a queue actually needs.

    They know they can spend two seconds on a document; they do not know which of
    ten thousand documents is the 2,400-page one. Checked per block rather than per
    chunk, because a document can spend a long time producing one chunk and a check
    that only ran on emit would overshoot on exactly the pathological case.
    """
    import time

    path = tmp_path / "big.pdf"
    path.write_bytes(fixtures.tiny_pdf(["Revenue was 1200 million."] * 40, pages=400))

    unbounded = Diagnostics()
    start = time.perf_counter()
    everything = list(diceo.chunk(path, diagnostics=unbounded))
    full = time.perf_counter() - start
    if full < budget * 2:
        pytest.skip(f"fixture is too fast ({full:.3f}s) to test a {budget}s budget")

    report = Diagnostics()
    start = time.perf_counter()
    got = list(diceo.chunk(path, limits=Limits(max_seconds=budget), diagnostics=report))
    elapsed = time.perf_counter() - start

    assert len(got) < len(everything), "the budget did not bite"
    assert got, "a budget that bites should still return what was already read"
    assert any("max_seconds" in item for item in report.truncated), (
        "work was cut short without being reported -- rule 3"
    )
    assert report.lost_data
    # Generous upper bound: the assertion worth making is that it is bounded at all,
    # not that a shared CI runner hits a tight one.
    assert elapsed < budget * 4, f"{elapsed:.3f}s against a {budget}s budget"


def test_max_pages_reports_which_limit_bit(tmp_path):
    path = tmp_path / "big.pdf"
    path.write_bytes(fixtures.tiny_pdf(pages=40))
    report = Diagnostics()
    list(diceo.chunk(path, limits=Limits(max_pages=5), diagnostics=report))
    assert any("max_pages=5" in item for item in report.truncated)


def test_max_rows_reports_which_limit_bit():
    rows = [["region", "revenue"], *[[f"c{i}", str(i)] for i in range(500)]]
    payload = ("\n".join(",".join(row) for row in rows)).encode()
    report = Diagnostics()
    list(diceo.chunk(payload, name="big.csv", limits=Limits(max_rows=20), diagnostics=report))
    assert any("max_rows" in item for item in report.truncated)
    assert report.lost_data


def test_no_limit_means_no_truncation_reported():
    """The other half: a document read whole must not claim it lost anything."""
    report = Diagnostics()
    list(diceo.chunk(fixtures.tiny_pdf(), name="r.pdf", diagnostics=report))
    assert not report.truncated
    assert not report.lost_data


@pytest.mark.parametrize("suffix", sorted(fixtures.every_format()))
def test_every_format_reports_how_much_text_it_produced(suffix):
    """`chars` must mean the same thing for every format: characters delivered.

    It read 0 for DOCX and PPTX until `diceo file.docx --count` printed it in
    front of someone. A coverage number that is silently zero is worse than absent,
    because a caller checking "did this document make it into my index?" would read
    it as a loss.
    """
    payload = fixtures.every_format()[suffix]
    report = Diagnostics()
    pieces = list(diceo.chunk(payload, name=f"sample{suffix}", diagnostics=report))
    produced = sum(len(piece.text) for piece in pieces)
    assert report.chars > 0, f"{suffix} produced {produced} characters but reported 0"
    # Within an order of magnitude of what was actually emitted -- chunking repeats
    # table headers and drops whitespace, so they are not required to be equal.
    assert report.chars >= produced // 4


def test_chunk_counts_each_source_block_once() -> None:
    """``chunk()`` sent one diagnostics object through both the reader wrapper and
    ``chunk_blocks()``, and each layer incremented it. The two paragraphs therefore
    reported four source blocks."""
    payload = b"first paragraph\n\nsecond paragraph\n"
    extracted_report = Diagnostics()
    blocks = list(diceo.extract(payload, name="two.txt", diagnostics=extracted_report))
    chunked_report = Diagnostics()
    list(diceo.chunk(payload, name="two.txt", diagnostics=chunked_report))

    assert extracted_report.blocks == len(blocks) == 2
    assert chunked_report.blocks == len(blocks)


# --------------------------------------------------------------------------- #
# detection: content wins where content proves something, and only there
# --------------------------------------------------------------------------- #


def test_magic_bytes_beat_a_lying_extension():
    """A `.xlsx` that is really a `.docx` is common in the wild -- mail systems
    rename attachments and export scripts hardcode suffixes. Trusting the name is
    how a whole document goes missing while every log line says success."""
    assert diceo.sniff(fixtures.tiny_docx()) == "docx"
    for lie in ("report.xlsx", "report.pdf", "report.csv", "report.txt"):
        assert diceo.sniff(io.BytesIO(fixtures.tiny_docx())) == "docx", lie
    # And through the whole pipeline, not just the detector.
    report = Diagnostics()
    list(diceo.chunk(fixtures.tiny_xlsx(), name="actually.docx", diagnostics=report))
    assert report.format == "xlsx"


def test_a_text_extension_is_not_overridden_by_a_header_like_first_line():
    """The other half of the rule, and the one that is easy to get wrong.

    `From:`/`To:` lines are not proof of anything -- a pasted message, YAML front
    matter, or a config file produces them. Sniffing before the extension made a
    `.md` file parse as email, which a caller cannot debug from the outside.
    """
    payload = b"From: the desk of the CFO\nTo: all staff\n\nRevenue was 1200.\n"
    assert diceo.sniff(io.BytesIO(payload)) == "eml", "no extension: sniff decides"
    for name in ("notes.txt", "notes.md", "README.rst"):
        report = Diagnostics()
        list(diceo.chunk(payload, name=name, diagnostics=report))
        assert report.format == "text", f"{name} was read as {report.format}"


def test_an_eml_extension_is_honoured_even_for_an_odd_message():
    payload = b"Subject: only a subject\n\nbody 1200\n"
    report = Diagnostics()
    list(diceo.chunk(payload, name="m.eml", diagnostics=report))
    assert report.format == "eml"


# --------------------------------------------------------------------------- #
# the same contract against a real rasterized document, not a synthetic one
# --------------------------------------------------------------------------- #

_SCANNED = pathlib.Path("data/fixtures/paper-scanned.pdf")


@pytest.mark.skipif(not _SCANNED.exists(), reason="run `just fixtures`")
def test_a_real_scanned_pdf_reports_every_page_as_needing_ocr():
    """The flagship diagnostic, on a genuinely rasterized 15-page document.

    `fixtures.image_only_pdf()` is a one-page file we built to exercise the code
    path; this is a real PDF produced by rendering pages to bitmaps, which is what
    a scanner or a "print to PDF" of a photocopy actually emits. Every competitor
    returns zero characters here and reports success.
    """
    report = Diagnostics()
    chunks = list(diceo.chunk(_SCANNED, diagnostics=report))

    assert chunks == [], "a page of pixels produced text, which means the fixture is wrong"
    assert report.pages == 15
    assert report.pages_image_only == 15, (
        "image-only pages were not recognised, so a caller would see an empty success"
    )
    assert report.needs_ocr and report.lost_data
    detail = " ".join(report.truncated)
    assert "OCR" in detail and "15" in detail, detail
