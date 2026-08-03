"""Every way diceo is allowed to fail.

The contract, which the fuzz suite in ``tests/test_robustness.py`` enforces on
several hundred deliberately broken files:

**A caller sees a :class:`DiceoError`, or they see chunks. Never a third thing.**

No ``KeyError`` from an XML dict, no ``struct.error`` from a truncated ZIP, no
``UnicodeDecodeError`` from a mislabelled encoding, no ``OSError`` from a ctypes
call into PDFium. Those are all real things the backends raise, and every one of
them reaching a caller is a bug in this package -- because the caller's realistic
handler is::

    try:
        for piece in diceo.chunk(path):
            index(piece)
    except diceo.DiceoError as exc:
        quarantine(path, str(exc))

and that handler has to be sufficient. A pipeline processing ten thousand files
cannot enumerate the exception types of five backends, and if it has to, it will
instead write ``except Exception`` and swallow real bugs along with bad documents.

Two deliberate design choices:

*Dual inheritance.* :class:`DocumentNotFound` is also a ``FileNotFoundError``, so
existing ``except FileNotFoundError`` code keeps working, and
:class:`UnsupportedFormat` is also a ``ValueError``, which is what this package
raised before the hierarchy existed. Adding structure must not break the handler
someone already wrote.

*An exception is for "I cannot give you this document."* Anything partial --
truncation, a page with no text layer, one broken slide out of forty -- is
**not** an exception. It is a count in ``diagnostics`` and the readable remainder
is still returned (rule 3). Failing a 900-page report because page 412 has a
cyclic font reference is the wrong trade for an indexing pipeline, and it is the
behaviour that makes people write their own extractor.
"""

from __future__ import annotations

__all__ = [
    "CorruptDocument",
    "DiceoError",
    "DocumentNotFound",
    "EncryptedDocument",
    "SourceNotSeekable",
    "UnsupportedFormat",
]


class DiceoError(Exception):
    """Base class for everything this package raises on purpose.

    ``except DiceoError`` is the complete handler. Carries the offending source
    so a log line can name the file without the caller threading it through.
    """

    def __init__(self, message: str, *, source: str = "") -> None:
        super().__init__(f"{source}: {message}" if source else message)
        self.source = source
        self.reason = message


class DocumentNotFound(DiceoError, FileNotFoundError):
    """No readable file at that path.

    Also a ``FileNotFoundError``, because that is what a caller already catches
    and being clever about it would only break their code.
    """


class UnsupportedFormat(DiceoError, ValueError):
    """We can identify this file, or fail to, but either way we cannot read it.

    The message must always say what *is* supported. An error that says only
    "unsupported" makes the reader open our source to find out; there are eleven
    formats and listing them costs nothing.
    """

    def __init__(
        self,
        message: str,
        *,
        source: str = "",
        detected: str = "",
        supported: tuple[str, ...] = (),
    ) -> None:
        if supported:
            message = f"{message}. Supported: {', '.join(supported)}"
        super().__init__(message, source=source)
        self.detected = detected
        """Best guess at what the file actually is, for the caller's log. Empty
        when even the guess failed."""


class CorruptDocument(DiceoError):
    """The format is right and the bytes are not: we recovered nothing at all.

    Raised only when *nothing* could be read. A file we can partly read is not
    corrupt as far as a caller is concerned -- it is a document plus diagnostics,
    because half a document in the index beats none of it.
    """


class EncryptedDocument(DiceoError):
    """Password-protected, and we were not given the password.

    Its own class rather than a :class:`CorruptDocument` because the remedy is
    completely different: the caller has to supply a credential or route the file
    to a human. Conflating "broken" with "locked" makes a triage queue useless.
    """


class SourceNotSeekable(DiceoError, ValueError):
    """A stream we cannot seek in, for a format that cannot be read forward.

    Not a limitation we could engineer away: a PDF's cross-reference table and a
    ZIP's central directory both live at the **end** of the file, so the first
    thing any reader must do is seek there. Every format in scope is one or the
    other. A pipe, a socket, or a chunked HTTP body therefore has to be spooled
    to disk or into memory first, and saying so plainly is much kinder than the
    mysterious partial failure the caller would otherwise debug.
    """

    def __init__(self, message: str = "", *, source: str = "") -> None:
        super().__init__(
            message
            or (
                "this source cannot seek, and no format diceo reads can be parsed "
                "forward from byte 0 (a PDF's xref and a ZIP's central directory are "
                "both at the end of the file). Spool it to a file or pass bytes"
            ),
            source=source,
        )
