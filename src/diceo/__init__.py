"""diceo -- one office document into retrieval-ready chunks, fast, in bounded memory.

The whole library::

    from diceo import chunk

    for piece in chunk("quarterly-report.pdf"):
        index(piece.embed_text, meta=piece.meta)

PDF, Word, Excel, PowerPoint, legacy ``.xls``, OpenDocument spreadsheets, CSV, HTML,
email and plain text all go in; chunks sized for a retriever come out.
:data:`FORMATS` is the exact list, spelled the way :func:`sniff` reports it, so
``"pdf" in diceo.FORMATS`` is the whole of "can it read this?" and a crawl can be
filtered before anything is opened.

**The unit of work is one document.** Parallelism, crawling and indexing belong to
the caller, so there is no global state here, nothing is loaded at import
time, and ``Chunk`` is picklable -- fan out across a process pool and this stays
out of your way.

**Nothing is dropped without being counted.** A document that is silently missing
from your index is the worst outcome in this domain, so every loss is data::

    from diceo import chunk, Diagnostics

    report = Diagnostics()
    pieces = list(chunk("scanned.pdf", diagnostics=report))
    if report.needs_ocr:
        route_to_ocr(path)              # every page was a picture, not text
    if report.lost_data:
        log.warning("incomplete: %s", report.as_dict())

**Failures are one exception type.** ``except DiceoError`` is the complete
handler; see :mod:`diceo.errors` for why that matters more than it sounds.

Imports are deliberately shallow: ``pypdfium2`` and the format readers are imported
on first use, not here, so ``import diceo`` stays well inside the 150 ms
cold-import budget (measured 20 ms) because it runs once per worker process,
thousands of times.
"""

from __future__ import annotations

from diceo.api import chunk, extract, sniff
from diceo.chunker import chunk_blocks
from diceo.errors import (
    CorruptDocument,
    DiceoError,
    DocumentNotFound,
    EncryptedDocument,
    SourceNotSeekable,
    UnsupportedFormat,
)
from diceo.source import SUPPORTED as _SUPPORTED
from diceo.types import Block, Chunk, Diagnostics, Limits, Locator

__version__ = "0.1.0"

#: Every format :func:`chunk` accepts, as :func:`sniff` names them. Importable so a
#: caller can filter a crawl before opening anything.
#:
#: Bound to :data:`diceo.source.SUPPORTED` rather than restated here: the reader
#: that refuses a format and the tuple that advertises it have to be the same object,
#: or the public name is free to drift away from what the library can actually read.
#: Imported under a private alias so this stays the *only* public spelling of it, and
#: ``diceo.source`` is already loaded by :mod:`diceo.api`, so it costs no import
#: time.
#:
#: This comment is for the rendered docs alone. ``FORMATS`` is a plain tuple, so
#: ``help(diceo.FORMATS)`` describes *tuples*, not this -- which is why the module
#: docstring above says what it is too, where ``help(diceo)`` will find it. The
#: value is its own best documentation at a prompt: ``print(diceo.FORMATS)`` prints
#: the names, and nothing has to be kept in step with it to stay true.
FORMATS: tuple[str, ...] = _SUPPORTED

__all__ = [
    "FORMATS",
    "Block",
    "Chunk",
    "CorruptDocument",
    "Diagnostics",
    "DiceoError",
    "DocumentNotFound",
    "EncryptedDocument",
    "Limits",
    "Locator",
    "SourceNotSeekable",
    "UnsupportedFormat",
    "__version__",
    "chunk",
    "chunk_blocks",
    "extract",
    "sniff",
]
