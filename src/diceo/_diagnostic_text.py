"""One spelling per diagnostic, in one place.

:attr:`Diagnostics.notes <diceo.types.Diagnostics.notes>` and
:attr:`~diceo.types.Diagnostics.truncated` are a contract: callers grep them, alert
on them and count them across a corpus. That only works if one fact has one spelling,
and it did not.

* "how many images we did not extract" arrived as ``images=3 (not extracted)`` from
  the HTML and spreadsheet readers and as ``media_parts=3`` from the OOXML one --
  and the spreadsheet and OOXML counts come from the *same regex over the same
  archive*, so those two were one fact under two keys.
* "the first N, and how many more" was written out longhand at five sites in three
  shapes, one of which quietly omitted the "and N more" and so reported 20 hidden
  slides out of 25 as though there were 20.
* ``control_chars_removed`` came with a PDF-specific explanation at one site and bare
  at two others, so the same event read differently depending on the format.

Nothing here is public API -- the leading underscore says so -- but the strings it
returns *are*. Changing one changes what a caller's grep finds, which is why the
tests that pin them are doing their job rather than being brittle.
"""

from __future__ import annotations

from collections.abc import Sequence


def bounded_list(items: Sequence[object], *, limit: int = 20, total: int | None = None) -> str:
    """``"a, b, c and 4 more"`` -- a list inside a diagnostic, bounded by us.

    Bounded because the alternative is a note whose length is chosen by the document:
    a workbook with 40,000 repeated sheet parts must not put 40,000 names in a log
    line. The count that matters is always spelled out separately by the caller
    (``parts_missing=25 (...)``); this renders only the examples.

    ``total`` is for the sites that bound the list while *collecting* it, where
    ``len(items)`` is already the truncated length and the real count lives elsewhere.
    """
    shown = [str(item) for item in items[:limit]]
    hidden = (len(items) if total is None else total) - len(shown)
    listed = ", ".join(shown)
    return f"{listed} and {hidden} more" if hidden > 0 else listed


def images_note(count: int) -> str:
    """Pictures that are in the document and not in the index.

    ``images`` rather than ``media_parts``: the OOXML term names the part, and what a
    caller wants to grep for is the thing. The OOXML *field* is still
    ``OoxmlDiagnostics.media_parts``, because there the part is what is being counted.
    """
    return f"images={count} (not extracted)"


def control_chars_note(count: int) -> str:
    """Characters removed because a caller could not store them.

    Deliberately does not name a character range. The PDF reader strips C0 and DEL;
    the text reader also strips C1 and keeps the form feed that the PDF reader does
    not. The old PDF-side wording ("C0/DEL bytes in the text layer") was accurate
    there and wrong everywhere else, which is the failure mode of a message that has
    more than one author.
    """
    return f"control_chars_removed={count} (unstorable control characters)"
