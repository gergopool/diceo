"""Build the DOCX and PPTX speed fixtures, big enough that per-document cost
stops mattering.

The real documents we measure on are ~100 KB of `document.xml` and 38 slides -- at
that size a run is milliseconds and zip-open overhead is a visible share of it, so a
difference between two extraction strategies cannot be read off. These two fixtures
are sized so per-*paragraph* and per-*slide* cost dominates:

  big.docx
      ~5,000 paragraphs, headings at four levels, 25 tables
  wordy.docx
      same text, but every paragraph split into many formatted runs
  big.pptx
      ~500 slides, each a title placeholder + body text box, table on every third
      slide

`wordy.docx` exists because `big.docx` turned out to be a *bad* discriminator:
python-docx emits one `w:r` per paragraph, so a per-node parser and a per-run
scanner do almost the same amount of work on it. Word does not write files like
that -- it splits runs at every formatting, spell-check and revision boundary, so
a real paragraph carries 5-20 runs each with its own `w:rPr`. Run density is
therefore the axis the whole "DOM vs scanner" question turns on, and it needs its
own fixture rather than an assumption. (The density is emulated, not copied from a
real Word file; what is measured is the *trend* against node count.)

Written with python-docx / python-pptx from the `dev` extra: write-only
libraries, dev-time only. They are also the *baseline* the byte scanner is measured
against by the OOXML probe in the research repository, so having them produce the
fixture keeps the file shape honest -- it is exactly the XML those libraries emit.

Both files deliberately carry the traps that silently corrupt text:
XML-escaping (`& < > " '`), a non-breaking space as a numeric reference, runs that
need `xml:space="preserve"`, and non-ASCII (multi-byte UTF-8) so a byte-level
scanner cannot get away with latin-1 assumptions::

    uv run --group dev python scripts/make_ooxml_fixtures.py
"""

from __future__ import annotations

import argparse
import random
import zipfile
from pathlib import Path

FIXTURES = Path("data/fixtures")

# Sentences are assembled from this pool so the fixture text compresses like prose
# rather than like one repeated string -- a zip full of identical paragraphs would
# make the decompression side of the measurement unrealistically cheap.
_WORD_POOL = (
    "throughput latency backlog dwell utilisation cadence variance tolerance "
    "shipment consignment terminal depot corridor gantry reefer chiller pallet "
    "manifest waybill audit baseline forecast restated provisional escalation "
    "Ashcombe Barrowfield Calderhythe Elsmere Fenwick Harlow Meridian Braemar"
)
WORDS = _WORD_POOL.split()

# The escaping traps. Each appears verbatim in the fixture text, so a scanner that
# mishandles one produces a diff instead of quietly losing a character.
TRAPS = (
    "R&D spend rose 4% — “materially” — versus <plan> at >99% uptime",
    "Cost per TEU was €1,204.55 (± 3 %), per the operator's own model",
    'Query used: SELECT * FROM dwell WHERE site="FEN-08" AND q<=3 AND rate>0.5',
)


def sentence(rng: random.Random) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(rng.randint(9, 18))).capitalize() + "."


def paragraph_text(rng: random.Random) -> str:
    return " ".join(sentence(rng) for _ in range(rng.randint(3, 6)))


def add_runs(paragraph, text: str, per_run: int, rng: random.Random) -> None:
    """Split one paragraph across many runs, each carrying its own w:rPr.

    Emulates what Word actually writes. `per_run=0` means one run, i.e. what
    python-docx does on its own.
    """
    words = text.split()
    step = per_run or len(words)
    for start in range(0, len(words), step):
        run = paragraph.add_run(" ".join(words[start : start + step]) + " ")
        # Any property at all forces lxml to write a <w:rPr> subtree, which is the
        # node count a DOM parser has to allocate and a scanner skips over.
        run.font.name = "Calibri"
        if rng.random() < 0.15:
            run.bold = True
        if rng.random() < 0.1:
            run.italic = True


def build_docx(out: Path, paragraphs: int, tables: int, seed: int, per_run: int = 0) -> None:
    from docx import Document

    rng = random.Random(seed)
    document = Document()
    every_table = max(1, paragraphs // tables)

    for index in range(paragraphs):
        if index % 250 == 0:
            document.add_heading(f"{index // 250 + 1}. {sentence(rng)[:60]}", level=1)
        elif index % 60 == 0:
            document.add_heading(f"{sentence(rng)[:48]}", level=2)
        elif index % 137 == 0:
            document.add_heading(f"{sentence(rng)[:40]}", level=3)
        elif index % 311 == 0:
            document.add_heading(f"{sentence(rng)[:36]}", level=4)

        if index % 17 == 0:
            # Two runs where the first ends in a space: python-docx then has to
            # write xml:space="preserve", which is the case a naive scanner drops.
            body = document.add_paragraph()
            body.add_run(TRAPS[index % len(TRAPS)] + " ")
            body.add_run(paragraph_text(rng))
        elif index % 7 == 0:
            style = "List Bullet"
            if per_run:
                add_runs(document.add_paragraph(style=style), paragraph_text(rng), per_run, rng)
            else:
                document.add_paragraph(paragraph_text(rng), style=style)
        elif per_run:
            add_runs(document.add_paragraph(), paragraph_text(rng), per_run, rng)
        else:
            document.add_paragraph(paragraph_text(rng))

        if index and index % every_table == 0:
            table = document.add_table(rows=12, cols=5)
            table.style = "Table Grid"
            for row in range(12):
                for col in range(5):
                    cell = table.cell(row, col)
                    if row == 0:
                        cell.text = f"Metric {col}" if col else "Site"
                    else:
                        cell.text = (
                            f"{rng.choice(WORDS)}-{row:02d}"
                            if col == 0
                            else f"{rng.uniform(0, 1000):.4f}"
                        )

    document.save(str(out))


def build_pptx(out: Path, slides: int, seed: int) -> None:
    from pptx import Presentation
    from pptx.util import Emu, Pt

    rng = random.Random(seed)
    presentation = Presentation()
    title_only = presentation.slide_layouts[5]
    blank = presentation.slide_layouts[6]
    width = presentation.slide_width

    for index in range(slides):
        # Every third slide uses a real title placeholder, the rest a bare text
        # box: both shapes occur in the wild and only the first one carries
        # `p:ph type="title"`, which is the only honest signal for slide_title.
        if index % 3 == 0:
            slide = presentation.slides.add_slide(title_only)
            slide.shapes.title.text = f"{index + 1}. {sentence(rng)[:55]}"
        else:
            slide = presentation.slides.add_slide(blank)
            box = slide.shapes.add_textbox(
                Emu(457200), Emu(365760), width - Emu(914400), Pt(40)
            )
            box.text_frame.text = f"{index + 1}. {sentence(rng)[:55]}"

        body = slide.shapes.add_textbox(Emu(457200), Emu(1200000), width - Emu(914400), Pt(200))
        frame = body.text_frame
        frame.word_wrap = True
        frame.text = TRAPS[index % len(TRAPS)]
        for _ in range(rng.randint(4, 7)):
            frame.add_paragraph().text = sentence(rng)

        if index % 3 == 1:
            shape = slide.shapes.add_table(
                8, 4, Emu(457200), Emu(3400000), width - Emu(914400), Pt(140)
            )
            for row in range(8):
                for col in range(4):
                    cell = shape.table.cell(row, col)
                    if row == 0:
                        cell.text = f"Q{col} 2023" if col else "Site"
                    else:
                        cell.text = (
                            f"{rng.choice(WORDS)}-{row:02d}"
                            if col == 0
                            else f"{rng.uniform(0, 1000):.4f}"
                        )

    presentation.save(str(out))


def report(path: Path, parts: tuple[str, ...]) -> None:
    """The MB/s denominator is *decompressed part* bytes, not file size -- the zip
    is mostly styles, themes and media that no text extractor reads."""
    with zipfile.ZipFile(path) as archive:
        sizes = [
            info.file_size
            for info in archive.infolist()
            if any(info.filename.startswith(prefix) for prefix in parts)
        ]
    print(
        f"  {path.name}: {path.stat().st_size / 1e6:.1f} MB zipped, "
        f"{sum(sizes) / 1e6:.1f} MB of text XML in {len(sizes)} part(s)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paragraphs", type=int, default=5_000)
    parser.add_argument("--tables", type=int, default=25)
    parser.add_argument("--slides", type=int, default=500)
    parser.add_argument(
        "--words-per-run",
        type=int,
        default=4,
        help="run density for wordy.docx; 4 gives ~13 runs/paragraph",
    )
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--force", action="store_true", help="rebuild even if present")
    args = parser.parse_args()

    FIXTURES.mkdir(parents=True, exist_ok=True)
    print("building OOXML fixtures:")

    docx = FIXTURES / "big.docx"
    if docx.exists() and not args.force:
        print(f"  {docx.name} exists, skipping")
    else:
        build_docx(docx, args.paragraphs, args.tables, args.seed)
    report(docx, ("word/document.xml",))

    wordy = FIXTURES / "wordy.docx"
    if wordy.exists() and not args.force:
        print(f"  {wordy.name} exists, skipping")
    else:
        build_docx(wordy, args.paragraphs, args.tables, args.seed, args.words_per_run)
    report(wordy, ("word/document.xml",))

    pptx = FIXTURES / "big.pptx"
    if pptx.exists() and not args.force:
        print(f"  {pptx.name} exists, skipping")
    else:
        build_pptx(pptx, args.slides, args.seed)
    report(pptx, ("ppt/slides/slide",))


if __name__ == "__main__":
    main()
