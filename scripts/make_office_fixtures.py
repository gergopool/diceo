"""Build the Office fixtures the office fast-path probes need.

Five shapes, each chosen because it breaks a different naive assumption:

  sheet-small.xlsx
      10k rows x 20 cols, shared-string table on -- the shape a SQL-to-Excel
      export actually has. The per-cell throughput fixture.
  sheet-big.xlsx
      1M rows x 12 cols (``--big-rows`` to shrink). The O(1)-memory fixture:
      any backend that materialises this fails the bounded-memory acceptance
      criterion outright, and the file is deliberately larger than the 300 MB
      peak-RSS budget once it is expanded into Python objects.
  sheet-messy.xlsx
      four sheets, and *not one of them* is a clean rectangle: a three-row
      merged header with grouped super-columns, two tables side by side on one
      sheet, a report that starts at row 7 under a merged title block, and a
      sheet that is only prose. This is the fixture that decides whether the D6
      chunker's header detection is honest about its own uncertainty.
  doc-long.docx
      ~200 pages equivalent with real Heading1..3 styles, tables, bulleted and
      numbered lists -- then post-processed to add the four things python-docx
      cannot author and every naive XML scanner gets wrong: **footnotes and
      endnotes in separate parts**, ``w:instrText`` field codes, a hyperlink
      run, and ``w:br``. See ``_inject_docx_hard_cases``.
  deck-large.pptx
      100 slides carrying 20 tables, 25 sets of speaker notes and 40 pictures,
      of which 20 slides hold a picture and *no text at all* -- the low-text-slide
      case that an inherited, unverified finding says leaves half a real deck
      unretrievable.

Every fixture is seeded, so two runs produce identical *content*. The files are not
byte-identical: a zip records each entry's mtime, so the archives differ even when
every part inside them matches::

    uv run --group dev python scripts/make_office_fixtures.py
    uv run --group dev python scripts/make_office_fixtures.py --big-rows 300000
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import shutil
import struct
import zipfile
import zlib
from pathlib import Path

FIXTURES = Path("data/fixtures")

# --------------------------------------------------------------------------- #
# Shared synthetic vocabulary.
#
# The cardinality ratio is the point: a sheet of all-distinct floats flatters a
# byte scanner (nothing to intern) and a sheet of one repeated word flatters the
# shared-string table. Real exports sit between, so these do too.
# --------------------------------------------------------------------------- #

SITES = [
    "Barrowfield Hub",
    "Calderhythe Terminal",
    "Dunmoor Cold Store",
    "Elsmere Depot",
    "Fenwick Yard",
    "Garrowmere Plant",
    "Harlowe Distribution",
    "Inglefield Works",
    "Jarrowmoor Depot",
    "Kelsterbank Hub",
    "Lyndhurst Terminal",
    "Marchmont Store",
]
METRICS = [
    "water reuse ratio",
    "solar self-consumption",
    "inbound dwell time",
    "cold chain excursion rate",
    "pallet utilisation",
    "diesel intensity",
    "grid import share",
    "waste diversion rate",
    "on-time despatch",
]
BASES = ["actual", "forecast", "restated"]
UNITS = ["%", "h", "kWh/t", "kg CO2e/t", "pallets/h"]
REGIONS = ["North", "South", "Midlands", "Wales", "Scotland"]
OWNERS = ["A. Fenn", "B. Okonjo", "C. Halloran", "D. Vermeer", "E. Ashworth"]


def _png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A minimal valid solid-colour PNG, built here so the fixtures need no
    binary asset checked into the repo (client bytes must never be committed)."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    scanline = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanline * height, 6))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# XLSX
# --------------------------------------------------------------------------- #

SMALL_HEADER = [
    "Site code",
    "Site",
    "Region",
    "Metric",
    "Basis",
    "Value",
    "Unit",
    "Observed",
    "Owner",
    "Reviewed",
    "Variance",
    "Target",
    "Prior year",
    "Weight",
    "Rank",
    "Flagged",
    "Comment",
    "Source system",
    "Batch",
    "Confidence",
]
# The 12-column fixture is the first 12 columns of the 20-column one, and it has to
# be *derived* rather than retyped: a hand-written 12-name list silently omitted
# "Region" and "Reviewed" while the rows still carried them, so every label in the
# big fixture was shifted against its own data and the sheet-summary chunk reported
# "Unit [number]" over the Value column. A fixture whose header lies about its data
# invalidates anything measured on it.
BIG_HEADER = SMALL_HEADER[:12]


def _cells(count: int, width: int, seed: int):
    """``count`` rows of ``width`` columns of plausible telemetry."""
    rng = random.Random(seed)
    epoch = dt.date(2024, 1, 1)
    for index in range(count):
        site = rng.randrange(len(SITES))
        row = [
            f"S{site:03d}",
            SITES[site],
            REGIONS[site % len(REGIONS)],
            METRICS[rng.randrange(len(METRICS))],
            BASES[rng.randrange(len(BASES))],
            round(rng.uniform(0, 100), 3),
            UNITS[rng.randrange(len(UNITS))],
            epoch + dt.timedelta(days=index % 900),
            OWNERS[rng.randrange(len(OWNERS))],
            rng.choice([True, False]),
            round(rng.uniform(-20, 20), 2),
            round(rng.uniform(50, 100), 1),
            round(rng.uniform(0, 100), 2),
            rng.randrange(1, 500),
            rng.randrange(1, 12),
            rng.choice(["yes", "no"]),
            f"note {rng.randrange(400)}",
            rng.choice(["SCADA", "manual", "ERP"]),
            f"B{rng.randrange(9000):04d}",
            rng.choice(["A", "B", "C"]),
        ]
        yield row[:width]


def write_small(out: Path, rows: int) -> None:
    import xlsxwriter

    book = xlsxwriter.Workbook(str(out), {"default_date_format": "yyyy-mm-dd"})
    sheet = book.add_worksheet("Observations")
    bold = book.add_format({"bold": True})
    sheet.write_row(0, 0, SMALL_HEADER, bold)
    for index, row in enumerate(_cells(rows, len(SMALL_HEADER), seed=11), start=1):
        sheet.write_row(index, 0, row)
    book.close()


def write_big(out: Path, rows: int) -> None:
    import xlsxwriter

    # constant_memory keeps the *writer* bounded; it also means inline strings
    # rather than a shared table, which is the harder case for a reader.
    book = xlsxwriter.Workbook(
        str(out), {"constant_memory": True, "default_date_format": "yyyy-mm-dd"}
    )
    sheet = book.add_worksheet("Observations")
    sheet.write_row(0, 0, BIG_HEADER)
    for index, row in enumerate(_cells(rows, len(BIG_HEADER), seed=13), start=1):
        sheet.write_row(index, 0, row)
    book.close()


def write_messy(out: Path) -> None:
    """The realistic mess. Four sheets, four different ways of not being a table."""
    import xlsxwriter

    book = xlsxwriter.Workbook(str(out))
    title = book.add_format({"bold": True, "font_size": 14, "align": "center"})
    group = book.add_format({"bold": True, "align": "center", "bg_color": "#DDDDDD"})
    leaf = book.add_format({"bold": True, "text_wrap": True})
    rng = random.Random(17)

    # 1. Three-row merged header with grouped super-columns.
    grouped = book.add_worksheet("Q3 Actuals")
    grouped.merge_range(0, 0, 0, 8, "Quarterly sustainability actuals - Q3 2024", title)
    grouped.merge_range(1, 0, 2, 0, "Site", leaf)
    grouped.merge_range(1, 1, 2, 1, "Region", leaf)
    grouped.merge_range(1, 2, 1, 4, "Water", group)
    grouped.merge_range(1, 5, 1, 7, "Energy", group)
    grouped.merge_range(1, 8, 2, 8, "Owner", leaf)
    for offset, name in enumerate(["Reuse %", "Intensity", "Variance"]):
        grouped.write(2, 2 + offset, name, leaf)
    for offset, name in enumerate(["Import share", "Solar %", "Variance"]):
        grouped.write(2, 5 + offset, name, leaf)
    for index, site in enumerate(SITES):
        grouped.write_row(
            3 + index,
            0,
            [
                site,
                REGIONS[index % len(REGIONS)],
                *[round(rng.uniform(0, 100), 2) for _ in range(6)],
                OWNERS[index % len(OWNERS)],
            ],
        )

    # 2. Two independent tables side by side, separated by a blank column.
    side = book.add_worksheet("Side by side")
    side.write_row(0, 0, ["Metric", "Value"], leaf)
    side.write_row(0, 3, ["Region", "Headcount", "Sites"], leaf)
    for index in range(9):
        side.write_row(1 + index, 0, [METRICS[index], round(rng.uniform(0, 100), 2)])
    for index, region in enumerate(REGIONS):
        side.write_row(1 + index, 3, [region, rng.randrange(40, 400), rng.randrange(1, 9)])

    # 3. A report whose data starts at row 7, under a merged title block.
    late = book.add_worksheet("Report")
    late.merge_range(0, 0, 1, 4, "Cold chain excursion report", title)
    late.write(2, 0, "Prepared by")
    late.write(2, 1, "C. Halloran")
    late.write(3, 0, "Period")
    late.write(3, 1, "2024-07-01 to 2024-09-30")
    late.write(4, 0, "Status")
    late.write(4, 1, "Final")
    late.write_row(6, 0, ["Site", "Excursions", "Longest (h)", "Basis", "Unit"], leaf)
    for index, site in enumerate(SITES):
        late.write_row(
            7 + index,
            0,
            [site, rng.randrange(0, 30), round(rng.uniform(0.5, 40), 1), "actual", "h"],
        )

    # 4. No table at all -- prose in column A. A header detector must not invent one.
    prose = book.add_worksheet("Notes")
    for index, line in enumerate(
        [
            "Methodology notes",
            "Water reuse ratio is measured at the site boundary and excludes rainwater "
            "harvesting, which is reported separately.",
            "Restated figures for Dunmoor Cold Store follow the 2024 meter replacement.",
            "Forecast basis rows are unaudited.",
        ]
    ):
        prose.write(index, 0, line)

    book.close()


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #

PARAGRAPHS = [
    "Water reuse at the site boundary is metered continuously and reconciled monthly "
    "against the potable import total, so a divergence larger than two per cent is "
    "investigated before the figure is published.",
    "The cold chain excursion rate counts every interval in which a probe reads above "
    "the licensed ceiling for longer than fifteen minutes, whether or not the load was "
    "subsequently rejected.",
    "Pallet utilisation is computed on despatched volume rather than on booked volume, "
    "which is why the ratio moves when a customer cancels late in the day.",
    "Diesel intensity is reported per tonne moved. Yard tractors and reach trucks are "
    "included; road haulage under a third-party contract is not.",
    "Grid import share fell after the second array was commissioned, but the winter "
    "months remain import-dominated and no storage is installed at this site.",
]


def write_doc_long(out: Path, sections: int) -> None:
    import docx
    from docx.enum.text import WD_BREAK

    document = docx.Document()
    document.add_heading("Sustainability operations manual", level=0)
    document.add_paragraph(
        "This manual is generated as a fixture. It exists to be parsed, not read."
    )
    rng = random.Random(19)

    for section in range(sections):
        document.add_heading(
            f"Section {section + 1}: {METRICS[section % len(METRICS)]}", level=1
        )
        for sub in range(3):
            document.add_heading(f"{section + 1}.{sub + 1} Measurement basis", level=2)
            for _ in range(2):
                document.add_paragraph(PARAGRAPHS[rng.randrange(len(PARAGRAPHS))])
            document.add_heading(f"{section + 1}.{sub + 1}.1 Exceptions", level=3)
            for item in range(3):
                document.add_paragraph(
                    f"Exception {item + 1} for {METRICS[(section + item) % len(METRICS)]}.",
                    style="List Bullet",
                )
            for item in range(3):
                document.add_paragraph(
                    f"Step {item + 1}: reconcile, restate, then re-publish.",
                    style="List Number",
                )
            # A paragraph carrying a hard line break: the naive scanner welds the
            # words either side of it into one token.
            paragraph = document.add_paragraph("Reported at the site boundary.")
            paragraph.add_run().add_break(WD_BREAK.LINE)
            paragraph.add_run("Excludes rainwater harvesting.")

            table = document.add_table(rows=4, cols=4)
            table.style = "Table Grid"
            for column, name in enumerate(["Site", "Basis", "Value", "Unit"]):
                table.cell(0, column).text = name
            for row in range(1, 4):
                cells = [
                    SITES[(section + row) % len(SITES)],
                    BASES[row % len(BASES)],
                    f"{rng.uniform(0, 100):.2f}",
                    UNITS[row % len(UNITS)],
                ]
                for column, value in enumerate(cells):
                    table.cell(row, column).text = value
        document.add_page_break()

    document.save(out)
    _inject_docx_hard_cases(out)


# A footnotes part, an endnotes part, and the rels/content-type entries that make
# Word accept them. Written by hand because python-docx has no footnote API -- and
# a fixture without them cannot test the silent-loss case that matters most, since
# a scanner reading only word/document.xml drops every footnote without a word.
_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

_FOOTNOTES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes {_W_NS}>
<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0"/></w:pPr>
<w:r><w:separator/></w:r></w:p></w:footnote>
<w:footnote w:id="1"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>
<w:r><w:t xml:space="preserve">Footnote one: the licensed ceiling is </w:t></w:r>
<w:r><w:t>eight degrees Celsius at Dunmoor Cold Store.</w:t></w:r></w:p></w:footnote>
<w:footnote w:id="2"><w:p><w:pPr><w:pStyle w:val="FootnoteText"/></w:pPr>
<w:r><w:t>Footnote two: restated after the 2024 meter replacement.</w:t></w:r>
</w:p></w:footnote>
</w:footnotes>"""

_ENDNOTES_XML = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:endnotes {_W_NS}>
<w:endnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0"/></w:pPr>
<w:r><w:separator/></w:r></w:p></w:endnote>
<w:endnote w:id="1"><w:p><w:pPr><w:pStyle w:val="EndnoteText"/></w:pPr>
<w:r><w:t>Endnote one: pallet utilisation excludes third-party haulage.</w:t></w:r>
</w:p></w:endnote>
</w:endnotes>"""

# Injected into the body: a footnote reference, an endnote reference, a hyperlink
# run, a PAGEREF field (instrText plus a cached result) and a bare PAGE field
# whose only "text" is the field instruction -- which a scanner that treats
# instrText as content will emit as the literal string "PAGE \\* MERGEFORMAT".
_HARD_PARAGRAPHS = """<w:p>
<w:r><w:t xml:space="preserve">Excursions are counted per interval.</w:t></w:r>
<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="1"/></w:r>
<w:r><w:t xml:space="preserve"> Restated figures carry a note.</w:t></w:r>
<w:r><w:rPr><w:rStyle w:val="FootnoteReference"/></w:rPr><w:footnoteReference w:id="2"/></w:r>
<w:r><w:rPr><w:rStyle w:val="EndnoteReference"/></w:rPr>
<w:endnoteReference w:id="1"/></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Published at </w:t></w:r>
<w:hyperlink r:id="rIdFixtureLink"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr>
<w:t>the operations portal</w:t></w:r></w:hyperlink>
<w:r><w:t xml:space="preserve"> and reviewed quarterly.</w:t></w:r></w:p>
<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>
<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc1 \\h </w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="separate"/></w:r>
<w:r><w:t>12</w:t></w:r>
<w:r><w:fldChar w:fldCharType="end"/></w:r>
<w:r><w:t xml:space="preserve"> is the cross-reference target.</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Page </w:t></w:r>
<w:r><w:fldChar w:fldCharType="begin"/></w:r>
<w:r><w:instrText xml:space="preserve"> PAGE \\* MERGEFORMAT </w:instrText></w:r>
<w:r><w:fldChar w:fldCharType="end"/></w:r>
<w:r><w:t xml:space="preserve"> of the manual.</w:t></w:r></w:p>"""

_LINK_REL = (
    '<Relationship Id="rIdFixtureLink" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
    'Target="https://example.invalid/operations-portal" TargetMode="External"/>'
)
_NOTE_RELS = (
    '<Relationship Id="rIdFixtureFootnotes" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" '
    'Target="footnotes.xml"/>'
    '<Relationship Id="rIdFixtureEndnotes" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes" '
    'Target="endnotes.xml"/>'
)
_NOTE_TYPES = (
    '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>'
    '<Override PartName="/word/endnotes.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.endnotes+xml"/>'
)


def _inject_docx_hard_cases(path: Path) -> None:
    """Rewrite the saved docx to add footnotes, endnotes, fields and a hyperlink.

    A zip cannot be edited in place, so the archive is copied entry by entry with
    four parts rewritten. Order is preserved because Word is sensitive to it.
    """
    source = path.with_suffix(".docx.tmp")
    shutil.move(path, source)
    with (
        zipfile.ZipFile(source) as original,
        zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as rebuilt,
    ):
        for item in original.infolist():
            data = original.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode("utf-8")
                # Insert the hard paragraphs just before the body's sectPr, which
                # is always the last child of w:body.
                marker = text.rfind("<w:sectPr")
                if marker < 0:
                    marker = text.rfind("</w:body>")
                data = (text[:marker] + _HARD_PARAGRAPHS + text[marker:]).encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                text = data.decode("utf-8")
                data = text.replace(
                    "</Relationships>", _LINK_REL + _NOTE_RELS + "</Relationships>"
                ).encode("utf-8")
            elif item.filename == "[Content_Types].xml":
                text = data.decode("utf-8")
                data = text.replace("</Types>", _NOTE_TYPES + "</Types>").encode("utf-8")
            rebuilt.writestr(item, data)
        rebuilt.writestr("word/footnotes.xml", _FOOTNOTES_XML)
        rebuilt.writestr("word/endnotes.xml", _ENDNOTES_XML)
    source.unlink()


# --------------------------------------------------------------------------- #
# PPTX
# --------------------------------------------------------------------------- #


def write_deck_large(out: Path, slides: int) -> None:
    import pptx
    from pptx.util import Emu, Inches, Pt

    presentation = pptx.Presentation()
    blank = presentation.slide_layouts[6]
    titled = presentation.slide_layouts[5]
    rng = random.Random(23)
    images = [
        _png(24, 18, (200, 40, 40)),
        _png(24, 18, (40, 160, 90)),
        _png(24, 18, (50, 90, 200)),
    ]
    image_paths = []
    for index, data in enumerate(images):
        target = FIXTURES / f".deck-image-{index}.png"
        target.write_bytes(data)
        image_paths.append(target)

    for index in range(slides):
        kind = index % 5
        slide = presentation.slides.add_slide(blank if kind == 4 else titled)
        if kind != 4:
            slide.shapes.title.text = f"{index + 1}. {METRICS[index % len(METRICS)]}"

        if kind == 0:  # bulleted body
            box = slide.shapes.add_textbox(Inches(0.6), Inches(1.8), Inches(8.5), Inches(4))
            frame = box.text_frame
            frame.text = PARAGRAPHS[index % len(PARAGRAPHS)][:90]
            for bullet in range(4):
                paragraph = frame.add_paragraph()
                paragraph.text = f"{SITES[(index + bullet) % len(SITES)]}: "
                paragraph.text += f"{rng.uniform(0, 100):.1f} {UNITS[bullet % len(UNITS)]}"
                paragraph.level = 1
                paragraph.font.size = Pt(16)
        elif kind == 1:  # a real table
            rows, columns = 5, 4
            shape = slide.shapes.add_table(
                rows, columns, Inches(0.6), Inches(1.8), Inches(8.5), Inches(3.5)
            )
            table = shape.table
            for column, name in enumerate(["Site", "Basis", "Value", "Unit"]):
                table.cell(0, column).text = name
            for row in range(1, rows):
                values = [
                    SITES[(index + row) % len(SITES)],
                    BASES[row % len(BASES)],
                    f"{rng.uniform(0, 100):.2f}",
                    UNITS[row % len(UNITS)],
                ]
                for column, value in enumerate(values):
                    table.cell(row, column).text = value
        elif kind == 2:  # picture plus a caption
            slide.shapes.add_picture(
                str(image_paths[index % len(image_paths)]),
                Inches(0.6),
                Inches(1.8),
                height=Inches(2.4),
            )
            box = slide.shapes.add_textbox(Inches(0.6), Inches(4.6), Inches(8.5), Inches(0.8))
            box.text_frame.text = f"Figure {index + 1}: trend at {SITES[index % len(SITES)]}."
        elif kind == 3:  # prose
            box = slide.shapes.add_textbox(Inches(0.6), Inches(1.8), Inches(8.5), Inches(4))
            box.text_frame.text = PARAGRAPHS[index % len(PARAGRAPHS)]
            box.text_frame.word_wrap = True
        else:  # kind == 4: a picture and nothing else. No title, no text at all.
            slide.shapes.add_picture(
                str(image_paths[index % len(image_paths)]),
                Emu(457200),
                Emu(457200),
                height=Inches(5),
            )

        # Notes on every fourth slide, so a notes-blind extractor loses a
        # countable amount rather than an unknown amount.
        if index % 4 == 0:
            slide.notes_slide.notes_text_frame.text = (
                f"Speaker notes for slide {index + 1}: the {METRICS[index % len(METRICS)]} "
                "figure is restated and the audit trail is in the appendix."
            )

    presentation.save(out)
    for target in image_paths:
        target.unlink()


# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--small-rows", type=int, default=10_000)
    parser.add_argument("--big-rows", type=int, default=1_000_000)
    parser.add_argument("--sections", type=int, default=100, help="docx sections (~200 pages)")
    parser.add_argument("--slides", type=int, default=100)
    parser.add_argument(
        "--only",
        nargs="*",
        default=None,
        choices=["small", "big", "messy", "docx", "pptx"],
    )
    arguments = parser.parse_args()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    wanted = set(arguments.only or ["small", "big", "messy", "docx", "pptx"])

    jobs = [
        ("small", "sheet-small.xlsx", lambda p: write_small(p, arguments.small_rows)),
        ("big", "sheet-big.xlsx", lambda p: write_big(p, arguments.big_rows)),
        ("messy", "sheet-messy.xlsx", write_messy),
        ("docx", "doc-long.docx", lambda p: write_doc_long(p, arguments.sections)),
        ("pptx", "deck-large.pptx", lambda p: write_deck_large(p, arguments.slides)),
    ]
    for key, name, build in jobs:
        if key not in wanted:
            continue
        target = FIXTURES / name
        build(target)
        print(f"{name:<20} {target.stat().st_size / 1e6:>9.1f} MB")


if __name__ == "__main__":
    main()
