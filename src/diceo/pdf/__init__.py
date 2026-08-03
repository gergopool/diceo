"""PDF cracking: PDFium text plus synthesized structure.

The surface anyone outside this package should use is one generator::

    from diceo.pdf import blocks

    for block in blocks("paper.pdf"):
        print(block.kind, block.level, block.text[:60])

``PDFIUM_LOCK`` comes with it because it is a contract, not an internal: PDFium
forbids concurrent calls even across different documents, so a caller who makes
their own pypdfium2 calls in the same process has to take the same lock, and a
contract you cannot import is not a contract.

The other seven names re-exported below -- ``Line``, ``Census``, ``StyleModel``,
``font_census``, ``lines``, ``page_lines``, and this module's own ``Block``, which
is the PDF-internal one and *not* :class:`diceo.types.Block` -- are the layout
stage's own vocabulary, kept importable for the tests and the research harness that
measure it directly. They are not covered by the package's stability promise and
they are not in :data:`diceo.__all__`; a change to them is not a breaking change.

Import is deliberately shallow -- ``pypdfium2`` is the only dependency and it is
imported by :mod:`diceo.pdf.extract`, not here, so ``import diceo`` stays
cheap (D12's 150 ms budget).
"""

from diceo.pdf.extract import (
    PDFIUM_LOCK,
    Block,
    Census,
    Line,
    StyleModel,
    blocks,
    font_census,
    lines,
    page_lines,
)

__all__ = [
    "PDFIUM_LOCK",
    "Block",
    "Census",
    "Line",
    "StyleModel",
    "blocks",
    "font_census",
    "lines",
    "page_lines",
]
