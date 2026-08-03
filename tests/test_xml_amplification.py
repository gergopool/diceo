"""Small files that used to cost gigabytes, and the two counters that stop them.

Three shapes, none of which the container guards in ``source`` can see, because all
three amplify on an axis other than bytes. Every one was reproduced end to end through
the public API before it was fixed, with ``Limits`` set, and every one was measured.

*A document type declaration.* expat resolves no external entities -- that much was
already true and is pinned below -- but it does expand internal ones, and its
billion-laughs limiter is a **ratio** (~100x) armed only past 8 MiB of output. Padding
the entity with literal filler that deflates to nothing therefore buys expansion in
proportion to the padding while keeping the ratio legal. Measured: a **19.8 KB** ``.docx``
declaring a 20 MB part reached **2.0 GB** of RSS and returned a chunk *with no error at
all*. ``Limits(max_seconds=1)`` was set; the whole cost is paid inside one ``iterparse``
call before the first block exists, which is where no budget can reach it.

*Nesting.* ``iterparse`` builds a tree, and a reader empties it only at the boundaries
it knows. A part that never closes anything retains one ``Element`` per start tag
whatever its tags are -- a 100 KB ``.docx`` of a million nested ``w:tbl`` reached 1.2 GB.

*Breadth.* The same thing sideways, and it needs its own counter because the two evade
each other: one ``w:p`` holding ten million ``w:r`` never reaches a clear, so depth stays
at 3. A 58 KB ``.xlsx`` whose single ``<row>`` holds two million ``<c>`` reached 623 MB.
The reverse -- nesting that closes a paragraph at every level, so a breadth counter
resets forever -- is `test_nesting_that_resets_the_breadth_counter_is_still_caught`,
which is the case that costs the second counter its keep.

Every fixture is built from the standard library, so this file runs on a fresh clone
with no corpus.
"""

from __future__ import annotations

import io
import zipfile

import pytest

import diceo

_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_SHEET_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_REL_NS = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_CONTENT_TYPES = (
    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/'
    '2006/content-types"/>'
)

#: Tight on purpose. Every case here must be refused *before* the budget matters; a
#: case that merely runs out of time is one where the cost was already paid.
_LIMITS = diceo.Limits(max_chars=1000, max_seconds=5)


def _rels(target: str) -> str:
    return (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/'
        'package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
        'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        f'Target="{target}"/></Relationships>'
    )


def _zipped(parts: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _docx(body: str = "", *, prolog: str = "", **parts: str) -> bytes:
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _rels("word/document.xml"),
        "word/document.xml": f'<?xml version="1.0"?>{prolog}<w:document {_W_NS}>'
        f"<w:body>{body or '<w:p><w:r><w:t>Revenue was 1200.</w:t></w:r></w:p>'}"
        "</w:body></w:document>",
        "word/styles.xml": f'<?xml version="1.0"?><w:styles {_W_NS}/>',
        "word/numbering.xml": f'<?xml version="1.0"?><w:numbering {_W_NS}/>',
    }
    members.update(parts)
    return _zipped(members)


_DEFAULT_SHEET = (
    '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>EMEA</t></is></c></row></sheetData>'
)


def _xlsx(sheet: str = "", *, prolog: str = "", **parts: str) -> bytes:
    members: dict[str, bytes | str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _rels("xl/workbook.xml"),
        "xl/workbook.xml": f'<?xml version="1.0"?><workbook {_SHEET_NS} {_REL_NS}>'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0"?><Relationships xmlns='
        '"http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
        'Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        'relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": f'<?xml version="1.0"?>{prolog}'
        f"<worksheet {_SHEET_NS}>{sheet or _DEFAULT_SHEET}</worksheet>",
    }
    members.update(parts)
    return _zipped(members)


def _refusal(payload: bytes, name: str) -> diceo.DiceoError:
    with pytest.raises(diceo.DiceoError) as caught:
        list(diceo.chunk(io.BytesIO(payload), name=name, limits=_LIMITS))
    return caught.value


#: A declaration whose entity is padded rather than nested. The padding is what keeps
#: the amplification *ratio* legal, and the ratio is the only thing expat bounds.
def _padded_bomb(megabytes: int = 4, copies: int = 20) -> str:
    return (
        '<!DOCTYPE d [<!ENTITY a "' + "A" * (megabytes * 1_000_000) + '">'
        '<!ENTITY b "' + "&a;" * copies + '">]>'
    )


# --------------------------------------------------------------------------- #
# 1. the declaration, in every part that is parsed
# --------------------------------------------------------------------------- #


def test_a_padded_entity_bomb_in_the_body_is_refused() -> None:
    payload = _docx("<w:p><w:r><w:t>&b;</w:t></w:r></w:p>", prolog=_padded_bomb())

    assert len(payload) < 100_000, "the fixture is not actually an amplifier"
    assert "document type" in str(_refusal(payload, "bomb.docx"))


def test_the_refusal_names_the_part_it_came_from() -> None:
    detail = str(_refusal(_docx(prolog=_padded_bomb()), "bomb.docx"))

    assert "word/document.xml" in detail
    assert "no office format writes one" in detail


def _replacing(payload: bytes, part: str, content: str) -> bytes:
    """``payload`` with one member's bytes swapped, everything else untouched."""
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members: dict[str, bytes | str] = {
            name: archive.read(name) for name in archive.namelist()
        }
    members[part] = content
    return _zipped(members)


@pytest.mark.parametrize("part", ["word/styles.xml", "word/numbering.xml"])
def test_a_declaration_in_a_supporting_part_is_refused_too(part: str) -> None:
    """Refused from every part, not only the body.

    A supporting part normally falls through to a fallback tier when it cannot be
    read, and that tiering is right for damage. It is wrong here: the fallback would
    parse the same hostile bytes a second way.
    """
    tag = "styles" if part.endswith("styles.xml") else "numbering"
    hostile = f'<?xml version="1.0"?>{_padded_bomb()}<w:{tag} {_W_NS}/>'

    assert part in str(_refusal(_replacing(_docx(), part, hostile), "bomb.docx"))


def test_a_declaration_in_the_package_relationships_is_refused() -> None:
    """``_rels/.rels`` is parsed only when the conventional part name is absent.

    Word Online and SharePoint write ``word/document2.xml``, which is the whole reason
    that tier exists -- so the fixture has to be one of those to reach the parse at
    all. A declaration sitting in the relationships of an otherwise conventional
    package is never read, and needs no verdict.
    """
    payload = _docx()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members: dict[str, bytes | str] = {
            name: archive.read(name) for name in archive.namelist()
        }
    members["word/document2.xml"] = members.pop("word/document.xml")
    members["_rels/.rels"] = (
        f'<?xml version="1.0"?>{_padded_bomb()}' + _rels("word/document2.xml").split("?>", 1)[1]
    )

    assert "_rels/.rels" in str(_refusal(_zipped(members), "bomb.docx"))


def test_a_declaration_in_a_worksheet_is_refused() -> None:
    payload = _xlsx(prolog=_padded_bomb())

    assert "xl/worksheets/sheet1.xml" in str(_refusal(payload, "bomb.xlsx"))


def test_an_external_entity_is_still_never_resolved() -> None:
    """The half that was already true, pinned so a future parser swap cannot lose it."""
    payload = _docx(
        "<w:p><w:r><w:t>&xxe;</w:t></w:r></w:p>",
        prolog='<!DOCTYPE d [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    )

    detail = str(_refusal(payload, "xxe.docx"))
    assert "document type" in detail
    assert "root" not in detail, "no part of /etc/passwd may appear in the message"


# --------------------------------------------------------------------------- #
# 2. nesting and breadth
# --------------------------------------------------------------------------- #


def test_a_million_nested_tables_are_refused() -> None:
    payload = _docx("<w:tbl><w:tr><w:tc>" * 200_000)

    assert len(payload) < 200_000, "the fixture is not actually an amplifier"
    assert "nests elements more than" in str(_refusal(payload, "deep.docx"))


def test_one_row_of_two_million_cells_is_refused() -> None:
    payload = _xlsx(
        '<sheetData><row r="1">' + "<c><v>1</v></c>" * 1_000_000 + "</row></sheetData>"
    )

    assert len(payload) < 200_000, "the fixture is not actually an amplifier"
    assert "unclosed elements" in str(_refusal(payload, "wide.xlsx"))


def test_a_merge_range_flood_is_refused() -> None:
    payload = _xlsx(
        '<sheetData><row r="1"><c><v>1</v></c></row></sheetData><mergeCells>'
        + '<mergeCell ref="A1:B2"/>' * 1_000_000
        + "</mergeCells>"
    )

    assert "unclosed elements" in str(_refusal(payload, "merges.xlsx"))


def test_nesting_that_resets_the_breadth_counter_is_still_caught() -> None:
    """The case that pays for the second counter.

    Every ``</w:p>`` returns the breadth counter to zero while every ``<w:tbl>`` stays
    open forever, so breadth alone never fires and the tree grows without limit.
    Measured on the single-counter build: **86 KB -> 1.1 GB**, and the only thing that
    ended it was expat's own nesting limit after 3.6 seconds.
    """
    payload = _docx("<w:tbl><w:p></w:p>" * 500_000)

    assert len(payload) < 200_000, "the fixture is not actually an amplifier"
    assert "nests elements more than" in str(_refusal(payload, "alternating.docx"))


# --------------------------------------------------------------------------- #
# 3. the controls: what none of this may do to a real document
# --------------------------------------------------------------------------- #


def test_an_ordinary_document_is_untouched() -> None:
    """A guard that fires on healthy input is noise within a day."""
    pieces = list(diceo.chunk(io.BytesIO(_docx()), name="quarterly.docx"))

    assert [piece.text for piece in pieces]
    assert any("1200" in piece.text for piece in pieces)


def test_an_ordinary_workbook_is_untouched() -> None:
    pieces = list(diceo.chunk(io.BytesIO(_xlsx()), name="quarterly.xlsx"))

    assert any("EMEA" in piece.text for piece in pieces)


def test_the_literal_text_doctype_inside_a_document_is_not_a_declaration() -> None:
    """The scan reads the prolog and stops at the root element.

    A document *about* XML says ``<!DOCTYPE html>`` in its own prose, and refusing it
    would be the guard inventing a corruption. This is why the scan is a small parser
    rather than ``b"<!DOCTYPE" in raw``.
    """
    body = (
        "<w:p><w:r><w:t>Every page begins with &lt;!DOCTYPE html&gt; in the "
        "prolog.</w:t></w:r></w:p>"
    )
    pieces = list(diceo.chunk(io.BytesIO(_docx(body)), name="about-xml.docx"))

    assert any("DOCTYPE" in piece.text for piece in pieces)


def test_a_comment_before_the_root_element_does_not_hide_a_declaration() -> None:
    """The other half of that: the scan steps over comments rather than giving up.

    Padding the prolog with a comment is the obvious way to walk a naive scan past the
    declaration, and a 512-byte peek would have been walked past by this fixture.
    """
    payload = _docx(
        prolog="<!-- " + "x" * 4000 + " -->" + _padded_bomb(),
    )

    assert "document type" in str(_refusal(payload, "commented.docx"))


def test_a_deeply_but_legally_nested_document_still_reads() -> None:
    """Word nests for layout constantly. The ceiling has to clear real documents."""
    depth = 30
    body = (
        "<w:tbl><w:tr><w:tc>" * depth
        + ("<w:p><w:r><w:t>Revenue was 1200.</w:t></w:r></w:p>")
        + "</w:tc></w:tr></w:tbl>" * depth
    )

    pieces = list(diceo.chunk(io.BytesIO(_docx(body)), name="nested.docx"))

    assert any("1200" in piece.text for piece in pieces)


def test_a_wide_but_legal_row_still_reads() -> None:
    """16,384 columns is the widest a worksheet may be, and it must not trip breadth."""
    cells = "".join(
        f'<c r="{chr(65 + (n % 26))}1" t="inlineStr"><is><t>{n}</t></is></c>'
        for n in range(4000)
    )
    pieces = list(
        diceo.chunk(
            io.BytesIO(_xlsx(f'<sheetData><row r="1">{cells}</row></sheetData>')),
            name="wide-but-legal.xlsx",
        )
    )

    assert pieces
