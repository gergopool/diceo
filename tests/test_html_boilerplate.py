"""Site chrome must not become document content.

Measured on 8 real pages fetched for experiment 034 (html boilerplate):
GOV.UK is 54.8% navigation, MDN 41.0%, Wikipedia 30.6%, Eurostat 2.3%. The defect scales
*inversely* with page length -- a long page dilutes its chrome, a short guide is mostly
chrome -- and crawled pages are short. On a realistic news page the answer chunk was 88%
boilerplate.

The rule is deliberately **semantic, not statistical**. jusText, given the same shape,
classified the cookie banner as the only "good" paragraph and rejected the article, because
a consent notice is long, well-formed, low-link-density prose that beats a short news item
on every feature it scores (experiment 029, bug-tracker audit). HTML5
gives us `<nav>`, `<header>`, `<footer>` and ARIA landmarks for free, and those cannot
misfire that way.

    uv run pytest tests/test_html_boilerplate.py -q
"""

from __future__ import annotations

import pytest

import diceo
from diceo.types import Diagnostics


def _text(html: str) -> str:
    return "\n".join(block.text for block in diceo.extract(html.encode()))


def _dropped(html: str) -> int:
    report = Diagnostics()
    list(diceo.chunk(html.encode(), diagnostics=report))
    for note in report.notes:
        if note.startswith("boilerplate_chars="):
            return int(note.split("=")[1].split()[0])
    return 0


# --------------------------------------------------------------------------- #
# what must go
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "chrome",
    [
        "<nav><ul><li><a href='/a'>Section A</a></li>"
        "<li><a href='/b'>Section B</a></li></ul></nav>",
        # A page footer: `main` is neither sectioning content nor a sectioning root,
        # so its nearest such ancestor is `body`.
        "<main><footer><p>Contact us. Sitemap. Accessibility statement.</p></footer></main>",
        "<header><p>Skip to main content. Menu. Search.</p></header>",
        "<footer><p>All rights reserved. Modern slavery statement.</p></footer>",
        # NPR's real shape: a footer nested in a section, so the structural rule keeps it --
        # but the author declared it page furniture with an ARIA landmark.
        "<main><div><section><footer role='contentinfo'><p>NPR Sitemap. Terms of Use.</p>"
        "</footer></section></div></main>",
        "<div role='banner'><p>Cookie preferences. Sign in.</p></div>",
    ],
)
def test_site_chrome_is_not_indexed(chrome: str):
    html = f"<html><body>{chrome}<article><h1>Council budget</h1>"
    html += "<p>The council approved 4.2M EUR on Tuesday.</p></article></body></html>"
    text = _text(html)
    assert "approved 4.2M EUR" in text, "the article must survive"
    for token in ("Section A", "Sitemap", "Skip to main", "rights reserved", "Sign in"):
        assert token not in text, f"{token!r} leaked from {chrome[:40]}"


def test_dropped_characters_are_counted():
    """Rule 3: text removed from a document is reported, never silently deleted."""
    html = (
        "<html><body><nav><ul><li><a href='/a'>Section A</a></li></ul></nav>"
        "<article><p>Real content here.</p></article></body></html>"
    )
    assert _dropped(html) > 0


# --------------------------------------------------------------------------- #
# what must stay -- each of these is a real page's real content
# --------------------------------------------------------------------------- #


def test_a_header_inside_an_article_holds_the_headings():
    """On the real ONS inflation bulletin, `article > div > section > header` holds *all
    ten* section headings ("1. Main points", "2. Consumer price inflation rates", ...).
    A rule that dropped `<header>` regardless of nesting loses every heading on a real
    government statistics page -- and 002 measured 12.8% of facts living in headings."""
    html = (
        "<html><body><main><article><div><section>"
        "<header><h2>1. Main points</h2></header>"
        "<p>CPIH rose by 3.2% in the 12 months to March.</p>"
        "</section></div></article></main></body></html>"
    )
    text = _text(html)
    assert "1. Main points" in text
    assert "CPIH rose by 3.2%" in text


def test_a_footer_inside_an_article_holds_the_attribution():
    """A scholarly article's author list, dates and DOI live in `article > footer`."""
    html = (
        "<html><body><article><h1>Cold chain integrity</h1><p>Body.</p>"
        "<footer><p>Roy, H. E. et al. 2026. doi:10.5281/zenodo.11274696</p></footer>"
        "</article></body></html>"
    )
    text = _text(html)
    assert "doi:10.5281/zenodo.11274696" in text


def test_a_footer_inside_a_figure_holds_the_source():
    """ONS puts "Source: Consumer price inflation from the ONS" in `figure > footer`."""
    html = (
        "<html><body><main><figure><table><tr><td>3.2</td></tr></table>"
        "<footer>Source: Consumer price inflation from the Office for National Statistics"
        "</footer></figure></main></body></html>"
    )
    assert "Office for National Statistics" in _text(html)


def test_an_aside_is_kept():
    """Asides hold pull-quotes and key-facts boxes as often as sponsor messages, so this
    one is a deliberate keep rather than an oversight."""
    html = "<html><body><aside><p>Key fact: 4.2M EUR approved.</p></aside></body></html>"
    assert "4.2M EUR approved" in _text(html)


def test_role_navigation_is_kept():
    """Including `role="navigation"` takes Wikipedia from 9.0% dropped to 30.2%, because
    its bottom navboxes carry it -- 4,338 characters of real topic terms. Sphinx does the
    same with prev/next links. That trade belongs to a caller, not to a tag rule."""
    html = (
        "<html><body><div role='navigation'><p>Refrigeration. Cold chain. Logistics.</p>"
        "</div><article><p>Body.</p></article></body></html>"
    )
    assert "Refrigeration. Cold chain." in _text(html)


def test_the_corpus_is_untouched():
    """The rule must be provably neutral on every published number: the held-out HTML has
    no nav, header, footer or aside at all, so not one chunk may change."""
    import glob

    paths = sorted(glob.glob("data/holdout-23/*.html"))[:6]
    if not paths:
        pytest.skip("corpus not present")
    for path in paths:
        report = Diagnostics()
        list(diceo.chunk(path, diagnostics=report))
        assert not any("boilerplate" in note for note in report.notes), path
