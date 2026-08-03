"""DOCX and PPTX: stream the one part that matters, keep the authored structure.

Decision D5. docx and pptx are ZIP + XML, so the fast path is ``zipfile`` plus a
pull parser over ``word/document.xml`` / ``ppt/slides/slideN.xml``, with no object
model built at any point. python-docx and python-pptx build an lxml DOM *and* a
tree of Python wrapper objects on top of it, and a retrieval pipeline then keeps
only the strings.

The reason to prefer this is not only speed. ``w:pStyle`` carries **real, authored
heading levels** -- ``Heading1``..``Heading9`` as the author set them -- so DOCX and
PPTX are the formats where diceo's structure is exactly right, the opposite of
PDF where it is inferred from font geometry.

What a naive scanner gets wrong, and what this module therefore does:

``w:tbl``
    Tables are emitted as ``table_row`` blocks with cells joined, not as loose
    paragraphs. A table read as paragraphs loses which values share a row, which
    is the only thing that made the table worth indexing.
``w:hyperlink``
    Link text is an ordinary run *inside* the hyperlink element. A scanner keyed
    on paragraph children misses it and silently drops every link's anchor text.
``w:br``
    Becomes a newline. Dropping it welds the words either side into one token.
``w:noBreakHyphen`` and ``w:sym``
    Characters that live in the *markup* instead of in a ``w:t``, and dropping them
    welds the words either side the same way: the IPBES French report indexed
    ``sous-régions`` as ``sousrégions``, and ``See<w:sym/>Appendix B`` came out
    ``SeeAppendix B``.
``w:gridSpan``
    A cell that covers several columns. Emitted as one field, it shifts every value
    after it in the row under the wrong header -- the defect the HTML path closed for
    ``colspan``.
``w:instrText`` and ``w:fldChar``
    Field codes. The instruction (``PAGEREF _Toc1 \\h``) is *not* document text;
    the cached result between ``separate`` and ``end`` is. Emitting the
    instruction puts ``PAGE \\* MERGEFORMAT`` in the index.
``w:footnoteReference`` / ``w:endnoteReference``
    Footnotes live in **separate parts**, so a reader of ``document.xml`` alone
    loses all of them without a word of warning -- the rule-3 failure exactly.
    The note parts are bounded by the number of notes (not by document size), so
    they are read up front and each note is emitted right after the paragraph that
    references it, preserving reading order.
``w:numPr``
    List numbering. The marker lives in ``numbering.xml``, not in the paragraph,
    so a bare scanner yields list items with no bullet and no number.
``w:delText``
    Tracked-change deletions. Excluded by construction, because deleted text is
    not in the document -- and it is a different tag, so a tag-keyed reader gets
    this right for free where a text-keyed one does not.
``w:txbxContent``
    A text box's paragraphs. They are ordinary ``w:p`` elements sitting *inside* a
    run of the paragraph the box is anchored to, so a reader with one paragraph
    accumulator splices the callout into the host sentence and then emits the
    host's remaining runs as a second, decapitated block. Each box gets its own
    accumulator frame.
``wp:docPr/@descr``
    A figure's alt text -- the only machine-readable form of a chart's message,
    and until experiment 038 (edge case mining) the one thing
    about an image we did *not* keep while counting the image itself.

Everything is a generator; nothing holds more than one paragraph (or one table
row) at a time.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple
from xml.etree.ElementTree import Element, iterparse

from diceo._zip_parts import MAX_DEPTH as _MAX_DEPTH
from diceo._zip_parts import MAX_OPEN_ELEMENTS as _MAX_OPEN_ELEMENTS
from diceo._zip_parts import MEDIA_DIR as _MEDIA_DIR
from diceo._zip_parts import NOT_OUR_VERDICT as _NOT_OUR_VERDICT
from diceo._zip_parts import OFF_REL as _OFF_REL
from diceo._zip_parts import dtd_free_stream as _dtd_free_stream
from diceo._zip_parts import refuse_dtd as _refuse_dtd
from diceo._zip_parts import refuse_shape as _refuse_shape

#: A chart's own part, and SmartArt's. **Counted, never opened.** A deck whose numbers
#: live in a chart -- which is what a chart is for -- indexes as an empty slide, and
#: `media_parts` does not see it because a chart is not a raster part. Reading them
#: would change chunk text, which is rule 4's decision and not this module's; saying
#: they are there is rule 3's, and that one is unconditional.
_CHART_PART = re.compile(r"(word|xl|ppt)/charts/chart\d*\.xml$", re.IGNORECASE)
_SMARTART_PART = re.compile(r"(word|xl|ppt)/diagrams/data\d*\.xml$", re.IGNORECASE)

__all__ = [
    "Block",
    "OoxmlDiagnostics",
    "iter_docx_blocks",
    "iter_pptx_blocks",
]

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.presentationml.org/2006/main}"
_P_ML = "{http://schemas.openxmlformats.org/presentationml/2006/main}"

# Qualified names, built once.
#
# These were written inline as `f"{_W}t"`, which is correct and reads well, and which
# formats a fresh string *on every comparison of every event*. The docx pull loop
# tests up to 15 of them per event and sees ~100,000 events on a 61 KB document, so
# that is hundreds of thousands of string builds per file for an answer that never
# changes. Hoisting them is invisible in the output -- proved by fingerprint -- and
# is the first thing to try before anything that alters behaviour.
_W_ABSTRACTNUM = _W + "abstractNum"
_W_ABSTRACTNUMID = _W + "abstractNumId"
_W_BR = _W + "br"
_W_CHAR = _W + "char"
_W_ENDNOTEREFERENCE = _W + "endnoteReference"
_W_FLDCHAR = _W + "fldChar"
_W_FLDCHARTYPE = _W + "fldCharType"
_W_FONT = _W + "font"
_W_FOOTNOTEREFERENCE = _W + "footnoteReference"
_W_GRIDSPAN = _W + "gridSpan"
_W_ID = _W + "id"
_W_ILVL = _W + "ilvl"
_W_INSTRTEXT = _W + "instrText"
_W_LVL = _W + "lvl"
_W_NAME = _W + "name"
_W_NOBREAKHYPHEN = _W + "noBreakHyphen"
_W_NUM = _W + "num"
_W_NUMFMT = _W + "numFmt"
_W_NUMID = _W + "numId"
_W_P = _W + "p"
_W_PSTYLE = _W + "pStyle"
_W_STYLE = _W + "style"
_W_STYLEID = _W + "styleId"
_W_SYM = _W + "sym"
_W_T = _W + "t"
_W_TAB = _W + "tab"
_W_TBL = _W + "tbl"
_W_TC = _W + "tc"
_W_TR = _W + "tr"
_W_TXBXCONTENT = _W + "txbxContent"
_W_VAL = _W + "val"
_A_BR = _A + "br"
_A_P = _A + "p"
_A_T = _A + "t"
_A_TBL = _A + "tbl"
_A_TR = _A + "tr"
_A_TC = _A + "tc"
#: `<p:sld show="0">` -- a slide the presenter chose not to show, which in practice
#: is routinely a superseded draft. Still indexed (rule 3 forbids dropping content)
#: but counted, the way hidden *sheets* already are, so a caller can decide.
_SLD_TAGS = frozenset({_P_ML + "sld", _P + "sld"})
_P_PH = _P + "ph"
_P_SP = _P + "sp"
_P_SLDID = _P_ML + "sldId"
#: Markup Compatibility. Word wraps every shape, textbox and SmartArt in an
#: `mc:AlternateContent` holding an `mc:Choice` for consumers that understand a
#: namespace and an `mc:Fallback` for those that do not. **Both carry the same text**,
#: so reading the element naively emits it twice, glued: `'Diagram captionDiagram
#: caption'`.
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_MC_ALTERNATE = _MC + "AlternateContent"
_MC_CHOICE = _MC + "Choice"
_MC_FALLBACK = _MC + "Fallback"
#: A tracked *move*. `w:delText` is excluded for free because deleted text uses its
#: own tag, and that argument does not extend this far: `w:moveFrom` holds the copy at
#: the **old** location in ordinary `w:t` runs, so a reader that only knows about
#: `w:del` emits the moved sentence twice.
_W_MOVEFROM = _W + "moveFrom"
#: OMML. An equation's characters live in `m:t`, a different namespace from `w:t` and
#: `a:t`, so a reader keyed on those drops every equation -- leaving
#: `'The relation is famous.'` where the document said `E=mc2`, which reads as complete
#: and is false. A paragraph of *display* maths (`m:oMathPara`, how every numbered
#: equation is written) produced no block at all. Flattened to characters rather than
#: converted to LaTeX: 020 measured markup at +0.2pp (p = 1.000), and an OMML converter
#: is a large dependency for no measured gain.
_M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
_M_T = _M + "t"
_M_OMATH = _M + "oMath"
#: WordprocessingDrawing -- the namespace of the *anchor*, not of the picture. Every
#: inline or floating drawing in a docx carries one `wp:docPr`, and its `descr`
#: attribute is the figure's alt text. `count_media` counted the image and threw the
#: only sentence describing it away.
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_WP_DOCPR = _WP + "docPr"
#: Frozensets, not tuples: this is tested on every end event, and building a
#: tuple there is exactly the per-event allocation experiment 036 measured.
_W_TEXT_TAGS = frozenset({_W_T, _M_T})
_A_TEXT_TAGS = frozenset({_A_T, _M_T})
#: Two presentationml namespace spellings are in the wild, so these are membership
#: tests rather than equality ones. Built once: the shape test runs on *every* start
#: event in a slide, and building a tuple plus a formatted string there was the
#: hottest line in the pptx path.
_SP_TAGS = frozenset({_P_ML + "sp", _P_SP})
_PH_TAGS = frozenset({_P_ML + "ph", _P_PH})

#: Every tag ``iter_docx_blocks``' start branch and end branch respectively act on,
#: so a tag in neither is dropped by one hash lookup instead of by falling through
#: the whole ``elif`` chain.
#:
#: This is the shape of the cost experiment 036 measured and did not remove: Word
#: wraps every run in ``w:rPr``, ``w:sz``, ``w:rFonts`` and ``w:lang``, so
#: ``doc-long.docx`` raises **99,533 events for 4,609 blocks** and the great majority
#: of them match nothing. 036 made each comparison cheap by hoisting 55 f-strings out
#: of the loops; these sets make most of the comparisons not happen at all.
#:
#: Each set is exactly the tags its branch tests, so the branch still decides every
#: event it used to decide. A tag added to one and not to the other would silently
#: stop being read -- which is why they are built from the same constants the branch
#: compares against, and sit here rather than being written out by hand.
_W_START_TAGS = frozenset(
    {_W_TBL, _W_TR, _W_TC, _W_TXBXCONTENT, _W_MOVEFROM, _MC_ALTERNATE, _MC_CHOICE, _MC_FALLBACK}
)
_W_END_TAGS = _W_TEXT_TAGS | {
    _M_OMATH,
    _W_TAB,
    _W_BR,
    _W_NOBREAKHYPHEN,
    _W_SYM,
    _W_MOVEFROM,
    _MC_FALLBACK,
    _MC_ALTERNATE,
    _W_INSTRTEXT,
    _W_FLDCHAR,
    _W_PSTYLE,
    _W_NUMID,
    _W_ILVL,
    _W_FOOTNOTEREFERENCE,
    _W_ENDNOTEREFERENCE,
    _WP_DOCPR,
    _W_TXBXCONTENT,
    _W_P,
    _W_TC,
    _W_GRIDSPAN,
    _W_TR,
    _W_TBL,
}
#: The same gate for the slide reader's start branch.
_PPTX_START_TAGS = (
    _SLD_TAGS | _SP_TAGS | {_A_TBL, _A_TR, _A_TC, _MC_ALTERNATE, _MC_CHOICE, _MC_FALLBACK}
)

DOCX_PART = "word/document.xml"

#: The relationship type that points at a package's main content part.
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)


def main_part(archive: zipfile.ZipFile, fallback: str = DOCX_PART) -> str:
    """The package's main content part, resolved the way the spec says.

    **Part names are not fixed.** Word Online and SharePoint write
    ``word/document2.xml``, and OPC part names are case-insensitive, so a hardcoded
    ``word/document.xml`` rejects real documents produced by real Microsoft software
    -- which this reader did until experiment 028 (hostile input audit)
    found it. The name lives in ``_rels/.rels``, on the relationship whose type is
    ``officeDocument``; python-docx is immune to this class of bug precisely because
    it looks there instead of guessing.

    Parsed with ``ElementTree``, not a regex. The regex this replaced carried two
    ``[^>]*`` runs and backtracked quadratically, so a 1 KB package holding an
    unclosed ``<Relationship`` repeated cost 13 seconds -- inside the C regex
    engine, where no `Limits` value can reach it. ElementTree also gets attribute
    order right for free, which is why there were two patterns rather than one.

    Three tiers, in decreasing authority: the relationship target, then a pattern
    scan of the archive, then `fallback`. The middle tier matters because a package
    can have both a non-standard part name *and* unreadable relationships, and
    refusing that loses a document whose content is sitting right there -- the
    detector already proved a ``word/document*.xml`` exists or we would not be here.
    """
    names = {name.lower(): name for name in archive.namelist()}
    try:
        rels = archive.read("_rels/.rels")
    except _NOT_OUR_VERDICT:
        # Unreadable relationships are exactly the case tier two exists for, so this
        # falls through to the pattern scan rather than deciding anything.
        rels = b""

    if rels:
        from xml.etree.ElementTree import ParseError, fromstring

        _refuse_dtd(rels, name=archive.filename or "", part="_rels/.rels")
        try:
            relationships = list(fromstring(rels))
        except (ParseError, ValueError):
            # Malformed relationships are tier two's case exactly as unreadable ones
            # are, so this decides nothing and falls through to the pattern scan.
            relationships = []
        for element in relationships:
            if element.get("Type", "") != _OFFICE_DOCUMENT_REL:
                continue
            # Targets are relative to the package root and may carry a leading "/".
            resolved = names.get(element.get("Target", "").lstrip("/").lower())
            if resolved:
                return resolved

    if fallback.lower() in names:
        return names[fallback.lower()]
    conventional = _MAIN_PART_SHAPES.get(fallback.lower())
    if conventional:
        for lowered, actual in sorted(names.items()):
            if conventional.match(lowered):
                return actual
    return fallback


#: Last-resort shapes for a main part, keyed by the conventional name asked for.
_MAIN_PART_SHAPES: dict[str, re.Pattern[str]] = {
    "word/document.xml": re.compile(r"word/document\d*\.xml$"),
    "ppt/presentation.xml": re.compile(r"ppt/presentation\d*\.xml$"),
    "xl/workbook.xml": re.compile(r"xl/workbook\d*\.xml$"),
}

_SLIDE = re.compile(r"ppt/slides/slide\d+\.xml$")


class Block(NamedTuple):
    """One retrieval-sized piece of text plus what is needed to place it.

    A NamedTuple, not a dataclass: there is one per paragraph and per table row,
    and on a large deck or a 300-page report that is tens of thousands of
    allocations.

    ``kind`` is one of ``heading``, ``paragraph``, ``list_item``, ``table_row``,
    ``footnote``, ``endnote``, ``slide_title``, ``note``, ``caption``.
    """

    kind: str
    text: str
    level: int = 0  # authored heading level for kind="heading", list depth for list_item
    part: int = 0  # slide number for pptx, 0 for docx
    row: int = -1  # position in the current table, -1 outside one


@dataclass
class OoxmlDiagnostics:
    """Rule 3: every skipped or unrepresentable thing gets counted."""

    paragraphs: int = 0
    headings: int = 0
    table_rows: int = 0
    list_items: int = 0
    footnotes: int = 0
    endnotes: int = 0
    field_instructions_skipped: int = 0
    slides: int = 0
    slides_without_text: list[int] = field(default_factory=list)
    notes_slides: int = 0
    media_parts: int = 0
    media_bytes: int = 0
    #: `charts/chart1.xml` and `diagrams/data1.xml` parts. Counted, not opened; see
    #: `_CHART_PART`. A note rather than a truncation, because a chart is a *rendering*
    #: of numbers that are usually also in a table on the same slide -- firing
    #: `lost_data` on every deck with a chart would train a caller to ignore it.
    charts: int = 0
    smartart: int = 0
    unresolved_list_markers: int = 0
    #: Slide numbers carrying ``show="0"``. Indexed anyway; see `_SLD_TAGS`.
    slides_hidden: list[int] = field(default_factory=list)
    #: `m:oMath` elements seen. Their characters *are* extracted (flattened); the count
    #: is here so a caller who needs typeset maths knows the document had some.
    equations: int = 0
    #: `w:fldChar` fields still open when their paragraph ended, i.e. abandoned. Each
    #: one was suppressing text; the count is how often that had to be broken out of.
    fields_unclosed: int = 0
    #: Tables nested inside a table cell. Counted because the flattening policy is a
    #: judgement call -- the inner rows are emitted as their own `table_row` blocks and
    #: the outer cell keeps its text -- and a caller seeing a high count knows the
    #: document uses tables for layout.
    nested_tables: int = 0
    #: `w:comment` elements in `word/comments.xml`. **Counted, not extracted.** They
    #: are content that is in the package and not in `document.xml` -- the footnote
    #: situation, which this module's docstring calls "the rule-3 failure exactly" --
    #: but unlike a footnote a comment is not part of the document a reader reads.
    #: Review chatter is noise in a production index and a reviewer's substantive note
    #: can be the answer to "why was this changed"; that is genuinely ambiguous, and
    #: the rule for ambiguous content here (hidden sheets, `display:none`) is to
    #: surface it rather than decide silently. Found on Microsoft's own SDK sample,
    #: where every one of five extractors reported no loss.
    comments: int = 0
    #: Runs inside `w:moveFrom` -- the copy of moved text at the location it left.
    #: Skipped, because emitting it duplicates the sentence.
    moved_runs_skipped: int = 0
    #: `w:sym` glyphs that became a space because nothing in `_SYMBOL_CHARS` names
    #: them. The mapped ones are deliberately **not** counted: a degree sign that is
    #: in the text is not a loss, and a counter that fires on every document with a
    #: symbol in it is one a caller stops reading.
    symbols_flattened: int = 0
    #: Cells carrying `w:gridSpan`, repeated into the columns they cover. Counted
    #: because the alternative reading -- one field, the rest of the row shifted -- is
    #: what a caller comparing diceo's row against the document will see, and
    #: because it is the only signal that the table had merged cells at all.
    merged_cells_expanded: int = 0
    #: Header and footer parts in the package. **Counted, not read** -- the policy is
    #: in `iter_docx_blocks`'s docstring and it is a good policy, but until 038 it was
    #: an *invisible* one: a document whose only mention of a contract number is in a
    #: page header produced no text and no note that a header existed. `header_bytes`
    #: is the uncompressed size from the central directory, so it is free, and it is
    #: what tells a caller whether the skipped parts held a line or a page.
    header_parts: int = 0
    footer_parts: int = 0
    header_bytes: int = 0
    footer_bytes: int = 0
    #: Paragraphs that came out of a `w:txbxContent`. Emitted -- a pull quote, a
    #: callout or a sidebar routinely carries a fact that is nowhere else in the
    #: document -- but counted, because they are *not* in the host paragraph's
    #: reading order and a caller reconstructing the prose wants to know.
    textbox_paragraphs: int = 0
    #: `wp:docPr` alt text emitted as a `caption` block.
    figure_alt_texts: int = 0
    #: Alt text Word wrote for itself and we refused to index; see `_MACHINE_ALT`.
    #: Counted rather than dropped silently, because "this figure has no *authored*
    #: description" is a real answer to "why is the chart not findable".
    figure_alt_texts_machine: int = 0
    parts_missing: list[str] = field(default_factory=list)
    #: The main content part, when it was *not* at the conventional name -- Word
    #: Online writes ``word/document2.xml``. Recorded because "we read a part you
    #: did not expect" is exactly the kind of thing a caller debugging a weird
    #: document wants to see, and because it is evidence the resolver did its job.
    main_part_resolved: str = ""


# --------------------------------------------------------------------------- #
# DOCX: styles, numbering, notes -- the small parts, read before the big one
# --------------------------------------------------------------------------- #

_HEADING = re.compile(r"heading[ _-]?([1-9])$", re.IGNORECASE)


def _style_levels(
    archive: zipfile.ZipFile,
) -> tuple[dict[str, int], dict[str, tuple[str, int]]]:
    """Map every style id in ``styles.xml`` to an outline level, and to
    numbering.

    Two maps, because ``styles.xml`` answers two questions a paragraph does not.

    *Heading level.* The *styleId* is localized by localized Word (German writes
    ``berschrift1``), so matching ``heading(\\d)`` against the id alone misses every
    heading in a German document. ``w:name`` is the language-independent name, so
    both are consulted.

    *List numbering.* This is the one that bites on ordinary documents: a
    paragraph styled ``List Bullet`` or ``List Number`` carries **no**
    ``w:numPr`` of its own -- the numbering reference lives on the style. A reader
    that only looks at the paragraph finds no list anywhere in a document full of
    lists, which is exactly what happened here before this map existed.
    """
    levels: dict[str, int] = {}
    numbering: dict[str, tuple[str, int]] = {}
    try:
        raw = archive.read("word/styles.xml")
    except KeyError:
        return levels, numbering
    from xml.etree.ElementTree import fromstring

    # One parse, two passes. Both loops asked `fromstring(raw)` for themselves, so a
    # 349 KB `styles.xml` -- what Word's default template actually ships -- was built
    # into an element tree twice per document, for two read-only walks over the same
    # elements. Neither loop writes to the tree or reads the other's map, so they are
    # free to share it. Measured on `big.docx` (349 KB `styles.xml`, 164 styles):
    # 22.3 ms -> 12.0 ms here, and 110 ms -> 100 ms for the whole `chunk()` call.
    # On `wordy.docx` the saving is real and invisible: a 10.8 MB body dwarfs it.
    _refuse_dtd(raw, name=archive.filename or "", part="word/styles.xml")
    root = fromstring(raw)

    for style in root.iter(_W_STYLE):
        style_id_for_num = style.get(_W_STYLEID)
        if style_id_for_num:
            reference = style.find(f"{_W}pPr/{_W}numPr")
            if reference is not None:
                num = reference.find(_W_NUMID)
                depth = reference.find(_W_ILVL)
                if num is not None and num.get(_W_VAL):
                    try:
                        level = int(depth.get(_W_VAL, "0")) if depth is not None else 0
                    except ValueError:
                        level = 0
                    numbering[style_id_for_num] = (num.get(_W_VAL, ""), level)

    for style in root.iter(_W_STYLE):
        style_id = style.get(_W_STYLEID)
        if not style_id:
            continue
        level = 0
        found = _HEADING.match(style_id)
        if found:
            level = int(found.group(1))
        else:
            name = style.find(_W_NAME)
            value = name.get(_W_VAL, "") if name is not None else ""
            found = _HEADING.match(value)
            if found:
                level = int(found.group(1))
            elif style_id == "Title" or value == "Title":
                level = 1
            elif style_id == "Subtitle" or value == "Subtitle":
                level = 2
        if level:
            levels[style_id] = level
        # An outline level set directly on the style is the fallback Word itself
        # uses when a custom style is styled as a heading without being named one.
        outline = style.find(f"{_W}pPr/{_W}outlineLvl")
        if outline is not None and style_id not in levels:
            with suppress(ValueError):
                levels[style_id] = int(outline.get(_W_VAL, "9")) + 1
    return levels, numbering


def _list_formats(archive: zipfile.ZipFile) -> dict[tuple[str, int], str]:
    """``(numId, ilvl)`` -> number format, from ``numbering.xml``.

    Two levels of indirection, both required: ``w:num`` maps a numId to an
    ``abstractNumId``, and the abstract definition holds one ``w:lvl`` per depth.
    Skipping the indirection (reading ``w:abstractNum`` as if its id were the
    numId) silently mislabels every list in a document with more than one.
    """
    try:
        raw = archive.read("word/numbering.xml")
    except KeyError:
        return {}
    from xml.etree.ElementTree import fromstring

    _refuse_dtd(raw, name=archive.filename or "", part="word/numbering.xml")
    root = fromstring(raw)
    abstract: dict[str, dict[int, str]] = {}
    for definition in root.iter(_W_ABSTRACTNUM):
        key = definition.get(_W_ABSTRACTNUMID, "")
        levels: dict[int, str] = {}
        for level in definition.iter(_W_LVL):
            try:
                depth = int(level.get(_W_ILVL, "0"))
            except ValueError:
                continue
            fmt = level.find(_W_NUMFMT)
            levels[depth] = fmt.get(_W_VAL, "decimal") if fmt is not None else "decimal"
        abstract[key] = levels

    formats: dict[tuple[str, int], str] = {}
    for num in root.iter(_W_NUM):
        num_id = num.get(_W_NUMID, "")
        link = num.find(_W_ABSTRACTNUMID)
        target = link.get(_W_VAL, "") if link is not None else ""
        for depth, fmt in abstract.get(target, {}).items():
            formats[(num_id, depth)] = fmt
    return formats


def _note_texts(archive: zipfile.ZipFile, part: str, tag: str) -> dict[str, str]:
    """Footnote or endnote id -> its text.

    Materialising this is safe in a way that materialising the body is not: it is
    bounded by the number of notes in the document, which is small and unrelated
    to page count. The separator/continuation pseudo-notes (ids <= 0) are skipped;
    they contain no authored text.
    """
    try:
        raw = archive.read(part)
    except KeyError:
        return {}
    from xml.etree.ElementTree import fromstring

    notes: dict[str, str] = {}
    _refuse_dtd(raw, name=archive.filename or "", part=part)
    for note in fromstring(raw).iter(f"{_W}{tag}"):
        identifier = note.get(_W_ID, "")
        try:
            if int(identifier) < 1:
                continue
        except ValueError:
            continue
        text = " ".join("".join(node.itertext()).strip() for node in note.iter(_W_P)).strip()
        if text:
            notes[identifier] = " ".join(text.split())
    return notes


# --------------------------------------------------------------------------- #
# DOCX body
# --------------------------------------------------------------------------- #


class _Para:
    """Accumulator for one paragraph's runs and the properties seen on the way.

    Runs are collected as a list and joined once, because a paragraph in a real
    document is a few dozen runs and string concatenation per run is the classic
    quadratic mistake in XML text extraction.
    """

    __slots__ = ("runs", "style", "num_id", "ilvl", "notes")

    def __init__(self) -> None:
        self.runs: list[str] = []
        self.style: str | None = None
        self.num_id: str | None = None
        self.ilvl: int = 0
        self.notes: list[tuple[str, str]] = []  # (kind, note id) in reference order

    def reset(self) -> None:
        self.runs.clear()
        self.style = None
        self.num_id = None
        self.ilvl = 0
        self.notes.clear()

    def text(self) -> str:
        joined = "".join(self.runs)
        # Collapse only runs of spaces/tabs that XML formatting introduced, while
        # keeping the newlines w:br produced -- they are authored line structure.
        if "\n" not in joined:
            # The overwhelmingly common shape: no `w:br`, so there is one line and
            # the split/join/strip round trip is `" ".join(joined.split())` written
            # the long way. `split()` already drops leading and trailing whitespace,
            # so the `.strip()` has nothing left to do either.
            return " ".join(joined.split())
        return "\n".join(" ".join(line.split()) for line in joined.split("\n")).strip()


_MARKER_STYLES = {
    "bullet": "- ",
    "none": "",
}


def count_media(archive: zipfile.ZipFile, report: OoxmlDiagnostics) -> None:
    """Count the parts diceo does not read: images, then charts and SmartArt.

    This existed for ``ppt/media/`` only, so a Word report with seven charts and an
    Excel workbook with one both reported **nothing** -- and `lost_data` stayed False.
    Docling marks 100% of figures in every format and MarkItDown does for html/docx/pptx;
    we were the only tool in experiment 033 (adjudication)'s
    adjudication that left a caller unable to tell a figure was there at all.

    We still do not *read* the image: OCR is out of scope, and the diagnostics are how a
    caller finds the documents that need it. Rule 3 only requires that the loss be
    counted rather than silent, and the same argument reaches
    one part further: a chart is not a raster, so ``media_parts`` never saw it, and a
    slide whose only content is a chart came out empty with nothing said. One pass over
    the central directory answers all three questions, so the counters are free.
    """
    for info in archive.infolist():
        if _MEDIA_DIR.match(info.filename):
            report.media_parts += 1
            report.media_bytes += info.file_size
        elif _CHART_PART.match(info.filename):
            report.charts += 1
        elif _SMARTART_PART.match(info.filename):
            report.smartart += 1


#: `<w:comment ...>` openers. A byte count rather than a parse: the part is small, the
#: only question asked of it is "how many", and a malformed comments part must not be
#: able to stop a readable document from being read.
_COMMENT_OPEN = re.compile(rb"<w:comment[\s>]")
#: `word/header1.xml`, `word/footer2.xml`. Anchored on the last path segment so a
#: media file called `header.png` cannot register.
_HEADER_FOOTER_PART = re.compile(r"(?:^|/)(header|footer)\d*\.xml$", re.IGNORECASE)


def count_comments(archive: zipfile.ZipFile, report: OoxmlDiagnostics) -> None:
    """Count review comments. See ``OoxmlDiagnostics.comments`` for why not extract."""
    try:
        body = archive.read("word/comments.xml")
    except _NOT_OUR_VERDICT:
        # A malformed or unreadable comments part must not stop a readable document
        # from being read; the count is simply not available.
        return
    report.comments += len(_COMMENT_OPEN.findall(body))


def count_headers_footers(archive: zipfile.ZipFile, report: OoxmlDiagnostics) -> None:
    """Record the header and footer parts we deliberately do not read.

    Sizes come from the central directory, so this costs no decompression. Matching
    on the part name rather than the relationship type is the cheap approximation:
    every producer writes ``word/header1.xml``, and under-counting a diagnostic is a
    smaller failure than reading and parsing a rels part on every document.
    """
    for info in archive.infolist():
        matched = _HEADER_FOOTER_PART.search(info.filename)
        if not matched:
            continue
        if matched.group(1).lower() == "header":
            report.header_parts += 1
            report.header_bytes += info.file_size
        else:
            report.footer_parts += 1
            report.footer_bytes += info.file_size


#: Word's own alt text, which it writes into `descr` without asking and which every
#: version since 2019 writes by default. Measured on the 51 real DOCX files in the
#: research corpus: **18 of 18** non-empty `descr` values are this -- 'A close-up of a chart
#: Description automatically generated', 'A white sheet with black text Description
#: automatically generated with medium confidence'. On a real NASA deck MarkItDown
#: indexed 'A helicopter flying over a city Description automatically generated' as
#: document content. It describes pixels, not meaning; indexing it puts a sentence in
#: the chunk that the document does not say.
_MACHINE_ALT = re.compile(r"description\s+automatically\s+generated", re.IGNORECASE)
#: The other half: a producer that copies the shape's *name* into `descr`, which
#: yields 'Picture 1' or 'Text Box 520001818' -- the id, spelled out. A trailing
#: number is part of the pattern, not an exception to it.
_GENERATOR_ALT = re.compile(
    r"^(?:picture|image|imagen|figure|chart|graph|graphic|diagram|drawing|shape|"
    r"text\s*box|textbox|group|rectangle|oval|line|arrow|canvas|object|"
    r"content\s+placeholder|placeholder)[\s._-]*\d*$",
    re.IGNORECASE,
)


def _alt_text(element: Element, report: OoxmlDiagnostics) -> str:
    """The authored alt text on a `wp:docPr`, or `''` if Word wrote it.

    `descr` before `title`: `descr` is the field Word's "Alt Text" pane writes and
    the one a screen reader announces, while `title` is a short label that is empty
    far more often. Newlines are collapsed because Word puts `\\n\\n` in front of its
    boilerplate and a caption is one line.
    """
    value = " ".join((element.get("descr") or element.get("title") or "").split())
    if not value:
        return ""
    if _MACHINE_ALT.search(value) or _GENERATOR_ALT.match(value):
        report.figure_alt_texts_machine += 1
        return ""
    report.figure_alt_texts += 1
    return value


#: What a `w:sym` from the Symbol font stands for, keyed by the font's own byte.
#: `w:sym` picks a glyph out of a symbol-encoded font, so the code point in `w:char`
#: means nothing without the font -- `F0E0` is a lozenge in Symbol and a right arrow
#: in Wingdings, which is why this is keyed on the font and not on the code point.
#: Only Symbol is mapped, and only these fourteen: they are the ones that sit *inside*
#: a token in prose (`80°C`, `±5%`, `µg/mL`), where the space everything else falls
#: back to would split the token in two. A dingbat -- a checkbox, a pointing hand --
#: is no query's term, so the separator is the entire benefit there.
#:
#: Read off the encoding vector of URW's StandardSymbolsPS -- the metric-compatible
#: Symbol clone from the URW base-35 font set -- rather than copied from anyone's
#: table. Wingdings could not be verified the same way, so it is not mapped: an
#: unverified arrow is worth less than an honest space.
_SYMBOL_CHARS = {
    0x6D: "μ",  # mu -- U+03BC, the one NFKC folds the micro sign onto
    0xA3: "≤",  # lessequal
    0xAB: "↔",  # arrowboth
    0xAC: "←",  # arrowleft
    0xAD: "↑",  # arrowup
    0xAE: "→",  # arrowright
    0xAF: "↓",  # arrowdown
    0xB0: "°",  # degree
    0xB1: "±",  # plusminus
    0xB3: "≥",  # greaterequal
    0xB4: "×",  # multiply
    0xB8: "÷",  # divide
    0xB9: "≠",  # notequal
    0xBB: "≈",  # approxequal
}

#: Upper bound on how far one `w:gridSpan` may expand a cell. `w:val="1000000"` is a
#: hostile file, and repeating a cell into a million columns is a row that does not
#: fit in memory -- the same cap, for the same reason, as the HTML path's `colspan`.
_MAX_GRID_SPAN = 64


def _symbol_char(element: Element, report: OoxmlDiagnostics) -> str:
    """The character a ``w:sym`` stands for, or a space.

    A space and never nothing: the element used to be dropped outright, and with it
    the boundary, so ``See<w:sym/>Appendix B`` reached the index as ``SeeAppendix B``
    -- one token that no query contains, in place of two that a query does.
    """
    if (element.get(_W_FONT) or "").lower().startswith("symbol"):
        try:
            code = int((element.get(_W_CHAR) or "").strip(), 16)
        except ValueError:
            code = -1
        # `F0B0` and `B0` are the same glyph: Word writes the private-use code point
        # the font's cmap maps into, other producers write the font byte itself.
        mapped = _SYMBOL_CHARS.get(code & 0xFF) if code >= 0 else None
        if mapped:
            return mapped
    report.symbols_flattened += 1
    return " "


def iter_docx_blocks(
    path: str | Path,
    *,
    diagnostics: OoxmlDiagnostics | None = None,
    part: str = DOCX_PART,
    include_notes: bool = True,
) -> Iterator[Block]:
    """Stream ``word/document.xml`` as structured blocks.

    Headers and footers are deliberately *not* read: they repeat on every page and
    indexing them adds a copy of the same boilerplate to every chunk of the
    document. Footnotes and endnotes *are* read, because they carry unique
    content. Both choices are recorded rather than assumed -- and since
    experiment 038 (edge case mining) the header/footer choice is
    *reported* as well, because a policy a caller cannot see is indistinguishable
    from a bug when the missing sentence was in a header.
    """
    report = diagnostics if diagnostics is not None else OoxmlDiagnostics()
    with zipfile.ZipFile(path) as archive:
        count_media(archive, report)
        count_comments(archive, report)
        count_headers_footers(archive, report)
        names = set(archive.namelist())
        if part not in names:
            # Not necessarily missing -- Word Online writes `word/document2.xml`, and
            # part names are case-insensitive. Ask the relationships before giving up.
            resolved = main_part(archive, fallback=part)
            if resolved != part and resolved in names:
                report.main_part_resolved = resolved
                part = resolved
            else:
                report.parts_missing.append(part)
                return
        levels, style_numbering = _style_levels(archive)
        formats = _list_formats(archive)
        footnotes = (
            _note_texts(archive, "word/footnotes.xml", "footnote") if include_notes else {}
        )
        endnotes = _note_texts(archive, "word/endnotes.xml", "endnote") if include_notes else {}
        counters: dict[tuple[str, int], int] = {}

        para = _Para()
        #: One saved accumulator per open `w:txbxContent`. A text box's paragraphs are
        #: real `w:p` elements *inside a run of the host paragraph*, so with a single
        #: accumulator the inner `</w:p>` flushed **the host's runs so far together
        #: with the box's**, and reset the host's style with them: measured, a callout
        #: anchored in a heading produced the single block
        #: `'Chapter 3 FindingsSidebar: 3.2 million hectares.'` at heading level 1, and
        #: a callout anchored mid-paragraph produced
        #: `'Revenue grew 12 percent in 2024. Pull quote: the Arctic route opened in
        #: 2019.'` followed by the orphaned `'The board approved the plan.'`. A stack,
        #: because Word nests a text box inside a text box.
        #:
        #: The saved table depth is what tells "a paragraph of the box, anchored in a
        #: cell" (not cell content) from "a paragraph of a table *inside* the box"
        #: (cell content). Without it a box holding a table lost every row.
        para_frames: list[tuple[_Para, int]] = []
        # `suppressed` is non-zero wherever run text is not document content: inside a
        # field *instruction*, inside the `w:moveFrom` copy of moved text, and inside
        # an `mc:Fallback` whose `mc:Choice` we already read.
        suppressed = 0
        #: Open `w:fldChar` fields, kept **separate** from `suppressed` and reset at the
        #: end of every paragraph. Unbounded, one unbalanced `begin` -- a field Word
        #: left half-written -- suppressed every run for the rest of the part: measured,
        #: a document with five paragraphs of content emitted zero blocks, with no
        #: exception and nothing in diagnostics. A field *instruction* is inside one
        #: paragraph in every real document (for a complex field the `begin`,
        #: `instrText` and `separate` are together, and only the cached result spans
        #: paragraphs, after suppression is already lifted), so closing it at the
        #: paragraph boundary is right for real files and caps a broken one's damage.
        field_depth = 0
        #: One entry per open `mc:AlternateContent`: has its `mc:Choice` been seen?
        alternates: list[bool] = []
        #: One entry per open `mc:Fallback`: did it raise `suppressed`? Popped on the way
        #: out so nesting cannot leave the counter unbalanced and mute the rest of the
        #: document.
        fallbacks: list[bool] = []
        cell_texts: list[str] = []
        row_index = -1
        in_table = 0
        pending_cells = False
        #: Where the open `w:tc` started writing into `cell_texts`, and the columns it
        #: says it covers -- `None` until a `w:gridSpan` says otherwise. Both are read
        #: at `</w:tc>`, which is the first moment the cell's text is complete.
        cell_start = 0
        cell_span: int | None = None
        #: One saved ``(cells, row index, in-a-cell, cell start, cell span)`` per
        #: enclosing table. Word nests tables for layout constantly, and with a single
        #: set of scalars the inner table's first row wiped the outer row's cells.
        frames: list[tuple[list[str], int, bool, int, int | None]] = []
        #: See `_MAX_DEPTH` and `_MAX_OPEN_ELEMENTS`. Both count *every* element,
        #: not only the ones this loop dispatches on: an unknown tag is skipped by
        #: the dispatch and retained by the tree exactly like a known one.
        depth = 0
        open_elements = 0
        #: Hoisted into locals: both are read once per element, and a module global
        #: costs a dict lookup where a local costs an array index. Measured at ~4%
        #: of the whole docx and xlsx read before this line existed.
        max_depth = _MAX_DEPTH
        max_open = _MAX_OPEN_ELEMENTS

        with archive.open(part) as stream:
            guarded = _dtd_free_stream(stream, name=archive.filename or "", part=part)
            for event, element in iterparse(guarded, ("start", "end")):
                tag = element.tag
                if event == "start":
                    depth += 1
                    open_elements += 1
                    if depth > max_depth or open_elements > max_open:
                        _refuse_shape(
                            depth, open_elements, name=archive.filename or "", part=part
                        )
                    if tag not in _W_START_TAGS:
                        continue
                    if tag == _W_TBL:
                        # Push the outer table's frame. Without this the inner
                        # table's first `w:tr` cleared `cell_texts` and the outer
                        # row's cells were gone -- for good, with lost_data False.
                        if in_table:
                            frames.append(
                                (cell_texts, row_index, pending_cells, cell_start, cell_span)
                            )
                            report.nested_tables += 1
                        in_table += 1
                        cell_texts = []
                        row_index = -1
                        pending_cells = False
                        cell_start = 0
                        cell_span = None
                    elif tag == _W_TR and in_table:
                        row_index += 1
                        cell_texts = []
                        pending_cells = True
                    elif tag == _W_TC and in_table:
                        cell_start = len(cell_texts)
                        cell_span = None
                    elif tag == _W_TXBXCONTENT:
                        # Everything until the matching end belongs to the box, not
                        # to the paragraph it is anchored to.
                        para_frames.append((para, in_table))
                        para = _Para()
                    elif tag == _W_MOVEFROM:
                        # The copy at the location the text moved *away* from.
                        suppressed += 1
                        report.moved_runs_skipped += 1
                    elif tag == _MC_ALTERNATE:
                        alternates.append(False)
                    elif tag == _MC_CHOICE:
                        if alternates:
                            alternates[-1] = True
                    elif tag == _MC_FALLBACK:
                        # Only a *second* rendering is a duplicate. An
                        # `AlternateContent` carrying nothing but a fallback is the
                        # only copy there is, and skipping it would lose the text.
                        if alternates and alternates[-1]:
                            suppressed += 1
                            fallbacks.append(True)
                        else:
                            fallbacks.append(False)
                    continue

                # --- end events ---
                depth -= 1
                if tag not in _W_END_TAGS:
                    continue
                if tag in _W_TEXT_TAGS:
                    if not (suppressed or field_depth) and element.text:
                        para.runs.append(element.text)
                elif tag == _M_OMATH:
                    report.equations += 1
                elif tag == _W_TAB:
                    para.runs.append("\t")
                elif tag == _W_BR:
                    para.runs.append("\n")
                elif tag == _W_NOBREAKHYPHEN:
                    # A character, not a hint. Dropped, it welded `sous` to
                    # `estimation` and to `régions` in the IPBES French report -- both
                    # times inside a word a query asks for. U+002D rather than the
                    # U+2011 the element literally is: rule 4 asks which token the
                    # query matches, and nobody types a non-breaking hyphen.
                    # `w:softHyphen` stays dropped; that one really is a line-break
                    # hint with no character behind it.
                    #
                    # Guarded like `w:t` and unlike `w:tab`/`w:br`, because a hyphen
                    # survives `.strip()` where their whitespace does not: appended
                    # inside the `mc:Fallback` copy of a text box, one would become a
                    # paragraph block reading `-`.
                    if not (suppressed or field_depth):
                        para.runs.append("-")
                elif tag == _W_SYM:
                    if not (suppressed or field_depth):
                        para.runs.append(_symbol_char(element, report))
                elif tag == _W_MOVEFROM:
                    suppressed = max(0, suppressed - 1)
                elif tag == _MC_FALLBACK:
                    if fallbacks and fallbacks.pop():
                        suppressed = max(0, suppressed - 1)
                elif tag == _MC_ALTERNATE:
                    if alternates:
                        alternates.pop()
                elif tag == _W_INSTRTEXT:
                    # Never content. Counted so the caller can see fields existed.
                    report.field_instructions_skipped += 1
                elif tag == _W_FLDCHAR:
                    kind = element.get(_W_FLDCHARTYPE)
                    if kind == "begin":
                        field_depth += 1
                    elif kind == "separate":
                        # The cached field *result* follows and is real text.
                        field_depth = max(0, field_depth - 1)
                    elif kind == "end":
                        field_depth = max(0, field_depth - 1)
                elif tag == _W_PSTYLE:
                    if para.style is None:
                        # First wins: a tracked-change (w:pPrChange) carries the
                        # *previous* style later in the same w:pPr.
                        para.style = element.get(_W_VAL)
                elif tag == _W_NUMID:
                    if para.num_id is None:
                        para.num_id = element.get(_W_VAL)
                elif tag == _W_ILVL:
                    try:
                        para.ilvl = int(element.get(_W_VAL, "0"))
                    except ValueError:
                        para.ilvl = 0
                elif tag == _W_FOOTNOTEREFERENCE:
                    identifier = element.get(_W_ID, "")
                    if identifier in footnotes:
                        para.notes.append(("footnote", identifier))
                elif tag == _W_ENDNOTEREFERENCE:
                    identifier = element.get(_W_ID, "")
                    if identifier in endnotes:
                        para.notes.append(("endnote", identifier))
                elif tag == _WP_DOCPR:
                    # A figure's alt text. Suppressed inside a rejected `mc:Fallback`
                    # or a field instruction for the same reason run text is.
                    if not (suppressed or field_depth):
                        alt = _alt_text(element, report)
                        if alt:
                            yield Block("caption", alt)
                elif tag == _W_TXBXCONTENT:
                    if para_frames:
                        para = para_frames.pop()[0]
                elif tag == _W_P:
                    if field_depth:
                        # Abandoned field. Closing it here is what stops one broken
                        # `begin` from muting every paragraph that follows.
                        report.fields_unclosed += field_depth
                        field_depth = 0
                    text = para.text()
                    notes = list(para.notes)
                    style, num_id, ilvl = para.style, para.num_id, para.ilvl
                    para.reset()
                    in_box = bool(para_frames) and in_table <= para_frames[-1][1]
                    if in_table and pending_cells and not in_box:
                        # Inside a table, a paragraph is cell content; the row is
                        # emitted when it closes. A text box *anchored* in a cell is
                        # not cell content -- appending it gave the cell
                        # `'Cell AFloating note.'`, one column's value with a
                        # floating callout welded onto it.
                        if text:
                            cell_texts.append(text)
                    elif text:
                        if in_box:
                            report.textbox_paragraphs += 1
                        level = levels.get(style or "", 0)
                        # A direct w:numPr wins; otherwise the paragraph's style may
                        # supply one (List Bullet / List Number carry it there).
                        if num_id is None and style in style_numbering:
                            num_id, ilvl = style_numbering[style]
                        if level:
                            report.headings += 1
                            yield Block("heading", text, level)
                        elif num_id is not None:
                            marker = _marker(num_id, ilvl, formats, counters, report)
                            report.list_items += 1
                            yield Block("list_item", marker + text, ilvl + 1)
                        else:
                            report.paragraphs += 1
                            yield Block("paragraph", text)
                    for kind, identifier in notes:
                        body = (footnotes if kind == "footnote" else endnotes)[identifier]
                        if kind == "footnote":
                            report.footnotes += 1
                        else:
                            report.endnotes += 1
                        yield Block(kind, body, 0)
                    element.clear()
                    open_elements = 0
                elif tag == _W_TC:
                    # Cell text was gathered by its paragraphs; what is left is the
                    # geometry. A cell that covers several columns has to *occupy*
                    # them, or the fields after it in the row sit under the wrong
                    # header: a `w:gridSpan="2"` header over a two-column table gave
                    # the row `'2024 results'` against `'Revenue | 12.4'`, and a
                    # mid-row span gave `'Region | 2024'` against
                    # `'North | 12.4 | 13.9'` -- 13.9 attributed to no header at all.
                    # Repeating the text into every column it covers is what the span
                    # means, and it is what the HTML path does for `colspan`.
                    if cell_span is not None and cell_span > 1 and pending_cells:
                        text = " ".join(cell_texts[cell_start:]).strip()
                        if text:
                            report.merged_cells_expanded += 1
                            del cell_texts[cell_start:]
                            cell_texts.extend([text] * cell_span)
                    cell_span = None
                elif tag == _W_GRIDSPAN:
                    if cell_span is None:
                        # First wins, as for `w:pStyle`: a `w:tcPrChange` carries the
                        # cell's *previous* geometry later in the same `w:tcPr`.
                        try:
                            span = int(element.get(_W_VAL, "1"))
                        except ValueError:
                            span = 1
                        cell_span = min(max(span, 1), _MAX_GRID_SPAN)
                elif tag == _W_TR:
                    if cell_texts:
                        report.table_rows += 1
                        yield Block("table_row", " | ".join(cell_texts), 0, 0, row_index)
                    cell_texts = []
                    pending_cells = False
                    element.clear()
                    open_elements = 0
                elif tag == _W_TBL:
                    in_table = max(0, in_table - 1)
                    if frames:
                        # Back to the cell the inner table was sitting in, so text
                        # after it is still that cell's content rather than a loose
                        # paragraph that has lost its row.
                        cell_texts, row_index, pending_cells, cell_start, cell_span = (
                            frames.pop()
                        )
                    else:
                        cell_texts = []
                        row_index = -1
                        pending_cells = False
                        cell_start = 0
                        cell_span = None
                    element.clear()
                    open_elements = 0


def _marker(
    num_id: str,
    ilvl: int,
    formats: dict[tuple[str, int], str],
    counters: dict[tuple[str, int], int],
    report: OoxmlDiagnostics,
) -> str:
    """The visible marker for a list paragraph.

    Known limitation, stated rather than hidden: numbering *restart* rules
    (``w:lvlRestart``, a new ``w:num`` overriding a start value) are not
    implemented, so a document with several restarting numbered lists sharing one
    numId will number them continuously. The text is never wrong, only the
    ordinal, and the alternative is reimplementing Word's numbering engine.
    """
    fmt = formats.get((num_id, ilvl))
    if fmt is None:
        report.unresolved_list_markers += 1
        return "- "
    if fmt in _MARKER_STYLES:
        return _MARKER_STYLES[fmt]
    key = (num_id, ilvl)
    counters[key] = counters.get(key, 0) + 1
    index = counters[key]
    if fmt == "lowerLetter":
        return f"{chr(96 + ((index - 1) % 26) + 1)}. "
    if fmt == "upperLetter":
        return f"{chr(64 + ((index - 1) % 26) + 1)}. "
    if fmt in {"lowerRoman", "upperRoman"}:
        roman = _roman(index)
        return f"{roman.lower() if fmt == 'lowerRoman' else roman}. "
    return f"{index}. "


_ROMAN = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]


def _roman(value: int) -> str:
    out: list[str] = []
    for amount, glyph in _ROMAN:
        while value >= amount:
            out.append(glyph)
            value -= amount
    return "".join(out)


# --------------------------------------------------------------------------- #
# PPTX
# --------------------------------------------------------------------------- #


def _slide_order(
    archive: zipfile.ZipFile, report: OoxmlDiagnostics
) -> tuple[list[str], list[str]]:
    """Slide parts in presentation order, plus the ones the presentation forgot.

    Filename order is a guess and it is wrong on any deck that has been edited:
    ``slideN.xml`` records *creation* order, and moving a slide rewrites
    ``p:sldIdLst`` without renaming a part. Slides in the zip but absent from
    ``sldIdLst`` are returned separately rather than dropped -- an unreferenced
    slide is still content, and losing it silently is the rule-3 failure.

    The *other* direction had no answer at all. The loop tested ``if target in
    present`` and let everything else fall off the end, so a deck whose slide parts
    are named in ``p:sldIdLst`` and **absent from the archive** -- a truncated
    download, a DLP filter, a repackaging script that dropped members -- produced
    zero blocks, zero slides, no exception and ``lost_data`` False. The caller was
    told nothing whatsoever; the document simply was not in their index. Every such
    slide now lands in ``parts_missing``, which the API routes through ``truncate``.
    """
    present = {name for name in archive.namelist() if _SLIDE.match(name)}

    def numeric(names: list[str]) -> list[str]:
        return sorted(names, key=lambda n: int(re.search(r"(\d+)", Path(n).stem).group(1)))

    try:
        rels = archive.read("ppt/_rels/presentation.xml.rels")
        presentation = archive.read("ppt/presentation.xml")
    except KeyError:
        return numeric(list(present)), []

    from xml.etree.ElementTree import fromstring

    name = archive.filename or ""
    _refuse_dtd(rels, name=name, part="ppt/_rels/presentation.xml.rels")
    _refuse_dtd(presentation, name=name, part="ppt/presentation.xml")
    targets: dict[str, str] = {}
    for relationship in fromstring(rels):
        identifier = relationship.get("Id")
        target = relationship.get("Target", "")
        if not identifier:
            continue
        targets[identifier] = (
            target[1:] if target.startswith("/") else posixpath.normpath(f"ppt/{target}")
        )

    ordered: list[str] = []
    #: A ``p:sldId`` whose relationship names a target the archive does not hold. The
    #: name is known and the archive does not have it, so this is not a guess.
    absent: list[str] = []
    #: A ``p:sldId`` whose ``r:id`` has no relationship at all. There is no target name
    #: to check against the archive, so it cannot be told apart from a slide whose
    #: relationship was merely dropped -- see the cancellation below.
    dangling: list[str] = []
    for element in fromstring(presentation).iter(_P_SLDID):
        identifier = element.get(f"{{{_OFF_REL}}}id", "")
        target = targets.get(identifier)
        if target is None:
            dangling.append(f"<slide r:id={identifier or 'none'}>")
        elif target in present:
            ordered.append(target)
        else:
            absent.append(target)

    linked = len(ordered)
    if ordered:
        orphans = numeric(list(present - set(ordered)))
    else:
        ordered, orphans = numeric(list(present)), []
    # One unreferenced part cancels one dangling `r:id`: both say "a slide part the
    # relationships do not connect to the presentation", and a deck whose rels are
    # stale produces one of each *for the same slide*. Without this the diagnostic
    # fires on a document from which nothing is missing, and a diagnostic that fires
    # on healthy input is one a caller learns to ignore.
    unclaimed = len(ordered) + len(orphans) - linked
    report.parts_missing.extend(absent)
    report.parts_missing.extend(dangling[unclaimed:])
    return ordered, orphans


def _notes_for(archive: zipfile.ZipFile, slide: str) -> str | None:
    """The notes part a slide points at, via that slide's own relationships."""
    rels_name = posixpath.join(
        posixpath.dirname(slide), "_rels", posixpath.basename(slide) + ".rels"
    )
    try:
        raw = archive.read(rels_name)
    except KeyError:
        return None
    from xml.etree.ElementTree import fromstring

    _refuse_dtd(raw, name=archive.filename or "", part=rels_name)
    for relationship in fromstring(raw):
        if relationship.get("Type", "").endswith("/notesSlide"):
            target = relationship.get("Target", "")
            return (
                target[1:]
                if target.startswith("/")
                else posixpath.normpath(posixpath.join(posixpath.dirname(slide), target))
            )
    return None


def _iter_shape_text(
    archive: zipfile.ZipFile, part: str, number: int, report: OoxmlDiagnostics
) -> Iterator[Block]:
    """Paragraph and table-row blocks from one slide (or notes) part.

    ``slide_title`` is emitted only where the slide really has a title
    placeholder (``p:ph type="title"|"ctrTitle"``). Guessing "the topmost shape is
    the title" invents structure that is not in the file, and decks built by
    generators routinely use plain text boxes.
    """
    runs: list[str] = []
    is_title = False
    in_table = 0
    row_index = -1
    cell_texts: list[str] = []
    pending_cells = False
    #: Paragraphs of the `a:tc` currently open. A slide table's fields are *cells*, not
    #: paragraphs: keying on `a:p` made a two-paragraph cell into two columns and an
    #: empty cell into none, so every later field sat under the wrong column name.
    cell_parts: list[str] = []
    in_cell = False
    #: See `_MC_ALTERNATE`. PowerPoint wraps every shape, chart and SmartArt in one.
    alternates: list[bool] = []
    fallbacks: list[bool] = []
    skip = 0
    #: See `_MAX_DEPTH` and `_MAX_OPEN_ELEMENTS`.
    depth = 0
    open_elements = 0
    #: See the note in `iter_docx_blocks`; a global here costs a dict lookup per element.
    max_depth = _MAX_DEPTH
    max_open = _MAX_OPEN_ELEMENTS

    with archive.open(part) as stream:
        guarded = _dtd_free_stream(stream, name=archive.filename or "", part=part)
        for event, element in iterparse(guarded, ("start", "end")):
            tag = element.tag
            if event == "start":
                depth += 1
                open_elements += 1
                if depth > max_depth or open_elements > max_open:
                    _refuse_shape(depth, open_elements, name=archive.filename or "", part=part)
                if tag not in _PPTX_START_TAGS:
                    continue
                if tag in _SLD_TAGS:
                    if element.get("show", "").strip() in {"0", "false"}:
                        report.slides_hidden.append(number)
                elif tag == _A_TBL:
                    in_table += 1
                    row_index = -1
                elif tag == _A_TR and in_table:
                    row_index += 1
                    cell_texts = []
                    pending_cells = True
                elif tag == _A_TC and in_table:
                    cell_parts = []
                    in_cell = True
                elif tag in _SP_TAGS:
                    is_title = False
                elif tag == _MC_ALTERNATE:
                    alternates.append(False)
                elif tag == _MC_CHOICE:
                    if alternates:
                        alternates[-1] = True
                elif tag == _MC_FALLBACK:
                    if alternates and alternates[-1]:
                        skip += 1
                        fallbacks.append(True)
                    else:
                        fallbacks.append(False)
                continue

            # --- end events ---
            depth -= 1
            if tag == _MC_FALLBACK:
                if fallbacks and fallbacks.pop():
                    skip = max(0, skip - 1)
                continue
            if tag == _MC_ALTERNATE:
                if alternates:
                    alternates.pop()
                continue
            if skip:
                continue

            if tag in _A_TEXT_TAGS:
                if element.text:
                    runs.append(element.text)
            elif tag == _M_OMATH:
                report.equations += 1
            elif tag == _A_BR:
                runs.append("\n")
            elif tag in _PH_TAGS:
                is_title = element.get("type") in {"title", "ctrTitle"}
            elif tag == _A_TC:
                # One field per cell, empties included: a blank column is what keeps
                # every later value under its own header.
                if in_cell:
                    cell_texts.append(" ".join(part for part in cell_parts if part))
                    cell_parts = []
                    in_cell = False
                element.clear()
                open_elements = 0
            elif tag == _A_P:
                text = " ".join("".join(runs).split())
                runs.clear()
                if in_table and pending_cells:
                    if in_cell:
                        cell_parts.append(text)
                    elif text:
                        cell_texts.append(text)
                elif text:
                    if is_title:
                        yield Block("slide_title", text, 1, number)
                    else:
                        report.paragraphs += 1
                        yield Block("paragraph", text, 0, number)
                element.clear()
                open_elements = 0
            elif tag == _A_TR:
                while cell_texts and not cell_texts[-1]:
                    cell_texts.pop()
                if any(cell_texts):
                    report.table_rows += 1
                    yield Block("table_row", " | ".join(cell_texts), 0, number, row_index)
                cell_texts = []
                cell_parts = []
                in_cell = False
                pending_cells = False
                element.clear()
                open_elements = 0
            elif tag == _A_TBL:
                in_table = max(0, in_table - 1)
                element.clear()
                open_elements = 0


def iter_pptx_blocks(
    path: str | Path,
    *,
    diagnostics: OoxmlDiagnostics | None = None,
    include_notes: bool = True,
) -> Iterator[Block]:
    """Stream a deck as blocks, one part per slide, in presentation order.

    Speaker notes are included by default and tagged ``note``: on a real deck they
    carry the narration that the slide only gestures at, and an inherited, unverified
    finding measured a 40-slide deck where text-only extraction left more than half
    the content unretrievable. Slides that yield **no text at all** are recorded in
    ``diagnostics.slides_without_text`` -- that is the picture-only slide, the
    single most common silent loss in a deck, and reporting it is what lets a
    caller decide whether OCR is worth it.
    """
    report = diagnostics if diagnostics is not None else OoxmlDiagnostics()
    with zipfile.ZipFile(path) as archive:
        count_media(archive, report)

        ordered, orphans = _slide_order(archive, report)
        for number, name in enumerate([*ordered, *orphans], start=1):
            report.slides += 1
            produced = 0
            for block in _iter_shape_text(archive, name, number, report):
                produced += 1
                yield block
            if include_notes:
                notes_part = _notes_for(archive, name)
                if notes_part:
                    emitted = False
                    for block in _iter_shape_text(archive, notes_part, number, report):
                        # A notes part contains the slide-number placeholder too;
                        # a bare number is not narration.
                        if block.text.strip().isdigit():
                            continue
                        emitted = True
                        produced += 1
                        yield Block("note", block.text, 0, number)
                    if emitted:
                        report.notes_slides += 1
            if not produced:
                report.slides_without_text.append(number)
