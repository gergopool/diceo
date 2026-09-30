"""Text files that get mistaken for mail, and mail that loses content silently.

Written after surveying CPython's ``email`` package, mail-parser, extract_msg,
flanker, mailparse and Apache Tika (experiment 029, bug-tracker audit).

The structural problem is that **an .eml file has no magic number**, and Python's
parser never fails: it treats any leading ``Word:`` lines as headers and the rest as
the body. When *every* leading line looks like a header, the body is empty and the
document silently enters the index with only its "headers" in it. Tika has fought
this across eight issues; two of its fix titles read "again" and "yet again", because
tightening a header-name list is tunable rather than solvable.

    uv run pytest tests/test_email_edge_cases.py -q
"""

from __future__ import annotations

import io
import re
from email.message import EmailMessage, Message

import pytest

import diceo
from diceo.types import Diagnostics


def _read(raw: bytes) -> tuple[str, Diagnostics]:
    report = Diagnostics()
    chunks = list(diceo.chunk(io.BytesIO(raw), diagnostics=report))
    return "\n".join(chunk.text for chunk in chunks), report


# --------------------------------------------------------------------------- #
# text files that are not mail
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("label", "raw", "must_contain"),
    [
        (
            # The case that found this: every line is a known header name, so the
            # whole file parses as headers with an empty body -- and because
            # duplicate headers collapse to the first, everything after line 1 is
            # silently gone. 30 of 59 bytes, no exception, no diagnostic.
            "a log file of Date: lines",
            b"Date: 2026-01-01 ERROR upload failed\nDate: 2026-01-02 ERROR retry failed\n",
            [b"upload failed".decode(), b"retry failed".decode()],
        ),
        (
            "a log file of Received: lines",
            b"Received: 2026-01-01 packet A\nReceived: 2026-01-02 packet B\n",
            ["packet A", "packet B"],
        ),
        (
            "a properties file",
            b"name:widget\nfrom:warehouse\nto:customer\n",
            ["widget", "warehouse", "customer"],
        ),
        (
            "a CSV whose header row looks like mail headers",
            b"From:,To:,Date:\nalice,bob,2026-01-01\n",
            ["alice", "bob"],
        ),
    ],
)
def test_a_text_file_is_not_mail_and_keeps_every_line(
    label: str, raw: bytes, must_contain: list[str]
):
    """Whatever we decide these files *are*, no line may vanish."""
    text, _ = _read(raw)
    missing = [needle for needle in must_contain if needle not in text]
    assert not missing, f"{label}: lost {missing!r} -- got {text!r}"


def test_a_real_email_is_still_detected(tmp_path):
    """The guard on the fix: tightening detection must not stop finding real mail."""
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "bob@example.com"
    message["Subject"] = "Q3 contract"
    message["Date"] = "Mon, 05 Jan 2026 09:00:00 +0000"
    message.set_content("The penalty clause was removed on 2026-01-04.")
    text, report = _read(message.as_bytes())
    assert report.format == "eml"
    assert "Q3 contract" in text
    assert "penalty clause was removed" in text


@pytest.mark.parametrize("cte", ["quoted-printable", "base64"])
def test_ignored_attachment_sizes_are_estimates_without_decoding(cte, monkeypatch):
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["To"] = "bob@example.com"
    message["Subject"] = "Attachment budget"
    message.set_content("Authored body remains available.")
    payload = b"=hello\xff!\r\n"
    message.add_attachment(
        payload, maintype="application", subtype="octet-stream", filename="payload.bin", cte=cte
    )
    raw = message.as_bytes()
    get_payload = Message.get_payload

    def forbid_attachment_decode(self, i=None, decode=False):
        if decode and self.get_filename():
            raise AssertionError("ignored attachments must not be decoded for sizing")
        return get_payload(self, i=i, decode=decode)

    monkeypatch.setattr(Message, "get_payload", forbid_attachment_decode)
    text, report = _read(raw)
    assert "Authored body remains available." in text and report.lost_data
    diagnostic = next(line for line in report.truncated if "attachment=payload.bin" in line)
    match = re.search(r"(\d+) bytes estimated from encoded payload", diagnostic)
    assert match and int(match.group(1)) > len(payload)


@pytest.mark.parametrize(
    "raw",
    [
        b"Received: from mx.example.com by mail.example.net\nSubject: hi\n\nbody\n",
        b"Message-ID: <abc@example.com>\nFrom: a@b.c\n\nbody\n",
        b"MIME-Version: 1.0\nFrom: a@b.c\nSubject: s\n\nbody\n",
        b"From: a@b.c\nDate: Mon, 05 Jan 2026 09:00:00 +0000\n\nbody\n",
    ],
)
def test_mail_shapes_that_must_keep_being_detected(raw: bytes):
    _, report = _read(raw)
    assert report.format == "eml"


# --------------------------------------------------------------------------- #
# mail that really is mail, losing content
# --------------------------------------------------------------------------- #


def test_duplicate_headers_do_not_silently_drop_one(tmp_path):
    """RFC 5322 permits at most one Subject, but duplicates occur. ``msg['Subject']``
    returns the first and reports nothing, so if the real subject is the second
    occurrence it is silently lost. ``get_all`` is the fix."""
    raw = (
        b"From: a@b.c\n"
        b"Subject: FIRST subject\n"
        b"Subject: SECOND subject\n"
        b"Date: Mon, 05 Jan 2026 09:00:00 +0000\n"
        b"\n"
        b"body text\n"
    )
    text, report = _read(raw)
    assert report.format == "eml"
    assert "FIRST subject" in text
    assert "SECOND subject" in text, f"lost the duplicate -- got {text!r}"
