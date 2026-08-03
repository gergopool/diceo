"""Facts about ZIP-packaged office documents that more than one reader needs.

Small on purpose. ``ooxml`` (docx, pptx), ``sheets`` (xlsx), ``legacy_sheets``
(ods) and ``source`` (detection) all open the same kind of container and all had
their own copy of the two or three things below. The copies had already started to
drift -- the media-directory pattern was spelled ``(word|xl|ppt)`` in one module
and ``(xl|word|ppt)`` in another -- which is the whole argument for a single
definition.

**Why a module of its own rather than importing from** ``ooxml``. Nothing in this
package imports across reader modules today: ``ooxml`` and ``sheets`` are both
leaves, and ``legacy_sheets`` and ``delimited`` depend on ``sheets``. Having
``sheets`` reach into ``ooxml`` would make reading a spreadsheet -- or, through
``legacy_sheets``, a 1998 ``.xls`` -- import the 63 KB Word/PowerPoint reader for
one regex. A leaf module both can point at costs nothing and keeps the direction
of every existing import unchanged.

Nothing here is public API; the leading underscore says so, which is why adding it
is not a decisions.md entry.

It imports ``zipfile``, and ``diceo.source`` -- which is on the ``import diceo``
path -- imports both it and ``zipfile`` already, so this costs no cold-import time.
Measured at 20 ms before and after, against the 150 ms budget. Anything added here
that imports something heavier is a different question and has to be measured again.
"""

from __future__ import annotations

import re
import zipfile
from typing import IO

from .errors import CorruptDocument

#: Where every OOXML flavour keeps its raster parts. This was ``ppt/media/`` only, so
#: a Word report with seven charts reported nothing at all.
MEDIA_DIR = re.compile(r"(word|xl|ppt)/media/", re.IGNORECASE)

#: The relationship namespace, the one ``r:id`` and ``r:embed`` are qualified by.
OFF_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: Ways reading one member of the archive cannot get an answer: the member is not
#: there, the archive will not open, the member is ZIP-encrypted (``RuntimeError``,
#: and the reason it is listed -- refusing there would turn a password-protected
#: workbook's `EncryptedDocument` into a `CorruptDocument`, which is the one
#: distinction a triage queue is built on), or its compression method is one CPython
#: cannot decode (``NotImplementedError`` -- deflate64, or LZMA on a build without the
#: module).
#:
#: **None of them is evidence about the document.** They say "this member is
#: unreadable", never "this file is broken", so every site that catches this tuple
#: carries on with whatever it can still do -- a fallback tier, a skipped diagnostic,
#: another reader -- rather than turning a supporting part into a verdict on the
#: whole document. A site that *does* mean to reach a verdict wants its own tuple,
#: chosen for what that verdict must not swallow; see ``source._zip_format``.
NOT_OUR_VERDICT = (
    KeyError,
    zipfile.BadZipFile,
    OSError,
    EOFError,
    ValueError,
    RuntimeError,
    NotImplementedError,
)

#: How much of a part's head is buffered to answer "is there a doctype here".
#: The prolog of an OOXML part is one ``<?xml ... ?>`` and nothing else -- 56 bytes
#: as Word writes it -- so this is three orders of magnitude of slack, and it is what
#: bounds the buffer a hostile file can make us hold before the scan has an answer.
DTD_SCAN_BYTES = 8192

_UTF8_BOM = b"\xef\xbb\xbf"


def declares_dtd(head: bytes) -> bool:
    """Does this part's **prolog** carry a ``<!DOCTYPE``?

    The one thing standing between a 20 KB file and 2 GB of resident memory.

    ``xml.etree`` is expat, and expat resolves no external entities (verified: a
    ``SYSTEM "file:///etc/passwd"`` entity raises *undefined entity*, and an
    external subset is ignored without a fetch). What it *does* do is expand
    **internal** ones, and its billion-laughs limiter is a **ratio** -- roughly
    100x, and only armed once 8 MiB of output exists. Padding the entity with
    literal filler that deflates to nothing therefore buys expansion in proportion
    to the padding while keeping the ratio legal: measured, a 19.8 KB ``.docx``
    declaring a 20 MB part reached **2.0 GB** of RSS and returned a chunk with no
    error at all. ``Limits`` cannot see it, because the whole cost is paid inside
    one ``iterparse`` call before the first block exists -- the same argument
    ``legacy_sheets`` already makes for the ODS hang guard, in a place the guard
    was never added. The byte-oriented ZIP guards in ``source`` cannot see it
    either: 20 MB is under every ceiling they enforce.

    Refusing the declaration outright is the cheap fix because **no OOXML producer
    emits one**. Word, LibreOffice, Google Docs, python-docx, xlsxwriter: none of
    them writes a DTD into any part, and the OPC spec gives it nothing to do. So
    this costs a real document nothing, which is what makes it affordable at every
    parse site rather than only at the ones that stream.

    Scans the prolog *only* -- comments and processing instructions are stepped
    over, and the first element start tag ends the search -- so the literal text
    ``<!DOCTYPE`` inside a document's own content can never be mistaken for a
    declaration. Cost on a healthy part is one pass over ~56 bytes.

    A part in a UTF-16 encoding reads as neither, and returns False here: the
    scan is bytewise and OOXML parts are UTF-8 in practice. That leaves expat's
    own ratio limiter as the only guard for a shape no real producer emits, which
    is the same position the package was in everywhere before this existed.
    """
    at = 3 if head.startswith(_UTF8_BOM) else 0
    size = len(head)
    while at < size:
        if head[at] in b" \t\r\n":
            at += 1
            continue
        if head[at] != 0x3C:  # "<" -- character data before the root element
            return False
        if head.startswith(b"<!--", at):
            end = head.find(b"-->", at + 4)
            if end < 0:
                return False
            at = end + 3
            continue
        if head.startswith(b"<?", at):
            end = head.find(b"?>", at + 2)
            if end < 0:
                return False
            at = end + 2
            continue
        # Whatever is here is either the declaration or the root element, and
        # either way the prolog is over.
        return head.startswith(b"<!DOCTYPE", at)
    return False


def refuse_dtd(head: bytes, *, name: str, part: str) -> None:
    """Raise if ``head`` opens with a document type declaration. See `declares_dtd`.

    Refuses the **document**, not the part, and does so from every part rather than
    only the body. A supporting part normally falls through to a fallback tier when
    it cannot be read, and that tiering is right for damage; it is wrong for a
    declaration no producer emits, where the fallback would simply parse the same
    hostile bytes a second way. One verdict is also one thing to test.
    """
    if declares_dtd(head):
        raise CorruptDocument(
            f"declares a document type in {part!r} -- no office format writes one, and "
            f"its entity expansion is unbounded by anything diceo can measure",
            source=name,
        )


class _HeadFirst:
    """``head`` replayed, then the rest of ``stream``. Cheaper than seeking back.

    ``ZipExtFile.peek`` returns at most 512 bytes however much is asked for, which
    a padded prolog walks straight past, and seeking a member back to zero restarts
    its decompressor. Replaying a buffer we had to read anyway costs one object per
    streamed part.

    ``read`` may return the head as a **short read** rather than topping it up to
    the requested size. That is legal for a stream and is what ``iterparse`` -- the
    only consumer -- expects: it loops until it gets ``b""``, and feeds whatever
    arrives. Nothing else should be handed one of these.
    """

    __slots__ = ("_head", "_stream")

    def __init__(self, head: bytes, stream: IO[bytes]) -> None:
        self._head = head
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        head = self._head
        if not head:
            return self._stream.read(size)
        if size < 0:
            self._head = b""
            return head + self._stream.read()
        if size >= len(head):
            self._head = b""
            return head
        self._head = head[size:]
        return head[:size]


def dtd_free_stream(stream: IO[bytes], *, name: str, part: str) -> IO[bytes]:
    """``stream``, after proving its prolog declares no doctype. See `refuse_dtd`."""
    head = stream.read(DTD_SCAN_BYTES)
    refuse_dtd(head, name=name, part=part)
    return _HeadFirst(head, stream)  # type: ignore[return-value]


#: Upper bound on element nesting in a streamed part, and the second half of the
#: memory story `declares_dtd` tells.
#:
#: ``iterparse`` builds a tree as it goes, and a reader empties it only at the
#: boundaries it knows -- the end of a paragraph, a row, a table, a shared string.
#: Nothing else is ever released, so a part that simply never *closes* anything
#: accumulates one live ``Element`` per start tag whatever its tags are: a 100 KB
#: ``.docx`` of a million nested ``w:tbl`` reached 1.2 GB of RSS, and the tags do not
#: have to be ones the reader knows -- an unknown element is skipped by the dispatch
#: and retained by the tree all the same. The ZIP guards in ``source`` bound *bytes*,
#: which is a different axis and cannot see this; ``Limits`` is checked between
#: blocks, and this shape produces none.
#:
#: 256 is far past anything a producer emits. Word nests for layout constantly and
#: the deepest part of the corpus measures 34; a document needing more than 256 would
#: already be one no other reader in the field could open.
MAX_DEPTH = 256

#: Upper bound on elements started since the tree was last emptied -- the *breadth*
#: half of what `MAX_DEPTH` bounds by nesting, and it needs its own counter because
#: the two shapes evade each other. A single ``w:p`` holding ten million ``w:r``
#: never reaches a clear, so depth stays at 3 while the tree grows without limit; a
#: ~2 MB part buys several GB. One ``<row>`` of 1.3 million ``<c>`` is the same shape
#: in a worksheet, and ``<mergeCells>`` with two million children is a third.
#:
#: Conversely a million *nested* tables, each holding one closed paragraph, resets
#: this counter at every level and is caught by `MAX_DEPTH` instead. Neither guard
#: subsumes the other, which is why both exist and why the reset is deliberately
#: optimistic: it is allowed to be, because depth is exact.
#:
#: 262,144 is four times the widest row the format admits: 16,384 columns -- the
#: ceiling ``sheets._MAX_COLUMNS`` already enforces -- at about four elements each.
#: The densest document fixture, ``wordy.docx``, peaks at 41 elements between clears,
#: so the slack against real input is four orders of magnitude. Measured on the
#: hostile shapes: 1 << 20 let 120 MB accumulate before tripping, 1 << 18 bounds it
#: to ~30 MB, and neither is reachable by a document any other reader would open.
MAX_OPEN_ELEMENTS = 1 << 18


def refuse_shape(depth: int, open_elements: int, *, name: str, part: str) -> None:
    """Raise for whichever of the two ceilings was passed. See `MAX_DEPTH`.

    Out of line on purpose: the comparison belongs in the parse loop, where it is two
    integer tests per element, and the message formatting belongs anywhere else.
    """
    if depth > MAX_DEPTH:
        raise CorruptDocument(
            f"nests elements more than {MAX_DEPTH:,} deep in {part!r} -- no office "
            f"format writes that, and the parser holds every open element until it "
            f"closes",
            source=name,
        )
    raise CorruptDocument(
        f"holds over {MAX_OPEN_ELEMENTS:,} unclosed elements in {part!r} -- no office "
        f"format writes that, and every one of them is held until its parent closes",
        source=name,
    )
