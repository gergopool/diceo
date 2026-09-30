"""Compact regressions for actual NIST, HMRC, W3C and Python audit failures."""

from __future__ import annotations

import io

from diceo.plaintext import iter_html_blocks
from diceo.types import Diagnostics


def _read(source: str, chunk_bytes: int = 1 << 16):
    report = Diagnostics()
    blocks = list(iter_html_blocks(io.BytesIO(source.encode()), report, chunk_bytes))
    return blocks, report


def test_main_header_is_document_content_and_main_footer_remains_chrome():
    blocks, report = _read(
        "<body><header>Menu</header><main><header><h1>Income Tax rates</h1>"
        "<p>Published 2026</p></header><p>Basic rate 20%</p>"
        "<footer>Site links</footer></main></body>"
    )
    assert [(b.kind, b.text) for b in blocks] == [
        ("heading", "Income Tax rates"),
        ("paragraph", "Published 2026"),
        ("paragraph", "Basic rate 20%"),
    ]
    assert any(note.startswith("boilerplate_chars=") for note in report.notes)


def test_pre_preserves_executable_indentation_and_string_whitespace():
    code = 'def squares(xs):\n    for x in xs:\n        print("two  spaces", x * x)'
    blocks, _ = _read(f"<pre><code>{code}</code></pre>", chunk_bytes=11)
    assert len(blocks) == 1
    assert blocks[0].text == code
    compile(blocks[0].text, "<extracted>", "exec")
    blocks, _ = _read("<pre>    first<br>        second</pre>")
    assert blocks[0].text == "    first\n        second"


def test_hidden_states_are_removed_but_searchable_and_visible_text_stays():
    blocks, report = _read(
        "<p>Before<span hidden>Accepted<span>secret</span></span>After</p>"
        "<div hidden='hidden'><p>Rejected</p><div>Permalink</div></div>"
        "<img hidden alt='invisible caption'>"
        "<math hidden alttext='invisible formula'><mi>x</mi></math>"
        "<section hidden='UNTIL-FOUND'>Searchable</section>"
        "<p aria-hidden='true'>Visible mathematical text</p>"
        "<details><summary>Collapsed</summary>Retrievable detail</details>",
        chunk_bytes=13,
    )
    text = "\n".join(b.text for b in blocks)
    assert "BeforeAfter" in text
    assert "Searchable" in text and "Visible mathematical text" in text
    assert "Retrievable detail" in text
    assert all(
        t not in text for t in ("Accepted", "Rejected", "Permalink", "caption", "formula")
    )
    assert any(note.startswith("hidden_chars=") for note in report.notes)
    assert not report.lost_data


def test_math_alternatives_stay_inline_and_do_not_duplicate_presentation():
    blocks, report = _read(
        '<p>Let <math alttext="p(x)=x^2"><mi>p</mi><mi>x</mi></math> be a polynomial.</p>'
        "<table><tr><td>Formula</td><td><math><semantics><mi>x</mi>"
        '<annotation encoding="application/x-tex">x^{2}</annotation>'
        '<annotation encoding="text/plain">duplicate</annotation>'
        "</semantics></math></td></tr></table>"
        '<p><math aria-label="alpha plus beta"><mi>α</mi></math>.</p>',
        chunk_bytes=17,
    )
    assert [b.text for b in blocks] == [
        "Let p(x)=x^2 be a polynomial.",
        "Formula | x^{2}",
        "alpha plus beta.",
    ]
    assert not report.lost_data


def test_math_without_recoverable_alternative_reports_loss():
    blocks, report = _read(
        "<p>Before <math><msup><mi>x</mi><mn>2</mn></msup>"
        '<annotation encoding="application/json">{"id":4}</annotation></math> after.</p>'
        '<p><math alttext="  "><mi>x</mi></math></p>'
    )
    assert [b.text for b in blocks] == ["Before after."]
    assert report.lost_data
    assert any(note.startswith("math_without_text=2") for note in report.truncated)


def test_unclosed_math_recovers_authored_alternative_and_reports_structure_loss():
    blocks, report = _read('<p>Before <math alttext="x^2"><mi>x</mi>')
    assert [b.text for b in blocks] == ["Before x^2"]
    assert report.lost_data
