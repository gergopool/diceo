"""Build the PDF fixtures every probe needs, from one public seed document.

Seed: arXiv 1706.03762v7 (born-digital, real tables, 15 pages), downloaded on first
run and verified against a pinned sha256. Derived fixtures:

  paper-large.pdf
      ~300 pages -- speed and memory at a scale where per-document overhead stops
      mattering and per-page cost dominates
  paper-huge.pdf
      ~1500 pages -- the O(1)-memory acceptance test
  paper-scanned.pdf
      15 pages rendered to images and re-wrapped, i.e. no text layer at all -- the
      silent-failure fixture

Build them all::

    uv run --group dev python scripts/make_pdf_fixtures.py
"""

from __future__ import annotations

import argparse
import hashlib
import urllib.request
from pathlib import Path

import pypdfium2 as pdfium

FIXTURES = Path("data/fixtures")
SEED = FIXTURES / "paper-tables.pdf"
SEED_URL = "https://arxiv.org/pdf/1706.03762v7"
SEED_SHA256 = "bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697"


def repeat(seed: Path, out: Path, copies: int) -> None:
    if out.exists():
        print(f"  {out.name} exists, skipping")
        return
    dest = pdfium.PdfDocument.new()
    src = pdfium.PdfDocument(seed)
    for _ in range(copies):
        dest.import_pages(src)
    dest.save(str(out))
    print(f"  {out.name}: {len(dest)} pages, {out.stat().st_size / 1e6:.1f} MB")


def rasterize(seed: Path, out: Path, scale: float = 1.4) -> None:
    """Render every page to a JPEG and rebuild a PDF from the images, so the
    result carries pixels and no text layer -- a scanned document in every way
    that matters to an extractor."""
    if out.exists():
        print(f"  {out.name} exists, skipping")
        return
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    src = pdfium.PdfDocument(seed)
    first = src[0]
    width, height = first.get_width(), first.get_height()
    pdf = canvas.Canvas(str(out), pagesize=(width, height))
    for page in src:
        image = page.render(scale=scale).to_pil().convert("RGB")
        pdf.drawImage(ImageReader(image), 0, 0, width=width, height=height)
        pdf.showPage()
        page.close()
    pdf.save()
    print(f"  {out.name}: {len(src)} pages, {out.stat().st_size / 1e6:.1f} MB (no text layer)")


def fetch_seed() -> None:
    """Download the seed paper, once, and refuse to build on anything else.

    It used to be assumed present, so `just all-fixtures` -- the command
    CONTRIBUTING gives a new contributor as their first -- exited on a fresh clone
    with `seed missing`, and every fixture-backed test skipped for a reason that
    read like the corpus was optional. Fetched rather than committed because a
    2.2 MB PDF in a package repository is 2.2 MB in everyone's clone forever.

    The digest is checked because a fixture is a control: a seed that quietly
    became a different paper would move every measurement built on it, and the
    measurements are what this project publishes.
    """
    if SEED.exists():
        return
    print(f"fetching seed: {SEED.name} <- {SEED_URL}")
    SEED.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(SEED_URL, timeout=60) as response:  # noqa: S310 -- https, pinned
        raw = response.read()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != SEED_SHA256:
        raise SystemExit(
            f"seed digest mismatch\n  expected {SEED_SHA256}\n  got      {digest}\n"
            f"arXiv re-generates PDFs occasionally. Check the document is still "
            f"1706.03762v7, then update SEED_SHA256 in this script."
        )
    SEED.write_bytes(raw)
    print(f"  {SEED.name}: {len(raw) / 1e6:.1f} MB, sha256 verified")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--large", type=int, default=20, help="copies for paper-large")
    parser.add_argument("--huge", type=int, default=100, help="copies for paper-huge")
    args = parser.parse_args()

    fetch_seed()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    print("building PDF fixtures:")
    repeat(SEED, FIXTURES / "paper-large.pdf", args.large)
    repeat(SEED, FIXTURES / "paper-huge.pdf", args.huge)
    rasterize(SEED, FIXTURES / "paper-scanned.pdf")


if __name__ == "__main__":
    main()
