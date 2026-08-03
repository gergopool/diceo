"""Four ways a MIME message loses its content between ``get_body()`` and the index.

Found by the experiment 038 (edge case mining) audit and each one
reproduced here before the fix, because ``get_body()`` and ``iter_attachments()`` are
not a partition of a message: a part can be neither, and then nobody sees it.

1. ``multipart/alternative``. ``get_body(preferencelist=("plain", "html"))`` returns
   the first *available* preference, not the fullest one, so a "View this email in
   your browser." stub beat the whole html body and the html sibling is invisible to
   ``iter_attachments()`` too. Measured before the fix, the entire newsletter was
   gone with `notes: []`, `truncated: []`, `lost_data: False`::

       'Q3 results\\nFrom: ...\\nView this email in your browser.'

2. Leaf parts that are neither. A meeting invite's ``text/calendar`` alternative and
   an image in a nested ``multipart/related`` branch both vanished the same way --
   `'Invitation: budget review\\nFrom: ...\\nPlain body of the invite.'`, no note.

3. ``message/rfc822``. ``get_payload(decode=True)`` is ``None`` when the payload is a
   list, so every forwarded mail was reported as
   `"attachment=(unnamed) (message/rfc822, 0 bytes)"` -- a false size and a name the
   documented recovery loop cannot use, with the forwarded body indexed nowhere.

4. The ``email_sniff_rejected`` fallback. It exists for a log of ``Date:`` lines, and
   it fired on real mail whose body ``get_body()`` could not reach, re-reading the
   raw bytes as plain text. What landed in the index was the MIME structure::

       'From: billing@example.com\\n...\\nContent-Transfer-Encoding: base64\\n'
       'JVBERi0xLjQgZmFrZSBieXRlcyBBQUFB...'

   with `format: text` and `lost_data: False`, while the statement never arrived.

    uv run pytest tests/test_eml_multipart.py -q
"""

from __future__ import annotations

import io
from email.message import EmailMessage

import diceo
from diceo.types import Diagnostics

#: A body worth keeping: three facts, none of them in the plain-text stub.
_HTML = (
    "<html><body><h1>Quarterly results</h1>"
    "<p>Revenue rose 12% to EUR 4.1M in the third quarter.</p>"
    "<p>The board approved the Helsinki lease on 14 November.</p>"
    "</body></html>"
)

#: A real invite's calendar part: the *when* and *where* live only here.
_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\n"
    "UID:abc123\r\nSEQUENCE:0\r\nTRANSP:OPAQUE\r\n"
    "SUMMARY:Budget review with the works council\r\n"
    "DTSTART:20260210T090000Z\r\nLOCATION:Room 4.12\\, Utrecht\r\n"
    "DESCRIPTION:Bring the Q3 depreciation schedule.\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)


def _read(raw: bytes) -> tuple[str, Diagnostics]:
    report = Diagnostics()
    chunks = list(diceo.chunk(io.BytesIO(raw), diagnostics=report))
    return "\n".join(chunk.text for chunk in chunks), report


def _envelope(subject: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "bob@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 05 Jan 2026 09:00:00 +0000"
    return message


def _stub_plus_html(stub: str = "View this email in your browser.") -> bytes:
    message = _envelope("Q3 results")
    message.set_content(stub)
    message.add_alternative(_HTML, subtype="html")
    return message.as_bytes()


def _forwarding(depth: int) -> bytes:
    """A chain of ``depth`` nested ``message/rfc822`` parts, innermost first."""
    message = _envelope("Original: invoice 4471")
    message.set_content("The invoice total is EUR 12,400 excluding VAT.")
    for level in range(depth):
        outer = _envelope(f"Fwd {level + 1}: invoice 4471")
        outer.set_content(f"Forwarding this on, round {level + 1}.")
        outer.add_attachment(message)
        message = outer
    return message.as_bytes()


# --------------------------------------------------------------------------- #
# 1. multipart/alternative: the fuller part wins
# --------------------------------------------------------------------------- #


def test_the_html_alternative_is_indexed_when_the_plain_part_is_a_stub():
    text, _ = _read(_stub_plus_html())

    assert "Revenue rose 12%" in text, text
    assert "Helsinki lease" in text, text


def test_the_html_alternative_keeps_its_heading_structure():
    """Rendered through the html reader, not pasted in as markup."""
    blocks = list(diceo.extract(_stub_plus_html(), name="m.eml"))

    headings = [block.text for block in blocks if block.is_heading]
    assert "Quarterly results" in headings, headings
    assert "<p>" not in "\n".join(block.text for block in blocks)


def test_the_discarded_alternative_is_counted():
    _, report = _read(_stub_plus_html())

    assert any(note.startswith("alternative_discarded=") for note in report.notes), report.notes


def test_a_plain_body_that_carries_the_text_still_wins():
    """The guard on the fix: the plain part is the cheaper and better read whenever
    it says the same thing, so only a *stub* may lose."""
    message = _envelope("Q3 results")
    message.set_content(
        "Revenue rose 12% to EUR 4.1M in the third quarter. "
        "The board approved the Helsinki lease on 14 November. "
        "Full detail follows in the attached deck."
    )
    message.add_alternative("<html><body><p>Revenue rose 12%.</p></body></html>", "html")

    _, report = _read(message.as_bytes())

    assert any("kept text/plain" in note for note in report.notes), report.notes


def test_the_stub_is_not_chosen_on_byte_length():
    """Experiment 029 measured the discriminator: the stub/html *byte* ratio was
    0.67, so raw length cannot decide. Here the html part is the longer of the two
    in bytes and still holds less text than the plain part."""
    padding = "<!-- " + "x" * 4000 + " -->"
    message = _envelope("Q3 results")
    message.set_content("Revenue rose 12% to EUR 4.1M in the third quarter.")
    message.add_alternative(f"<html><body>{padding}<p>Revenue.</p></body></html>", "html")

    text, _ = _read(message.as_bytes())

    assert "EUR 4.1M" in text, text


# --------------------------------------------------------------------------- #
# 2. leaves that are neither the body nor an attachment
# --------------------------------------------------------------------------- #


def test_a_calendar_alternative_reaches_the_index():
    message = _envelope("Invitation: budget review")
    message.set_content("Plain body of the invite.")
    message.add_alternative(_ICS, subtype="calendar")

    text, _ = _read(message.as_bytes())

    assert "Budget review with the works council" in text, text
    assert "Room 4.12, Utrecht" in text, text


def test_the_calendar_bookkeeping_is_left_out():
    """SUMMARY and LOCATION are content; UID, SEQUENCE and TRANSP are machine state
    that costs tokens and matches nothing."""
    message = _envelope("Invitation: budget review")
    message.set_content("Plain body of the invite.")
    message.add_alternative(_ICS, subtype="calendar")

    text, _ = _read(message.as_bytes())

    for noise in ("BEGIN:VCALENDAR", "UID:abc123", "TRANSP", "SEQUENCE"):
        assert noise not in text, text


def test_an_image_in_a_nested_related_branch_is_reported():
    message = _envelope("Newsletter")
    message.set_content("Plain body.")
    message.add_alternative(_HTML, subtype="html")
    message.get_payload()[1].add_related(
        b"\x89PNG\r\n\x1a\n" + b"0" * 200, maintype="image", subtype="png", cid="logo"
    )

    _, report = _read(message.as_bytes())

    assert any("image/png" in item for item in report.truncated), report.truncated
    assert report.lost_data


def test_an_ordinary_mail_reports_no_unreferenced_parts():
    """The guard: the reconciliation pass must not fire on every message. A body and
    an attachment account for a two-part mail completely."""
    message = _envelope("Budget approval")
    message.set_content("Please approve the budget of 1200 EUR.")
    message.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="b.pdf")

    _, report = _read(message.as_bytes())

    assert not any("unreferenced_part" in item for item in report.truncated), report.truncated


def test_a_second_html_alternative_is_not_indexed_twice():
    """Both html parts say the same thing, and a duplicated body doubles the tokens
    of every chunk it lands in."""
    message = _envelope("Newsletter")
    message.set_content("Plain stub.")
    message.add_alternative(_HTML, subtype="html")
    message.add_alternative(_HTML, subtype="html")

    text, _ = _read(message.as_bytes())

    assert text.count("Helsinki lease") == 1, text


# --------------------------------------------------------------------------- #
# 3. message/rfc822 is a document, not a file
# --------------------------------------------------------------------------- #


def test_a_forwarded_message_is_read_rather_than_reported_as_an_attachment():
    text, _ = _read(_forwarding(1))

    assert "EUR 12,400 excluding VAT" in text, text


def test_a_forwarded_message_keeps_its_own_subject_and_envelope():
    text, _ = _read(_forwarding(1))

    assert "Original: invoice 4471" in text, text
    assert "Fwd 1: invoice 4471" in text, text


def test_a_forwarded_message_is_never_reported_as_zero_bytes():
    """The observed line was `attachment=(unnamed) (message/rfc822, 0 bytes)`: the
    size is false, and `(unnamed)` is not something the documented recovery loop can
    ask for."""
    _, report = _read(_forwarding(1))

    assert not any("0 bytes" in item for item in report.truncated), report.truncated
    assert not any("(unnamed)" in item for item in report.truncated), report.truncated


def test_a_forwarded_message_is_noted_with_its_real_size():
    _, report = _read(_forwarding(1))

    noted = [note for note in report.notes if note.startswith("forwarded_message=")]
    assert noted, report.notes
    assert " 0 bytes" not in noted[0], noted


def test_a_bare_rfc822_wrapper_indexes_its_message_once():
    """A `.eml` whose whole content type is `message/rfc822`. `walk()` descends into
    the message such a part carries, so a reconciliation pass built on it read the
    forwarded body a second time as an unreferenced `text/plain` leaf:

        '... EUR 12,400 excluding VAT.\\nThe invoice total is EUR 12,400 excluding VAT.'
    """
    inner = _envelope("Original: invoice 4471")
    inner.set_content("The invoice total is EUR 12,400 excluding VAT.")
    wrapper = _envelope("Fwd wrapper")
    wrapper.set_content(inner)

    text, _ = _read(wrapper.as_bytes())

    assert text.count("EUR 12,400") == 1, text


def test_forwarding_deeper_than_the_limit_is_reported_not_read():
    """Unbounded recursion on attacker-shaped mail is the other half of rule 3: the
    remainder is refused *and* counted, never dropped. Five nested forwards, three
    read (`Fwd 5` down to `Fwd 2`), the rest named in diagnostics."""
    text, report = _read(_forwarding(5))

    assert "Fwd 2: invoice 4471" in text, text
    assert "Fwd 1: invoice 4471" not in text, text
    assert any("rfc822_depth_exceeded" in item for item in report.truncated), report.truncated
    assert report.lost_data


# --------------------------------------------------------------------------- #
# 4. the sniff fallback is for text files, not for mail
# --------------------------------------------------------------------------- #


def _statement() -> bytes:
    """Genuine mail whose only part is a pdf: no body for `get_body()` to find and
    nothing for `iter_attachments()` to yield, because it is not multipart."""
    message = _envelope("Your statement")
    message.set_content(
        b"%PDF-1.4 fake bytes " + b"A" * 300,
        maintype="application",
        subtype="pdf",
        filename="statement.pdf",
    )
    return message.as_bytes()


def test_a_mail_with_no_reachable_body_stays_an_email():
    _, report = _read(_statement())

    assert report.format == "eml", report.format


def test_mime_headers_and_base64_never_reach_the_index():
    text, _ = _read(_statement())

    for leaked in ("Content-Transfer-Encoding", "JVBERi0", "MIME-Version"):
        assert leaked not in text, text


def test_the_unreachable_part_is_reported_as_lost():
    text, report = _read(_statement())

    assert report.lost_data, report.as_dict()
    assert any("statement.pdf" in item for item in report.truncated), report.truncated
    assert "Your statement" in text, "the subject is still worth indexing"


def test_an_html_only_alternative_is_read_rather_than_re_read_as_text():
    """The same fallback fired on a newsletter with an empty plain stub, indexing
    `--===============0347563079348309381==` and quoted-printable soft breaks."""
    message = _envelope("HTML only newsletter")
    message.set_content("")
    message.add_alternative(_HTML, subtype="html")

    text, report = _read(message.as_bytes())

    assert report.format == "eml", report.format
    assert "Helsinki lease" in text, text
    assert "boundary" not in text, text


def test_a_log_of_header_lines_is_still_re_read_as_text():
    """The guard on the gate: the fallback exists because an .eml has no magic
    number, and this file is not mail -- no MIME-Version, no boundary, one part."""
    raw = b"Date: 2026-01-01 ERROR upload failed\nDate: 2026-01-02 ERROR retry failed\n"

    text, report = _read(raw)

    assert report.format == "text", report.format
    assert "retry failed" in text, text
