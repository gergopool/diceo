"""A slide the presentation names and the package does not hold.

Rule 3's worst case, live in the package until this file existed. ``p:sldIdLst`` names
the slides; ``_slide_order`` resolved each ``r:id`` to a part name and kept the ones the
archive actually held. Everything else fell off the end of the loop. So a deck whose
slide parts had been stripped -- a truncated download, a DLP filter, a repackaging
script that dropped members -- produced::

    chunks    : 0
    error     : (none)
    lost_data : False
    notes     : []

Nothing. Not a count, not a note, not an exception. The document simply was not in the
caller's index and no observable anywhere said so, which is rule 3's stated worst
failure in this domain.

``parts_missing`` was *already* counted for docx and *already* forwarded to
``report.notes`` -- and a note does not flip ``lost_data``, which is what the docstring
of ``diceo.chunk`` documents as the handler::

    if report.lost_data:
        log.warning("incomplete: %s", report.as_dict())

A diagnostic the documented handler cannot see is decoration. It goes through
``truncate`` now.

The rest of this file widens the same question to the neighbouring damage -- a slide part
that is present but truncated, one that is not well-formed, one whose deflate stream is
broken, and an ``r:id`` pointing at a relationship that does not exist. Each has to reach
the caller as chunks-plus-a-counted-loss or as a ``DiceoError``; a clean empty result is
the one answer that is never allowed.

Two of the tests here are controls, and they matter as much as the rest: a diagnostic
that fires on an ordinary deck is one a caller learns to ignore inside a day.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

import diceo
from diceo.ooxml import OoxmlDiagnostics, iter_pptx_blocks

_A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/><Default Extension="xml" ContentType="application/xml"/></Types>'
)


def _slide(text: str) -> str:
    return (
        f'<?xml version="1.0"?><p:sld {_A} {_P}><p:cSld><p:spTree>'
        f"<p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>"
        f"</p:spTree></p:cSld></p:sld>"
    )


def _pptx(
    slides: dict[int, str | None],
    *,
    rels: dict[int, str] | None = None,
    extra: dict[str, str] | None = None,
) -> bytes:
    """A deck whose ``p:sldIdLst`` names every key of ``slides``.

    A ``None`` payload is the whole point: the slide is named in the presentation and
    in the relationships, and its part is **not written into the archive**. ``rels``
    overrides the ``r:id`` a slide is referenced by, so a dangling reference can be
    built without touching anything else.
    """
    numbers = sorted(slides)
    rels = rels or {number: f"rId{number}" for number in numbers}
    ids = "".join(f'<p:sldId id="{255 + n}" r:id="{rels[n]}"/>' for n in numbers)
    parts: dict[str, str] = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": f'<?xml version="1.0"?><Relationships {_RELS_NS}>'
        f'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/officeDocument" '
        f'Target="ppt/presentation.xml"/></Relationships>',
        "ppt/presentation.xml": f'<?xml version="1.0"?><p:presentation {_P} {_R}>'
        f"<p:sldIdLst>{ids}</p:sldIdLst></p:presentation>",
        "ppt/_rels/presentation.xml.rels": f'<?xml version="1.0"?><Relationships {_RELS_NS}>'
        + "".join(
            f'<Relationship Id="rId{n}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/slide" Target="slides/slide{n}.xml"/>'
            for n in numbers
        )
        + "</Relationships>",
    }
    for number, payload in slides.items():
        if payload is not None:
            parts[f"ppt/slides/slide{number}.xml"] = payload
    parts.update(extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _break_deflate(payload: bytes, member: str) -> bytes:
    """Overwrite the start of one member's compressed data, leaving the rest intact.

    The local header is 30 fixed bytes plus the name and the extra field, so the offset
    is computable from the archive itself rather than guessed. Corrupting the bytes in
    place keeps every other member readable, which is the point -- a whole-file
    truncation would test the container, and the question here is what one unreadable
    *part* does to a deck.
    """
    raw = bytearray(payload)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        start = archive.getinfo(member).header_offset
    name_length = int.from_bytes(raw[start + 26 : start + 28], "little")
    extra_length = int.from_bytes(raw[start + 28 : start + 30], "little")
    data = start + 30 + name_length + extra_length
    raw[data : data + 8] = b"\xff" * 8
    return bytes(raw)


def _run(payload: bytes, tmp_path: Path) -> tuple[list, diceo.Diagnostics]:
    path = tmp_path / "deck.pptx"
    path.write_bytes(payload)
    report = diceo.Diagnostics()
    return list(diceo.chunk(path, diagnostics=report)), report


def _truncated(report: diceo.Diagnostics) -> str:
    return " || ".join(report.truncated)


def _notes(report: diceo.Diagnostics) -> str:
    return " || ".join(report.notes)


# --------------------------------------------------------------------------- #
# The defect: named in the relationships, absent from the archive
# --------------------------------------------------------------------------- #


def test_every_slide_part_absent_is_not_a_clean_empty_result(tmp_path):
    """The exact failure this file exists for: zero chunks and nothing said."""
    chunks, report = _run(_pptx({1: None, 2: None}), tmp_path)

    assert chunks == []
    assert report.lost_data, report.as_dict()
    assert "parts_missing=2" in _truncated(report), report.truncated


def test_the_missing_parts_are_named_not_just_counted(tmp_path):
    """ "Which slide is not in my index" is the only useful next question."""
    _, report = _run(_pptx({1: None, 2: None}), tmp_path)

    assert "ppt/slides/slide1.xml" in _truncated(report), report.truncated
    assert "ppt/slides/slide2.xml" in _truncated(report), report.truncated


def test_a_missing_part_reaches_truncated_and_not_only_notes(tmp_path):
    """The regression guard. ``parts_missing`` was a note, and a note is invisible to
    ``if report.lost_data:`` -- the handler ``diceo.chunk``'s docstring documents."""
    _, report = _run(_pptx({1: None}), tmp_path)

    assert any("parts_missing" in entry for entry in report.truncated), report.as_dict()
    assert report.as_dict()["lost_data"] is True


def test_one_missing_slide_among_healthy_ones_still_yields_the_others(tmp_path):
    """Half a deck plus a counted loss, never half a deck reported as whole."""
    chunks, report = _run(_pptx({1: _slide("Alpha"), 2: None, 3: _slide("Gamma")}), tmp_path)

    assert "Alpha" in chunks[0].text and "Gamma" in chunks[0].text
    assert report.lost_data, report.as_dict()
    assert "ppt/slides/slide2.xml" in _truncated(report), report.truncated


def test_a_dangling_r_id_with_nothing_to_stand_in_for_it_is_a_loss(tmp_path):
    """``p:sldId`` naming an ``r:id`` that has no relationship at all.

    There is no target name to check against the archive, so the slide cannot be
    located -- but the presentation says it exists, and nothing in the package is it.
    """
    payload = _pptx({1: _slide("Alpha"), 2: None}, rels={1: "rId1", 2: "rId404"})
    chunks, report = _run(payload, tmp_path)

    assert "Alpha" in chunks[0].text
    assert report.lost_data, report.as_dict()
    assert "rId404" in _truncated(report), report.truncated


# --------------------------------------------------------------------------- #
# Present but unreadable: each one is an error or a counted loss, never silence
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("empty", ""),
        ("truncated", _slide("Alpha")[:120]),
        ("not well formed", '<?xml version="1.0"?><p:sld><unclosed></p:sld>'),
    ],
)
def test_the_only_slide_being_unreadable_raises_rather_than_returning_nothing(
    label, body, tmp_path
):
    """Zero chunks and no exception is the answer that is never allowed."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(_pptx({1: body}))

    with pytest.raises(diceo.DiceoError):
        list(diceo.chunk(path))


def test_a_broken_deflate_stream_raises_rather_than_returning_nothing(tmp_path):
    payload = _break_deflate(_pptx({1: _slide("Alpha")}), "ppt/slides/slide1.xml")
    path = tmp_path / "deck.pptx"
    path.write_bytes(payload)

    with pytest.raises(diceo.DiceoError):
        list(diceo.chunk(path))


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("empty", ""),
        ("truncated", _slide("Beta")[:120]),
        ("not well formed", '<?xml version="1.0"?><p:sld><unclosed></p:sld>'),
    ],
)
def test_a_later_slide_being_unreadable_keeps_the_earlier_ones_and_counts_the_loss(
    label, body, tmp_path
):
    chunks, report = _run(_pptx({1: _slide("Alpha"), 2: body}), tmp_path)

    assert "Alpha" in chunks[0].text
    assert report.lost_data, report.as_dict()
    assert "reader_failed_after" in _truncated(report), report.truncated


# --------------------------------------------------------------------------- #
# Charts and SmartArt: counted, said out loud, not extracted
# --------------------------------------------------------------------------- #

_CHART_PARTS = {
    "ppt/charts/chart1.xml": '<?xml version="1.0"?><chartSpace/>',
    "ppt/charts/chart2.xml": '<?xml version="1.0"?><chartSpace/>',
    "ppt/diagrams/data1.xml": '<?xml version="1.0"?><dataModel/>',
}


def test_a_deck_whose_numbers_live_in_a_chart_says_so(tmp_path):
    """A chart is not a raster part, so ``media_parts`` never saw one. The slide
    carrying it indexed as a title and nothing else, with no observable at all."""
    chunks, report = _run(_pptx({1: _slide("Q3 revenue")}, extra=_CHART_PARTS), tmp_path)

    assert "Q3 revenue" in chunks[0].text
    assert "charts=2" in _notes(report), report.notes
    assert "smartart=1" in _notes(report), report.notes


def test_counting_a_chart_is_not_reported_as_lost_data(tmp_path):
    """A note, not a truncation. A chart is usually a rendering of numbers that are
    also in a table on the same slide, and flagging every deck with a chart as
    incomplete would make ``lost_data`` mean nothing."""
    _, report = _run(_pptx({1: _slide("Q3 revenue")}, extra=_CHART_PARTS), tmp_path)

    assert report.truncated == []
    assert report.lost_data is False


def test_the_chart_count_is_reached_through_the_reader_diagnostics(tmp_path):
    path = tmp_path / "deck.pptx"
    path.write_bytes(_pptx({1: _slide("Q3 revenue")}, extra=_CHART_PARTS))
    stats = OoxmlDiagnostics()

    list(iter_pptx_blocks(path, diagnostics=stats))

    assert (stats.charts, stats.smartart) == (2, 1)
    assert stats.media_parts == 0


# --------------------------------------------------------------------------- #
# Controls -- a diagnostic that fires on healthy input is one nobody reads
# --------------------------------------------------------------------------- #


def test_a_healthy_deck_gains_nothing(tmp_path):
    chunks, report = _run(_pptx({1: _slide("Alpha"), 2: _slide("Beta")}), tmp_path)

    assert "Alpha" in chunks[0].text and "Beta" in chunks[0].text
    assert report.truncated == []
    assert report.lost_data is False
    assert "parts_missing" not in _notes(report), report.notes
    assert "charts=" not in _notes(report), report.notes


def test_a_healthy_deck_reports_no_missing_parts_to_the_reader_either(tmp_path):
    path = tmp_path / "deck.pptx"
    path.write_bytes(_pptx({1: _slide("Alpha"), 2: _slide("Beta")}))
    stats = OoxmlDiagnostics()

    blocks = list(iter_pptx_blocks(path, diagnostics=stats))

    assert [block.text for block in blocks] == ["Alpha", "Beta"]
    assert stats.parts_missing == []


def test_a_stale_relationship_over_a_slide_that_is_present_is_not_a_loss(tmp_path):
    """The false positive the cancellation in ``_slide_order`` exists to prevent.

    A dangling ``r:id`` names nothing, so it cannot be checked against the archive --
    and a deck with an unreferenced part sitting right there has one of each *for the
    same slide*. Both slides are read; reporting a missing part here would fire the
    diagnostic on a document from which nothing is absent.
    """
    payload = _pptx({1: _slide("Alpha"), 2: _slide("Beta")}, rels={1: "rId1", 2: "rId404"})
    chunks, report = _run(payload, tmp_path)

    assert "Alpha" in chunks[0].text and "Beta" in chunks[0].text
    assert report.truncated == []
    assert report.lost_data is False


def test_slide_order_still_follows_the_presentation(tmp_path):
    """Nothing about the ordering changed; ``_slide_order`` grew an out-parameter."""
    payload = _pptx({1: _slide("Alpha"), 2: _slide("Beta")}, rels={1: "rId2", 2: "rId1"})
    # rId1 -> slides/slide1.xml, rId2 -> slides/slide2.xml, so the presentation asks
    # for slide2 first.
    path = tmp_path / "deck.pptx"
    path.write_bytes(payload)
    stats = OoxmlDiagnostics()

    blocks = list(iter_pptx_blocks(path, diagnostics=stats))

    assert [block.text for block in blocks] == ["Beta", "Alpha"]
    assert stats.parts_missing == []
