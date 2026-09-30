"""What is this file, and how do we get bytes out of it?

Two jobs that belong together, because both of them are about refusing to guess.

**Identification is by content, not by name.** A `.xlsx` that is really a `.docx`
is common in the wild -- mail systems rename attachments, users "Save As" without
looking, export scripts hardcode a suffix. Trusting the name is how a whole
document goes missing from an index while every log line says success. So the
magic bytes decide, the ZIP part names disambiguate the four formats that all
start ``PK``, and the extension is consulted only for formats that genuinely have
no magic (there is no byte sequence that proves a file is a CSV).

**We say what we cannot do, by name.** Every unsupported format gets its own
message saying what it is and why. "Unsupported format" alone sends the reader
into our source; naming it (``.doc`` is the pre-2007 binary format, and here is
what to do about it) costs one line and answers the support question before it is
asked.
"""

from __future__ import annotations

import io
import re
import stat
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from diceo._zip_parts import NOT_OUR_VERDICT
from diceo.errors import (
    CorruptDocument,
    DocumentNotFound,
    EncryptedDocument,
    SourceNotSeekable,
    UnsupportedFormat,
)
from diceo.types import Limits

#: Everything :func:`diceo.chunk` accepts. Ordered as a human would list them.
#:
#: The one definition: :data:`diceo.FORMATS` is this tuple re-exported, not a copy.
#: It was a copy, and a second format list is a list that will be wrong -- a format
#: added here and missed there is a public API that lies about what it reads.
SUPPORTED: tuple[str, ...] = (
    "pdf",
    "docx",
    "xlsx",
    "pptx",
    "xls",
    "xlsb",
    "ods",
    "csv",
    "html",
    "eml",
    "text",
)

#: ZIP-based containers, keyed by a pattern matching the part that proves what they
#: are. Checked in this order: a ``.docm`` with a stray ``xl/`` folder is still a Word
#: document.
#:
#: Patterns rather than exact names, for two reasons that are both real files:
#:
#: * **Word Online and SharePoint emit ``word/document2.xml``.** An exact-name check
#:   rejected those as "a plain ZIP archive, not a document" -- a valid document
#:   refused outright. This is the failure that makes python-docx immune and its
#:   imitators broken: python-docx resolves the part through ``[Content_Types].xml``
#:   and the package relationships, which is where the part name actually lives.
#: * **OPC part names are case-insensitive by spec**, so ``Word/Document.xml`` is the
#:   same part and some writers produce it.
#:
#: Detection only needs the *type*, so a tolerant name match is enough here; the
#: reader resolves the true part name via the relationships -- see
#: `diceo.ooxml.main_part`.
_ZIP_PARTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"word/document\d*\.xml$", re.IGNORECASE), "docx"),
    (re.compile(r"xl/workbook\d*\.xml$", re.IGNORECASE), "xlsx"),
    (re.compile(r"xl/workbook\d*\.bin$", re.IGNORECASE), "xlsb"),
    (re.compile(r"ppt/presentation\d*\.xml$", re.IGNORECASE), "pptx"),
)

#: OpenDocument declares itself in an uncompressed ``mimetype`` member that the
#: spec requires to be the archive's *first* entry -- so this is real magic, not a
#: guess, and it distinguishes a spreadsheet from a text document from a deck.
_ODF_MIMETYPES: dict[bytes, str] = {
    b"application/vnd.oasis.opendocument.spreadsheet": "ods",
    b"application/vnd.oasis.opendocument.text": "odt",
    b"application/vnd.oasis.opendocument.presentation": "odp",
}

_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".rst", ".log", ".text"}
_DELIMITED_SUFFIXES = {".csv", ".tsv", ".psv", ".tab"}
_HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
_EMAIL_SUFFIXES = {".eml", ".mbox"}
#: Extensions that promise a binary container -- a ZIP or an OLE2 compound file.
#: A file with one of these names whose bytes are *text* was renamed rather than
#: converted, and saying so is the point: see `_text_under_a_container_name`.
_CONTAINER_SUFFIXES = {".xlsx", ".xls", ".xlsb", ".ods", ".pptx", ".docx"}

#: Formats we identify confidently and decline, each with the reason a maintainer
#: would give in an issue reply. Being specific here is the difference between a
#: user filing "diceo can't read my file" and a user knowing what to do next.
_DECLINED: dict[str, str] = {
    "doc": (
        "this is the pre-2007 Word binary format (OLE2 compound file). diceo "
        "reads .docx. Convert with LibreOffice: soffice --convert-to docx"
    ),
    "ppt": (
        "this is the pre-2007 PowerPoint binary format (OLE2 compound file). "
        "diceo reads .pptx. Convert with LibreOffice: soffice --convert-to pptx"
    ),
    "msg": (
        "this is an Outlook .msg (OLE2 compound file). The mature Python reader "
        "for it is GPL-licensed, which diceo cannot depend on without making "
        "itself GPL. Export the mail as .eml, which diceo reads"
    ),
    "rtf": (
        "RTF is not supported yet. Its text is recoverable but the control-word "
        "grammar is a real parser, not a heuristic, and doing it badly loses "
        "content silently. Convert with LibreOffice: soffice --convert-to docx"
    ),
    "odt": (
        "OpenDocument Text is not supported yet -- only .ods (spreadsheets) of "
        "the OpenDocument family. Convert with LibreOffice: soffice "
        "--convert-to docx"
    ),
    "odp": (
        "OpenDocument Presentation is not supported yet -- only .ods "
        "(spreadsheets) of the OpenDocument family. Convert with LibreOffice: "
        "soffice --convert-to pptx"
    ),
    "epub": (
        "EPUB is a ZIP of XHTML documents; diceo does not read it yet. Unzip it "
        "and pass the XHTML files, which diceo does read"
    ),
    "zip": (
        "this is a plain ZIP archive, not a document. diceo's unit of work is "
        "one document -- unzip it and pass the members individually"
    ),
}

#: An absolute ceiling per ZIP member. Refused whatever its ratio, because the
#: harm a bomb does is measured in bytes we would have to read, not in cleverness.
MAX_MEMBER_BYTES = 2 * 1024**3
#: A suspicious expansion factor -- but only applied above `BOMB_MIN_BYTES`.
#: Ratio alone is *not* evidence of an attack: XML compresses 10-20x normally and a
#: generated report with a million similar rows compresses far more, so refusing on
#: ratio alone would reject legitimate documents that every competitor indexes
#: fine. A false positive here is worse than a false negative.
MAX_COMPRESSION_RATIO = 200
#: ...below which a high ratio is harmless. 64 MB of anything is not an attack; it
#: is a big document, and we stream it.
BOMB_MIN_BYTES = 64 * 1024**2

#: The parts a reader loads **whole** instead of streaming, because resolving a style
#: id or a relationship target needs the finished tree rather than a run of events
#: (`ooxml._style_levels`, `ooxml.main_part`, `sheets._date_styles`). Matched on the
#: last path segment and case-insensitively, since OPC part names are.
_EAGER_PARTS = re.compile(
    r"(?:^|/)(?:styles|numbering|comments|footnotes|endnotes|workbook\d*|presentation\d*)"
    r"\.xml$|\.rels$",
    re.IGNORECASE,
)
#: ...and their ceiling, which is far below `MAX_MEMBER_BYTES` because the cost is
#: different in kind. A streamed part costs a buffer; a part read whole costs its own
#: size *and* the element tree built from it, which measured 12x. A `word/styles.xml`
#: declaring 63 MB inside a 225 KB file took peak RSS to 823 MB here and passed both
#: tests above -- under 2 GB, and one byte under the 64 MB where the ratio test starts.
#: 16 MB is 21x the largest such part in the 137-package fixture corpus (`styles.xml`,
#: 750 KB, a 300-page IPBES report), so it refuses bombs and no document we have seen.
MAX_EAGER_MEMBER_BYTES = 16 * 1024**2
#: What the whole archive may declare, summed. Every ceiling above is *per member*,
#: and per-member ceilings compose badly: 300 parts of 63 MB each clear all three
#: (under 2 GB, under 16 MB where eager parts are checked, one byte under the 64 MB
#: where the ratio test starts) and still declare **17.6 GB**. Measured here, that
#: 18 MB .pptx streamed 2.8M chunks in 33 s and held peak RSS at 311 MB -- so
#: bounded memory survived it, and the caller's index would not have.
#:
#: 4 GB is twice the single-member ceiling and 30x the largest package in the
#: 137-package fixture corpus, so it refuses the shape above and no real document.
#: `Limits(max_chars=...)` already stopped this; a caller should not have to set a
#: limit to be told the file is a bomb.
MAX_TOTAL_BYTES = 4 * 1024**3
#: Members in one archive. A 300-slide deck is real and carries thousands of parts
#: once rels and media are counted, so this is deliberately far above any document
#: and only refuses an archive built to make the per-member walk itself the attack.
MAX_MEMBERS = 65_536

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: OLE2 directory entries store stream names as UTF-16LE, so the marker is searched
#: for in that encoding rather than as ASCII. ``EncryptedPackage`` is the stream Office
#: puts the ciphertext in; ``EncryptionInfo`` carries the key derivation parameters.
#: Either one present means the file has a password.
_ENCRYPTED_STREAMS = (
    "EncryptedPackage".encode("utf-16-le"),
    "EncryptionInfo".encode("utf-16-le"),
)


def _ole2_holds_encrypted_package(src: Source) -> bool:
    """Is this OLE2 container an encrypted OOXML package rather than a 1997 document?

    Reads the header's sector size and directory start (MS-CFB §2.2), then scans the
    directory chain for the stream names Office uses for encryption. Bounded to 64
    sectors so a hostile file cannot turn a format sniff into a scan of the whole
    disk, and every failure answers "no" -- this runs during detection, where being
    wrong must not be worse than the message it replaces.
    """
    handle = src.handle
    try:
        handle.seek(0)
        header = handle.read(512)
        if len(header) < 512:
            return False
        sector_shift = int.from_bytes(header[30:32], "little")
        if not 7 <= sector_shift <= 20:  # 128 B .. 1 MB, well past the legal 512/4096
            return False
        sector_size = 1 << sector_shift
        sector = int.from_bytes(header[48:52], "little")
        for _ in range(64):
            if sector >= 0xFFFFFFFA:  # end of chain / free / special
                return False
            handle.seek(512 + sector * sector_size)
            block = handle.read(sector_size)
            if not block:
                return False
            if any(marker in block for marker in _ENCRYPTED_STREAMS):
                return True
            # Walking the FAT to find the next directory sector needs the FAT itself;
            # the first sector holds the root and its first children, which is where
            # these two streams sit in every file Office writes. Trying the next
            # physical sector is a cheap second look, not a chain walk.
            sector += 1
    except (OSError, ValueError):
        return False
    finally:
        with suppress(OSError, ValueError):
            handle.seek(0)
    return False


@dataclass(slots=True)
class Source:
    """A named, seekable byte source, plus whatever the backends need.

    ``path`` is set only when the caller gave us a real file, because two of the
    three backends open a path far more cheaply than a Python file object, and a
    1,000-page PDF reopens itself every 100 pages (D13b).
    When it is ``None`` the backends get ``handle`` instead.
    """

    name: str
    handle: IO[bytes]
    path: Path | None = None
    size: int = -1

    notes: list[str] = field(default_factory=list)
    """What :func:`detect` had to decide against the file's own name, drained into
    ``diagnostics.notes`` by the caller. A crawl that is quietly renaming CSV exports
    to ``.xlsx`` is a fact about the corpus, and the only place it is visible is here
    -- the chunks themselves look perfectly healthy (rule 3)."""

    url: str = ""
    encoding: str | None = None

    @property
    def backend_arg(self) -> Path | IO[bytes]:
        """Whatever this source should be handed to a reader as."""
        if self.path is not None:
            return self.path
        self.handle.seek(0)
        return self.handle

    @property
    def doc_id(self) -> str:
        """Default document identity: the file stem, or the stream's name."""
        return Path(self.name).stem or self.name


def _describe_file_type(mode: int) -> str:
    """Name what a non-regular file actually is, so the error is actionable.

    "not a regular file" alone sends the reader looking at their code; "named pipe
    (FIFO)" sends them looking at the path they passed, which is where the problem
    is.
    """
    for predicate, label in (
        (stat.S_ISFIFO, "named pipe (FIFO)"),
        (stat.S_ISSOCK, "socket"),
        (stat.S_ISBLK, "block device"),
        (stat.S_ISCHR, "character device"),
        (stat.S_ISLNK, "dangling symlink"),
    ):
        if predicate(mode):
            return label
    return "special file"


@contextmanager
def open_source(
    source: str | Path | bytes | bytearray | memoryview | IO[bytes],
    *,
    download_timeout: float = 30.0,
    max_download_bytes: int = 128 * 1024**2,
) -> Iterator[Source]:
    """Normalise anything a caller might pass into a :class:`Source`.

    Accepts an HTTP(S) URL, path, raw ``bytes``, or a binary file object. A file object must be
    seekable, and the error explains why in terms of the file formats rather than
    our implementation -- see :class:`~diceo.errors.SourceNotSeekable`.
    """
    if isinstance(source, str) and source[:8].lower().startswith(("https://", "http://")):
        with _url_source(source, download_timeout, max_download_bytes) as remote:
            yield remote
        return
    if isinstance(source, str | Path):
        path = Path(source)
        # Stat *before* opening. `open("rb")` on a FIFO blocks until a writer
        # appears, so a named pipe passed as a path used to hang this library
        # forever -- no exception, no timeout, no diagnostic. A hang is the worst
        # thing a library can do to a caller, and it is worse than a crash in an
        # indexing pipeline because one bad path stalls the whole worker. The same
        # guard covers device nodes, sockets, and the blocking files under /proc.
        try:
            metadata = path.stat()
            mode = metadata.st_mode
        except ValueError as exc:
            # `stat()` on a path holding a NUL byte raises ValueError, not OSError, so
            # the careful OSError ladder below misses it entirely. A crawler that builds
            # paths from URL-decoded strings or a database column produces these
            # routinely, and "embedded null character" reaching the caller as a bare
            # ValueError is indistinguishable from a bug in diceo.
            raise DocumentNotFound(
                f"not a usable path ({exc})", source=repr(str(path))
            ) from exc
        except FileNotFoundError as exc:
            raise DocumentNotFound("no such file", source=str(path)) from exc
        except PermissionError as exc:
            raise DocumentNotFound(
                "not readable (permission denied)", source=str(path)
            ) from exc
        except OSError as exc:
            raise DocumentNotFound(
                f"cannot be examined ({exc.strerror})", source=str(path)
            ) from exc
        if stat.S_ISDIR(mode):
            raise DocumentNotFound(
                "this is a directory; diceo's unit of work is one document",
                source=str(path),
            )
        if not stat.S_ISREG(mode):
            raise DocumentNotFound(
                f"not a regular file ({_describe_file_type(mode)}); diceo needs a "
                f"seekable document on disk",
                source=str(path),
            )
        try:
            handle = path.open("rb")
        except FileNotFoundError as exc:
            raise DocumentNotFound("no such file", source=str(path)) from exc
        except IsADirectoryError as exc:
            raise DocumentNotFound(
                "this is a directory; diceo's unit of work is one document",
                source=str(path),
            ) from exc
        except PermissionError as exc:
            raise DocumentNotFound(
                "not readable (permission denied)", source=str(path)
            ) from exc
        except OSError as exc:
            raise DocumentNotFound(
                f"cannot be opened ({exc.strerror})", source=str(path)
            ) from exc
        try:
            # Reuse the stat result that established this is a regular file. `size`
            # is descriptive metadata, not a second freshness check, and asking the
            # filesystem for the same inode twice adds a syscall to every document.
            yield Source(name=path.name, handle=handle, path=path, size=metadata.st_size)
        finally:
            handle.close()
        return

    if isinstance(source, bytes | bytearray | memoryview):
        buffer = io.BytesIO(bytes(source))
        yield Source(name="<bytes>", handle=buffer, size=buffer.getbuffer().nbytes)
        return

    read = getattr(source, "read", None)
    if read is None:
        raise UnsupportedFormat(
            f"cannot read from {type(source).__name__}; pass a path, bytes, or a "
            f"binary file object"
        )
    name = str(getattr(source, "name", "<stream>"))
    # Four calls into an object the caller owns, and every one of them can fail for a
    # reason that has nothing to do with diceo: a closed file raises ValueError from
    # `seekable()`, a pipe raises OSError(ESPIPE) from `seek()` *after* claiming to be
    # seekable, a network-backed handle raises OSError(EIO) mid-read. All three used to
    # travel straight out of `chunk()` past `except DiceoError`. A caller handing us a
    # stream is the case where the object is least under our control, so it is the case
    # that most needs converting rather than trusting.
    try:
        seekable = getattr(source, "seekable", None)
        usable = seekable is not None and seekable()
    except (OSError, ValueError) as exc:
        raise SourceNotSeekable(f"{name}: the stream cannot be queried ({exc})") from exc
    if not usable:
        raise SourceNotSeekable(source=name)
    try:
        probe = read(0)
    except (OSError, ValueError) as exc:
        raise CorruptDocument(f"the stream cannot be read ({exc})", source=name) from exc
    if isinstance(probe, str):
        raise UnsupportedFormat(
            'this file was opened in text mode; diceo needs bytes. Open it with mode="rb"',
            source=name,
        )
    try:
        source.seek(0, io.SEEK_END)
        size = source.tell()
        source.seek(0)
    except (OSError, ValueError) as exc:
        # `seekable()` said yes and `seek()` disagreed. A pipe wrapped in a BufferedReader
        # does exactly this, and the message has to name the real problem rather than the
        # method that happened to raise.
        raise SourceNotSeekable(
            f"{name}: the stream reports itself seekable but seeking failed ({exc})"
        ) from exc
    yield Source(name=name, handle=source, size=size)


@contextmanager
def _url_source(url: str, timeout: float, max_bytes: int) -> Iterator[Source]:
    """Fetch one document with stdlib only; local inputs import no HTTP machinery."""
    import gzip
    import http.client
    import tempfile
    import urllib.error
    import urllib.parse
    import urllib.request
    import zlib

    try:
        parsed = urllib.parse.urlsplit(url)
        _ = parsed.port  # validate an explicitly supplied port before making a request
    except ValueError as exc:
        raise UnsupportedFormat(f"invalid HTTP(S) URL ({exc})", source=url) from exc
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsupportedFormat(
            "pass an HTTP(S) URL with a host and no embedded credentials", source=url
        )

    class Redirects(urllib.request.HTTPRedirectHandler):
        def http_error_302(self, req, fp, code, msg, headers):
            # urllib otherwise drains each redirect body with an unbounded read.
            # Keep its redirect/loop handling, but discard that body without reading.
            fp.close()
            return super().http_error_302(req, io.BytesIO(), code, msg, headers)

        http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            target = urllib.parse.urlsplit(newurl)
            if (
                target.scheme not in ("http", "https")
                or not target.hostname
                or target.username is not None
                or target.password is not None
            ):
                raise UnsupportedFormat("redirect is not an HTTP(S) document URL", source=url)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "diceo/0.1 (+https://github.com/gergopool/diceo)",
            "Accept-Encoding": "identity",
        },
    )
    # Seekability is required by PDF/ZIP readers. Spill large downloads to disk rather
    # than retaining a second whole document in Python memory; close on every exit.
    with tempfile.SpooledTemporaryFile(max_size=1024**2, mode="w+b") as handle:
        try:
            with urllib.request.build_opener(Redirects()).open(
                request, timeout=timeout
            ) as response:
                final_url = response.geturl()
                media = response.headers.get_content_type()
                charset = response.headers.get_content_charset()
                filename = response.headers.get_filename()
                declared = response.headers.get("Content-Length")
                if declared and int(declared) > max_bytes:
                    raise CorruptDocument(
                        f"download exceeds max_download_bytes={max_bytes}", source=url
                    )
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding not in ("identity", "gzip"):
                    raise UnsupportedFormat(
                        f"unsupported HTTP content encoding {encoding!r}", source=url
                    )
                wire_size = 0

                class WireLimit:
                    def read(self, count=-1):
                        nonlocal wire_size
                        count = min(
                            count if count >= 0 else 64 * 1024, max_bytes - wire_size + 1
                        )
                        piece = response.read(count)
                        wire_size += len(piece)
                        if wire_size > max_bytes:
                            raise CorruptDocument(
                                f"download exceeds max_download_bytes={max_bytes}", source=url
                            )
                        return piece

                body = gzip.GzipFile(fileobj=WireLimit()) if encoding == "gzip" else response
                size = 0
                while piece := body.read(min(64 * 1024, max_bytes - size + 1)):
                    size += len(piece)
                    if size > max_bytes:
                        raise CorruptDocument(
                            f"download exceeds max_download_bytes={max_bytes}", source=url
                        )
                    handle.write(piece)
                if body is not response:
                    body.close()
                if encoding == "identity" and declared and size != int(declared):
                    raise CorruptDocument(
                        "HTTP response ended before its declared length", source=url
                    )
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            EOFError,
            http.client.HTTPException,
            zlib.error,
        ) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            if isinstance(exc, (CorruptDocument, UnsupportedFormat)):
                raise
            raise DocumentNotFound(f"cannot download ({exc})", source=url) from exc
        handle.seek(0)
        name = Path(urllib.parse.unquote(urllib.parse.urlsplit(final_url).path)).name
        if filename and (not name or not Path(name).suffix):
            name = filename.replace("\\", "/").rsplit("/", 1)[-1]
            name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127)
        if not name:
            name = parsed.hostname
        if media in ("text/html", "application/xhtml+xml"):
            if Path(name).suffix.lower() not in (".html", ".htm", ".xhtml"):
                name += ".html"
        elif not Path(name).suffix:
            suffix = {
                "text/html": ".html",
                "application/xhtml+xml": ".html",
                "application/pdf": ".pdf",
                "text/csv": ".csv",
                "text/plain": ".txt",
                "application/vnd.ms-excel": ".xls",
                "application/vnd.ms-excel.sheet.binary.macroenabled.12": ".xlsb",
                "application/vnd.oasis.opendocument.spreadsheet": ".ods",
            }.get(media, "")
            name += suffix
        yield Source(
            name=name,
            handle=handle,
            size=size,
            url=final_url,
            encoding=charset if media in ("text/html", "application/xhtml+xml") else None,
            notes=[f"source_url={final_url}"],
        )


def _zip_format(src: Source, head: bytes) -> str:
    """Which OOXML/ODF document is this ZIP, if any."""
    try:
        src.handle.seek(0)
        with zipfile.ZipFile(src.handle) as archive:
            infos = archive.infolist()
    except (zipfile.BadZipFile, OSError, EOFError, ValueError, NotImplementedError) as exc:
        # Spelled out rather than reusing `_zip_parts.NOT_OUR_VERDICT`, which every
        # other archive-read guard in the package now shares. That tuple means "this
        # member is unreadable, carry on"; this one is the opposite -- a verdict on
        # the whole document -- so it is chosen for what the verdict must *not*
        # swallow. `RuntimeError` is the reason it cannot be shared: it is how zipfile
        # reports an encrypted member, and turning that into `CorruptDocument` erases
        # the corrupt/encrypted distinction a triage queue is built on. `KeyError` is
        # absent because nothing here looks a member up by name.
        #
        # NotImplementedError is the one that does not look like it belongs: `zipfile`
        # raises it for a compression method CPython cannot decode (deflate64, or LZMA
        # on a build without the module) and for an unsupported ZIP version. It is a
        # statement about the *file*, not about unfinished code in diceo, and it
        # reaches here rather than the reader guard because detect() runs eagerly.
        raise CorruptDocument(
            f"starts like a ZIP-based document but the archive will not open ({exc})",
            source=src.name,
        ) from exc

    _refuse_duplicate_parts(infos, src.name)
    # The ODF mimetype member is stored uncompressed at a fixed offset by spec, so it
    # can be read straight out of the header we already have. This used to answer
    # *before* the archive was opened, which was cheap and left an .ods as the one
    # ZIP-based document with no guard on it at all -- a 3 GB member sails through a
    # spreadsheet and is refused in the identical .docx. The order it answers in is
    # unchanged; only the two checks in front of it are new.
    for mimetype, kind in _ODF_MIMETYPES.items():
        if mimetype in head:
            _check_zip_bomb(infos, src.name)
            return kind
    names = {info.filename for info in infos}
    for pattern, kind in _ZIP_PARTS:
        found = next((name for name in names if pattern.match(name)), None)
        if found:
            _check_zip_bomb(infos, src.name, part=found)
            return kind
    if "mimetype" in names:
        try:
            src.handle.seek(0)
            with (
                zipfile.ZipFile(src.handle) as archive,
                archive.open("mimetype") as member,
            ):
                # `read(120)`, not `read()[:120]`. The slice used to be applied to a
                # member that was already whole in memory, which put the one unbounded
                # inflate in the package *in front of* `_check_zip_bomb` -- a crafted
                # `.ods` declaring 3 GB of `mimetype` was inflated and only then
                # refused, by the guard that exists to stop exactly that. `ZipExtFile`
                # bounds both halves: it pulls at most a few KB of compressed bytes and
                # caps what the decompressor may produce from them, so the cost of this
                # read is a property of the call rather than of the file. The two
                # standard paths above read no member at all; this fallback tier is for
                # a package whose `mimetype` is not stored first, and 120 bytes is all
                # the magic it is looking for.
                declared = member.read(120)
        except NOT_OUR_VERDICT:
            # One member being unreadable says nothing about the package -- the tiers
            # below can still name it -- so this falls through with no answer rather
            # than raising. Unlike the archive-will-not-open case above, which *is* a
            # verdict, this one is free to catch `RuntimeError`, and had to start:
            # `detect()` runs eagerly, in front of `_guard`, so an ODF package whose
            # `mimetype` member is ZIP-encrypted escaped as a raw RuntimeError, which
            # errors.py's contract ("a DiceoError, or chunks, never a third thing")
            # forbids.
            declared = b""
        for mimetype, kind in _ODF_MIMETYPES.items():
            if mimetype in declared:
                _check_zip_bomb(infos, src.name)
                return kind
        if b"epub" in declared:
            return "epub"
    if "META-INF/container.xml" in names:
        return "epub"
    return "zip"


def _refuse_duplicate_parts(infos: list[zipfile.ZipInfo], name: str) -> None:
    """Refuse an OOXML package that names the same part twice.

    A ZIP may legally hold two members with one name; OPC may not, and `zipfile`
    resolves the ambiguity by returning **the last one written**. So a package
    carrying a real `word/document.xml` followed by an empty one extracts as an empty
    document -- no exception, no diagnostic, `lost_data` False. That is rule 3's worst
    case: the caller's index simply does not contain the document, and nothing says so.

    Refusing is the honest answer rather than picking a copy, because the two copies
    are two different documents and only the producer knows which was meant. Compared
    case-insensitively: OPC part names are case-insensitive, and Windows-built
    packages differing only in case hit the same ambiguity.
    """
    lowered = [info.filename.lower() for info in infos]
    if len(lowered) == len(set(lowered)):
        return
    seen: set[str] = set()
    duplicates = sorted({part for part in lowered if part in seen or seen.add(part)})
    raise CorruptDocument(
        f"names {len(duplicates)} part(s) more than once (first: {duplicates[0]!r}) -- "
        f"different readers pick different copies, so the file has no single meaning",
        source=name,
    )


def _check_zip_bomb(infos: list[zipfile.ZipInfo], name: str, part: str = "") -> None:
    """Refuse an archive whose central directory already admits to being a bomb.

    Checked before reading, from the metadata, which is the only point at which
    refusing is free. A 42 KB file that claims 4.5 GB of ``document.xml`` is not a
    document we should try to be clever about.

    Every member is measured, not only the one the detector matched: the part that
    ends up in memory is rarely the main one. ``styles.xml`` is read whole by every
    ``.docx`` we open, and a package whose main part is unremarkable can still put
    hundreds of megabytes there -- which is why `_EAGER_PARTS` carries its own,
    much lower ceiling.

    The per-member ceilings are also summed, because on their own they compose into
    a hole: an archive that stays one byte under each of them in 300 separate parts
    passes every test and still declares 17.6 GB. See `MAX_TOTAL_BYTES`.
    """
    # The selected main part comes from this same central-directory list. Verify it
    # while doing the mandatory safety walk instead of rebuilding every member name
    # into a second set afterwards. Besides the extra pass, that set briefly scales
    # with package complexity -- exactly the shape this guard exists to bound.
    if len(infos) > MAX_MEMBERS:
        raise CorruptDocument(
            f"declares {len(infos):,} members, over diceo's {MAX_MEMBERS:,} ceiling "
            f"-- an archive this fragmented is not one document",
            source=name,
        )
    found_part = not part
    total = 0
    for info in infos:
        if info.filename == part:
            found_part = True
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise CorruptDocument(
                f"declares over {MAX_TOTAL_BYTES / 1024**3:.0f} GB uncompressed across "
                f"its members, whatever any single one of them claims -- this is shaped "
                f"like a zip bomb",
                source=name,
            )
        if info.file_size > MAX_MEMBER_BYTES:
            raise CorruptDocument(
                f"{info.filename} declares {info.file_size / 1024**3:.1f} GB "
                f"uncompressed, over diceo's {MAX_MEMBER_BYTES / 1024**3:.0f} GB "
                f"per-part ceiling",
                source=name,
            )
        if info.file_size > MAX_EAGER_MEMBER_BYTES and _EAGER_PARTS.search(info.filename):
            raise CorruptDocument(
                f"{info.filename} declares {info.file_size / 1024**2:.0f} MB "
                f"uncompressed, over diceo's {MAX_EAGER_MEMBER_BYTES // 1024**2} MB "
                f"ceiling for a part that has to be read whole rather than streamed "
                f"(the largest one in a real document measured here is 0.7 MB) -- "
                f"re-save or convert the file (soffice --convert-to docx) if it is "
                f"genuine",
                source=name,
            )
        if info.file_size < BOMB_MIN_BYTES or not info.compress_size:
            continue
        ratio = info.file_size / info.compress_size
        if ratio > MAX_COMPRESSION_RATIO:
            raise CorruptDocument(
                f"{info.filename} expands {ratio:.0f}x ({info.compress_size:,} bytes "
                f"to {info.file_size:,}), over diceo's {MAX_COMPRESSION_RATIO}x "
                f"limit for a part above {BOMB_MIN_BYTES // 1024**2} MB -- this is "
                f"shaped like a zip bomb",
                source=name,
            )
    if not found_part:  # pragma: no cover - caller checked
        raise CorruptDocument(f"missing {part}", source=name)


def _looks_like_email(head: bytes) -> bool:
    """RFC 5322 headers at the very start, which is what an ``.eml`` file is.

    Deliberately strict: a *Received* or *From* line in the first few lines, and
    nothing that is not a header before it. Prose that happens to contain a colon
    must not be mistaken for mail.
    """
    try:
        text = head[:1024].decode("ascii", "strict")
    except UnicodeDecodeError:
        return False
    seen = 0
    for line in text.splitlines()[:12]:
        if not line:
            break
        if line[:1] in (" ", "\t"):  # header continuation
            continue
        name, _, _ = line.partition(":")
        if not _ or not name or not name.replace("-", "").isalnum():
            return False
        seen += 1
        lowered = name.lower()
        if lowered in ("received", "message-id", "mime-version"):
            return True
        if seen >= 2 and lowered in ("from", "to", "date", "subject"):
            return True
    return False


def _looks_like_html(head: bytes) -> bool:
    lowered = head[:2048].lstrip().lower()
    return lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml")) and (
        b"<html" in lowered or b"<head" in lowered or b"xhtml" in lowered
    )


def _looks_like_mhtml(head: bytes) -> bool:
    """A MIME archive, which is what "Save as Web Page, complete" writes.

    It is named ``.html`` (or ``.mht``), it holds the page plus every image as MIME
    parts, and read as HTML it indexes its own envelope: ``Content-Type``, the
    boundary strings, and base64 of the images as if they were prose. Measured on
    one such file, 1,201 of 1,201 characters in the single chunk were envelope and
    base64 -- the two real sentences were in there too, unfindable.

    It is mail by construction (RFC 2557 is a MIME message), so the mail reader is
    the honest route: it picks the ``text/html`` part, renders it, and reports the
    images as attachments.

    Both markers are required, in the header block, before any body: a page that
    merely mentions ``MIME-Version`` in its text must not be diverted.
    """
    try:
        text = head[:2048].decode("ascii", "strict")
    except UnicodeDecodeError:
        return False
    version = False
    multipart = False
    for line in text.splitlines()[:24]:
        if not line:
            break
        if line[:1] in (" ", "\t"):  # header continuation, e.g. a wrapped boundary
            lowered = line.lower()
            multipart = multipart or "boundary=" in lowered
            continue
        name, colon, value = line.partition(":")
        if not colon or not name or not name.replace("-", "").isalnum():
            return False
        lowered_name, lowered_value = name.lower(), value.lower()
        version = version or lowered_name == "mime-version"
        multipart = multipart or (
            lowered_name == "content-type" and "multipart/" in lowered_value
        )
    return version and multipart


#: Non-blank lines a head must show before the delimiter agreement below means
#: anything. Two lines of prose can agree on a comma count by accident; three
#: rarely do, and a spreadsheet export never has fewer.
_DELIMITED_MIN_LINES = 3


def _looks_delimited(head: bytes) -> bool:
    """Do these bytes parse as a table, or do they merely contain commas?

    ``delimited.sniff_delimiter`` cannot answer this on its own, which is the trap
    here: it *always* returns a delimiter -- ``","`` when nothing scored -- because
    its caller already knows the file is a CSV and only needs to know which
    character separates the fields. ``looks_delimited`` is the same scoring with the
    agreement test kept, and it lives next to the scoring so the two cannot drift.
    """
    # Function-local: text under a container name is rare, and `diceo.delimited`
    # pulls in `csv`, `plaintext` and `sheets`, which the 150 ms cold-import budget
    # will not spend on a case that almost never happens.
    from diceo.delimited import looks_delimited

    text = head.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.strip()]
    if lines and not text.endswith(("\n", "\r")):
        del lines[-1]  # the head cut this one mid-row; its field count is a lie
    if len(lines) < _DELIMITED_MIN_LINES:
        return False
    return looks_delimited(lines) is not None


def _text_under_a_container_name(src: Source, head: bytes, suffix: str) -> str:
    """The name promises a ZIP or OLE2 container and the bytes are text.

    Falling through to ``text`` -- what happened before this existed -- costs twice.
    A CSV export renamed ``.xlsx`` is read as prose, so it loses the header line
    repeated into every row group that the sheet path gives it (0.528 against 0.449,
    experiment 022 (formats and overlap)); and nothing anywhere
    says the file was not what it claimed. The second half is the one a caller
    cannot recover on their own: an export step quietly writing ``.xlsx`` over CSV
    is a fact about their pipeline, and every chunk looks healthy.
    """
    kind = "csv" if _looks_delimited(head) else "text"
    src.notes.append(
        f"extension_disagrees_with_content: {suffix} promises a binary Office "
        f"container and the bytes are plain text; read as {kind}"
    )
    return kind


def detect(src: Source) -> str:
    """Identify ``src``, or raise :class:`~diceo.errors.UnsupportedFormat`.

    Returns one of :data:`diceo.FORMATS`. Content decides wherever content can:
    a format with magic bytes is never identified by its extension.
    """
    # `open_source` stats carefully -- FIFO, device node, directory, permission -- and
    # then this read went unguarded, so a file that passes every one of those checks and
    # then fails on read (a network mount going away, a bad sector, EIO) escaped as a raw
    # OSError. The guarding has to be here rather than at the call: this runs eagerly, in
    # front of `_guard`, which only ever sees the reader generator.
    try:
        src.handle.seek(0)
        head = src.handle.read(4096)
        src.handle.seek(0)
    except (OSError, ValueError) as exc:
        raise CorruptDocument(f"cannot be read ({exc})", source=src.name) from exc
    suffix = Path(src.name).suffix.lower()

    if not head:
        # Every format, one answer. `.txt`, `.csv` and `.md` used to be waved through
        # here as "legitimately empty", and what a caller actually got was zero chunks,
        # no exception and an untouched `Diagnostics`: no note, no truncation,
        # `lost_data` False. That is the literal shape of the failure this library
        # exists to eliminate, and it contradicted `chunk()`'s own docstring, which
        # lists "an empty file" among the `CorruptDocument` cases raised **by the
        # call**. Eight zero-byte files in the fixture corpus took that path.
        #
        # Raising is the behaviour change, and it is the right one: a crawl that hands
        # us a zero-byte artifact wants to hear about it in the same `except
        # DiceoError` its empty PDFs already land in, not to receive an empty list
        # that looks exactly like a document with nothing to say.
        raise CorruptDocument("file is empty (0 bytes)", source=src.name)

    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        kind = _zip_format(src, head)
        if kind in _DECLINED:
            raise UnsupportedFormat(
                _DECLINED[kind], source=src.name, detected=kind, supported=SUPPORTED
            )
        return kind
    if head.startswith(b"PK") and len(head) > 4:
        # PK\x05\x06 is an empty archive; PK\x07\x08 a spanned one.
        raise CorruptDocument(
            "a ZIP header with no first member -- the archive is empty or split across volumes",
            source=src.name,
        )
    if head.startswith(_OLE2_MAGIC):
        # A password-protected .xlsx/.docx/.pptx is an OOXML package wrapped in an
        # OLE2 container, so it lands here -- and used to be told, with total
        # confidence, that it was "a pre-2007 Office document" it should "rename with
        # its real extension or convert with LibreOffice". Every word of that is
        # wrong, and it sends the reader to do work that cannot succeed. The one
        # thing they needed to hear -- *this file has a password* -- was the one
        # thing not said, and `EncryptedDocument` exists precisely for it.
        if _ole2_holds_encrypted_package(src):
            raise EncryptedDocument(
                "this is a password-protected Office document (an OOXML package "
                "encrypted inside an OLE2 container). diceo does not decrypt; "
                "supply the file with its protection removed",
                source=src.name,
            )
        # Otherwise: one container format, four possible documents inside. The
        # extension is the only cheap discriminator; the OLE directory would need a
        # parser.
        guess = {".xls": "xls", ".doc": "doc", ".ppt": "ppt", ".msg": "msg"}.get(suffix)
        if guess == "xls":
            return "xls"
        if guess in _DECLINED:
            raise UnsupportedFormat(
                _DECLINED[guess], source=src.name, detected=guess, supported=SUPPORTED
            )
        raise UnsupportedFormat(
            "this is an OLE2 compound file (a pre-2007 Office document). Only "
            "legacy .xls is supported; rename it with its real extension or "
            "convert it with LibreOffice",
            source=src.name,
            detected="ole2",
            supported=SUPPORTED,
        )
    if head.lstrip()[:5].lower() == b"{\\rtf":
        raise UnsupportedFormat(
            _DECLINED["rtf"], source=src.name, detected="rtf", supported=SUPPORTED
        )
    if _looks_like_html(head):
        return "html"

    # Past this point there are no magic bytes left, so the **extension leads**.
    # "Content wins over the file name" is the right rule only where content
    # *proves* something: `%PDF` and `PK\x03\x04` are proof, and a run of
    # `Name: value` lines is not. Sniffing first meant a `.txt` or `.md` file whose
    # first lines happened to look like mail headers -- a pasted message, a YAML
    # front-matter block -- was read as email, which is a surprise a caller cannot
    # debug from the outside. So an explicit text-ish extension is honoured, and the
    # header sniff below only decides files whose extension tells us nothing.
    if suffix in _EMAIL_SUFFIXES:
        return "eml"
    if suffix in _HTML_SUFFIXES:
        # ...with one exception, because it is the one case where the extension is
        # not the producer's opinion of the content: "Save as Web Page, complete"
        # writes a MIME archive and calls it `.html`. Narrowed to these suffixes on
        # purpose -- the sniff is content-first, and letting it override `.txt` or
        # `.md` would resurrect exactly the surprise this block exists to prevent.
        if _looks_like_mhtml(head):
            src.notes.append(
                'mhtml_archive: this is a MIME archive ("Save as Web Page, complete"), '
                "not a page -- read as mail, so its parts are separated and its images "
                "are reported as attachments rather than indexed as base64"
            )
            return "eml"
        return "html"
    if suffix in _DELIMITED_SUFFIXES:
        return "csv"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    if _looks_like_email(head):
        return "eml"
    if suffix == ".pdf":
        # PDF readers tolerate leading junk before %PDF, and files with a stripped
        # header are common enough that trying beats refusing on the magic alone.
        return "pdf"
    if _is_probably_text(head):
        if suffix in _CONTAINER_SUFFIXES:
            return _text_under_a_container_name(src, head, suffix)
        return "text"

    printable = head[:16]
    raise UnsupportedFormat(
        f"unrecognised format (first bytes {printable!r})",
        source=src.name,
        supported=SUPPORTED,
    )


def _is_probably_text(head: bytes) -> bool:
    """Would decoding this as text lose information?

    A NUL byte or a high proportion of undecodable bytes means binary, and reading
    a binary file as text is the silent-loss failure rule 3 exists for: it always
    "succeeds" and produces mojibake nobody can retrieve.
    """
    if b"\x00" in head:
        return False
    if head.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A multi-byte character split by the 4 KB boundary is not a failure.
        if exc.start < len(head) - 4:
            return False
    control = sum(1 for byte in head if byte < 9 or 13 < byte < 32)
    return control / max(len(head), 1) < 0.05


def sniff(source: str | Path | bytes | IO[bytes], *, limits: Limits | None = None) -> str:
    """Identify a document's format. Content wins over the file name.

    >>> from diceo import sniff
    >>> sniff("quarterly.xlsx")                      # doctest: +SKIP
    'xlsx'

    Returns one of :data:`diceo.FORMATS`. Raises
    :class:`~diceo.errors.UnsupportedFormat` for a format we can name but not
    read, with a message that says what it is and what to do about it.
    """
    options = (
        {}
        if limits is None
        else {
            "download_timeout": limits.download_timeout,
            "max_download_bytes": limits.max_download_bytes,
        }
    )
    with open_source(source, **options) as src:
        return detect(src)
