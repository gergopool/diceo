"""What a declared charset is worth, in html and in mail.

Two defects with one shape: a label was believed, and the bytes were not consulted.

**html.** ``charset=iso-8859-1`` is the most-declared non-utf-8 label on the web and it
is nearly always a lie -- the page is windows-1252. Python's ``iso-8859-1`` codec cannot
raise, so the declared label always won the candidate race in ``_decode_text`` and the
curly quotes, en/em dashes and the ellipsis of the document arrived as U+0091..U+0097:
invisible C1 control characters, embedded and stored, matching no query anybody will ever
type. The WHATWG Encoding standard maps the label -- and every ``ascii`` spelling -- to
windows-1252 outright for exactly this reason, and browsers have done so since 2012.

**mail.** The body was decoded with ``get_content()``, which obeys the declared charset
and nothing else. A charset Python has no codec for raised ``LookupError`` and the whole
body was reported gone while its text sat in the file unread; a utf-8 body wearing an
``iso-8859-1`` label decoded without error into ``SeÃ±or FranÃ§ois`` and nothing counted
it, because a single-byte codec cannot report being wrong.

Every case here goes through ``diceo.chunk`` on a path -- what a caller sees, not what
an internal helper returns. The control tests matter as much as the rest: a diagnostic
that fires on an ordinary utf-8 page or an ordinary ``us-ascii`` mail is noise a caller
learns to ignore inside a day.

    uv run pytest tests/test_html_encoding_labels.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

import diceo
from diceo.types import Diagnostics

#: One sentence carrying every cp1252 byte that Latin-1 turns into an invisible control:
#: 0x93/0x94 curly quotes, 0x97 em dash, 0x96 en dash, 0x85 ellipsis. Plus 0xA7 and 0xE9,
#: which the two codecs agree on -- so a test can tell "wrong codec" from "no codec".
PUNCTUATION = "The “Regulation” — see §1 – applies… café"

#: Everything in U+0080..U+009F. Nothing authors these; one in the output means a byte
#: was read through a codec that mapped it nowhere.
C1 = range(0x80, 0xA0)


def _page(charset: str, body: str, encoding: str = "cp1252") -> bytes:
    return (
        f'<html><head><meta charset="{charset}"></head><body><p>'.encode()
        + body.encode(encoding)
        + b"</p></body></html>"
    )


def _mail(charset: str, body: bytes, subtype: str = "plain", cte: str = "8bit") -> bytes:
    return (
        b"From: audit@example.org\r\nTo: index@example.org\r\nSubject: Quarterly\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/" + subtype.encode() + b'; charset="' + charset.encode() + b'"\r\n'
        b"Content-Transfer-Encoding: " + cte.encode() + b"\r\n\r\n" + body + b"\r\n"
    )


def _read(raw: bytes, name: str, tmp_path: Path) -> tuple[str, Diagnostics]:
    path = tmp_path / name
    path.write_bytes(raw)
    report = Diagnostics()
    chunks = list(diceo.chunk(path, diagnostics=report))
    return "\n".join(chunk.text for chunk in chunks), report


# --------------------------------------------------------------------------- #
# html: the declared label
# --------------------------------------------------------------------------- #


def test_a_cp1252_page_declaring_iso_8859_1_keeps_its_punctuation(tmp_path: Path):
    """The defect itself, end to end."""
    text, report = _read(_page("iso-8859-1", PUNCTUATION), "latin1.html", tmp_path)

    assert PUNCTUATION in text
    assert not report.lost_data


def test_that_page_ships_no_invisible_control_characters(tmp_path: Path):
    """The half a caller cannot see: five characters that are *there* and are nothing.

    Worth its own assertion because the text above could be recovered while a stray C1
    still rode along -- and a C1 in a chunk is a character the embedder pays for, the
    store keeps and no query can reach.
    """
    text, _ = _read(_page("iso-8859-1", PUNCTUATION), "latin1.html", tmp_path)

    assert [hex(ord(char)) for char in text if ord(char) in C1] == []


@pytest.mark.parametrize(
    "label",
    [
        "iso-8859-1",
        "ISO-8859-1",
        "iso8859-1",
        "iso_8859-1",
        "iso-ir-100",
        "latin1",
        "l1",
        "cp819",
        "ibm819",
        "csisolatin1",
        "ascii",
        "us-ascii",
        "ansi_x3.4-1968",
        # Already cp1252 by any reading; here so the table cannot regress into one that
        # only handles the aliases and drops the canonical spelling.
        "windows-1252",
        "x-cp1252",
    ],
)
def test_every_whatwg_label_for_windows_1252_decodes_as_windows_1252(
    label: str, tmp_path: Path
):
    """The published label table, one row at a time.

    ``ascii`` and ``us-ascii`` are in the same row of the spec as ``latin1``: a document
    that declares ascii and then ships a byte above 0x7F has told you nothing, and the
    only decoding a browser will do with it is cp1252.
    """
    text, _ = _read(_page(label, PUNCTUATION), "labelled.html", tmp_path)

    assert PUNCTUATION in text


def test_a_genuine_c1_byte_is_removed_and_counted(tmp_path: Path):
    """Belt and braces, for the document that really does carry a C1 character.

    The label table cannot cover every route to a C1 -- this page is honest utf-8 and
    encodes U+0093 deliberately. Before, it went to the index invisible. Now it is gone
    from the text and present in the count, which is the trade rule 3 asks for.
    """
    # Escaped, not literal: a C1 in a source file is exactly as invisible to a
    # reviewer as it is in a chunk, which is the whole complaint.
    text, report = _read(
        _page("utf-8", "before\u0093after", encoding="utf-8"), "c1.html", tmp_path
    )

    assert "beforeafter" in text
    # Substring rather than list membership, the way every other test of this note is
    # written. What this test means is "the C1 was counted"; pinning the whole line
    # made it also assert that the note carries no explanation, which was never the
    # intent and is not what the other three formats' notes do.
    assert any("control_chars_removed=1" in note for note in report.notes), report.notes


# --------------------------------------------------------------------------- #
# html: control -- the ordinary page must be untouched
# --------------------------------------------------------------------------- #


def test_a_utf8_page_is_unchanged_and_gains_no_note(tmp_path: Path):
    """The 99% case. If this ever gains a note the diagnostic has become noise."""
    text, report = _read(_page("utf-8", PUNCTUATION, encoding="utf-8"), "clean.html", tmp_path)

    assert PUNCTUATION in text
    assert not report.lost_data
    assert [note for note in report.notes if not note.startswith("title=")] == []


def test_a_page_with_no_charset_declaration_at_all_is_unchanged(tmp_path: Path):
    """No label is not a wrong label: utf-8 is still tried first and still wins."""
    raw = b"<html><body><p>" + PUNCTUATION.encode("utf-8") + b"</p></body></html>"
    text, report = _read(raw, "bare.html", tmp_path)

    assert PUNCTUATION in text
    assert report.notes == []


def test_a_label_that_is_not_windows_1252_is_still_obeyed(tmp_path: Path):
    """The table is a correction, not a takeover.

    ``windows-1251`` is Cyrillic and means it. Mapping it to cp1252 would read
    ``Привет`` as ``Ïðèâåò`` -- the same class of damage, pointing the other way.
    """
    text, _ = _read(
        _page("windows-1251", "Привет", encoding="cp1251"),
        "cyrillic.html",
        tmp_path,
    )

    assert "Привет" in text


# --------------------------------------------------------------------------- #
# mail: the body reaches the caller whatever the label says
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "charset",
    [
        "x-unknown-8",
        # Not the same failure: `idna` is a real codec that refuses an error handler and
        # raises a bare `UnicodeError` rather than `LookupError`. A first cut of the fix
        # caught only `LookupError`, and this mail came back as a `DiceoError` with no
        # headers at all -- worse than the defect it replaced.
        "idna",
    ],
)
def test_a_body_whose_charset_label_is_unusable_still_arrives(charset: str, tmp_path: Path):
    """Rule 3's worst case: the text was in the file and the caller got none of it.

    ``get_content()`` raised on the label and the body was reported as
    ``body_undecodable`` -- accurate about the exception, wrong about the document,
    which is plain utf-8 and always was.
    """
    body = "The revenue rose 12% in España.".encode()
    text, report = _read(_mail(charset, body), "unusable.eml", tmp_path)

    assert "The revenue rose 12% in España." in text
    assert not report.lost_data
    assert any(note.startswith(f"charset_unusable={charset}") for note in report.notes)


def test_a_body_with_no_charset_at_all_is_read_from_its_bytes(tmp_path: Path):
    """The third shape, found while fixing the other two.

    RFC 2045 defaults an absent charset to us-ascii and ``get_content()`` obeys that
    literally, so every byte above 0x7F came back as U+FFFD -- two characters gone from
    this mail and ``lost_data`` True, on a body that is ordinary utf-8. The bytes are
    the better witness, and they are the only witness a header did not write.
    """
    raw = (
        b"From: audit@example.org\r\nSubject: Quarterly\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: text/plain\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
        + "Revenue rose in España.".encode()
        + b"\r\n"
    )
    text, report = _read(raw, "nocharset.eml", tmp_path)

    assert "Revenue rose in España." in text
    assert not report.lost_data


def test_a_utf8_body_mislabelled_latin1_is_read_as_utf8(tmp_path: Path):
    """Mojibake that decoded without an error, because a single-byte codec always does."""
    body = "Señor François — café".encode()
    text, report = _read(_mail("iso-8859-1", body), "mislabelled.eml", tmp_path)

    assert "Señor François — café" in text
    assert "SeÃ±or" not in text
    assert any(note.startswith("charset_override=iso-8859-1->utf-8") for note in report.notes)


def test_a_declared_codec_that_produced_replacements_loses_to_clean_utf8(tmp_path: Path):
    """The other half of the override: the label decoded, and decoded to damage.

    An odd-length payload cannot be utf-16, so the declared decode ends in U+FFFD while
    the same bytes are flawless utf-8. Cyrillic deliberately, because its utf-8 lead
    bytes are 0xD0/0xD1: the mojibake signature the test above relies on cannot fire
    here, so this really is the replacement-character clause on its own. Base64, because
    the parser rewrites CRLF to LF in an 8bit body and that would even the length out.
    """
    import base64

    body = "Привет всем!!".encode()
    assert len(body) % 2 == 1
    text, report = _read(
        _mail("utf-16-be", base64.b64encode(body), cte="base64"), "odd.eml", tmp_path
    )

    assert "Привет всем!!" in text
    assert any(note.startswith("charset_override=utf-16-be->utf-8") for note in report.notes)


def test_a_genuinely_cp1252_body_is_not_flipped_to_utf8(tmp_path: Path):
    """The override must need evidence, not a hunch.

    These bytes are not valid utf-8, so there is nothing to override with and the
    declared label -- corrected to cp1252 by the same WHATWG table -- is simply right.
    """
    text, report = _read(
        _mail("iso-8859-1", PUNCTUATION.encode("cp1252")), "real-latin1.eml", tmp_path
    )

    assert PUNCTUATION in text
    assert [note for note in report.notes if note.startswith("charset_override")] == []


def test_a_declared_charset_utf8_cannot_guess_still_wins(tmp_path: Path):
    """The label leads; it just does not get the last word.

    Nothing in the bytes says ``windows-1251``. Dropping the declared charset in favour
    of the sniffer would read this as ``Ïðèâåò`` -- so the fix must not become "always
    prefer utf-8".
    """
    text, report = _read(
        _mail("windows-1251", "Привет".encode("cp1251")),
        "cyrillic.eml",
        tmp_path,
    )

    assert "Привет" in text
    assert [note for note in report.notes if note.startswith("charset_")] == []


def test_the_override_needs_evidence_and_not_merely_a_valid_utf8_payload(tmp_path: Path):
    """The pin the two tests above only look like.

    An adversarial pass mutated the rule to fire whenever the payload is *valid* UTF-8
    -- the exact degeneration those tests are credited with preventing -- and both of
    them still passed. They could not do otherwise: ``'Привет'.encode('cp1251')`` and
    cp1252 punctuation are both invalid UTF-8, so the branch under test is unreachable
    from either fixture. A control that survives the mutation it names is decoration.

    This one discriminates. UTF-8 Cyrillic decodes through cp1252 without a single
    U+FFFD (every byte it uses is defined there) and carries no ``Ã``/``Â``/``â`` bigram,
    because those lead bytes are Latin-1-supplement and General-Punctuation ones and
    this is neither. So the declared reading stands, mojibake and all, and the mutated
    rule -- which would return ``Привет`` with a ``charset_override`` note -- fails here
    and only here.

    Pinning it is not the same as calling it ideal: the utf-8 reading is very probably
    what the producer meant. But widening the override to "valid UTF-8 is enough" also
    flips every genuine single-byte body that happens to survive a UTF-8 parse, and
    that is a policy call for the retrieval harness, not a fix to slip in under a test.
    What rule 3 requires meanwhile is that the caller be told the ambiguity exists.
    """
    utf8_cyrillic = "Привет".encode()  # UTF-8 bytes, carrying a windows-1252 label
    text, report = _read(_mail("windows-1252", utf8_cyrillic), "ambiguous.eml", tmp_path)

    assert "Привет" not in text, "the declared label still leads"
    assert [note for note in report.notes if note.startswith("charset_override")] == []
    assert any(note.startswith("charset_ambiguous=windows-1252") for note in report.notes)


def test_an_ascii_body_is_never_called_ambiguous(tmp_path: Path):
    """The control for the note above, and the reason it is not simply "valid UTF-8".

    Pure ASCII is valid UTF-8, so a windows-1252 label on an ASCII body -- the commonest
    single shape in any mail archive -- would otherwise carry an ambiguity note on every
    message, and a note that fires on everything is one a caller filters out within a
    day, taking the real ones with it.
    """
    text, report = _read(
        _mail("windows-1252", b"Revenue rose 12 percent."), "plain.eml", tmp_path
    )

    assert "Revenue rose 12 percent." in text
    assert [note for note in report.notes if note.startswith("charset_")] == []


@pytest.mark.parametrize("cte", ["base64", "quoted-printable"])
def test_transfer_encodings_still_decode(cte: str, tmp_path: Path):
    """Reading the payload rather than the content moved the CTE onto us.

    ``get_payload(decode=True)`` undoes base64 and quoted-printable the same way
    ``get_content()`` did -- pinned here because if it did not, every encoded mail in a
    corpus would arrive as its own armour and no test above would have noticed.
    """
    import email.message
    import email.policy

    message = email.message.EmailMessage(policy=email.policy.default)
    message["Subject"] = "Quarterly"
    message.set_content("Revenue rose in España…", cte=cte)
    text, report = _read(message.as_bytes(), f"{cte}.eml", tmp_path)

    assert "Revenue rose in España…" in text
    assert not report.lost_data


def test_an_html_mail_body_is_decoded_the_same_way(tmp_path: Path):
    """The body chooser renders html parts too, so both routes need the same decoding."""
    body = b"<html><body><p>" + "café — España".encode() + b"</p></body></html>"
    text, _ = _read(_mail("iso-8859-1", body, subtype="html"), "html-body.eml", tmp_path)

    assert "café — España" in text


# --------------------------------------------------------------------------- #
# mail: control -- the ordinary message must be untouched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("charset", ["utf-8", "us-ascii", "iso-8859-1"])
def test_an_ordinary_ascii_mail_gains_no_encoding_note(charset: str, tmp_path: Path):
    """The noise test, and the reason it is parametrized.

    ``us-ascii`` is the declared charset on a large share of real mail and
    ``iso-8859-1`` on much of the rest. If correcting the label made either of them
    announce itself, every mail in a corpus would carry a note and the note would stop
    meaning anything.
    """
    text, report = _read(_mail(charset, b"Revenue rose 12 percent."), "plain.eml", tmp_path)

    assert "Revenue rose 12 percent." in text
    assert not report.lost_data
    assert [note for note in report.notes if note.startswith("charset_")] == []
    assert [note for note in report.notes if note.startswith("control_chars")] == []


def test_a_well_formed_utf8_mail_is_byte_identical(tmp_path: Path):
    """Exact text, not "contains" -- the control has to be able to fail."""
    body = "Señor François — café".encode()
    text, report = _read(_mail("utf-8", body), "clean.eml", tmp_path)

    assert text == (
        "Quarterly\nFrom: audit@example.org\nTo: index@example.org\nSeñor François — café"
    )
    # Every mail carries the "parsed whole" note: that is a property of the format
    # and of the standard library's parser, not a finding about this document.
    # Pinned rather than prefix-filtered, so a real finding still fails here.
    assert [note for note in report.notes if not note.startswith("eml is parsed whole")] == []
    assert not report.lost_data
