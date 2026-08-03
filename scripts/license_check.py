"""Fail the build if the default install carries a copyleft dependency.

This is [rule 1](../docs/principles.md) mechanised.
The whole reason diceo exists rather than `pip install pymupdf4llm` is that the
default dependency tree is safe for commercial use, and a claim like that decays
the moment it stops being checked -- a transitive dependency can add a copyleft
licence in a patch release without anyone noticing.

Checked against the **default** install only, in a throwaway environment. The
benchmark harness in the research repository deliberately installs licence-tainted
competitors so we can measure against them; none of that may ever reach this tree::

    uv run python scripts/license_check.py            # default deps
    uv run python scripts/license_check.py --show-all

Three failure modes are treated as failures, not warnings:

* a **forbidden** licence anywhere in the tree;
* an **unrecognised** licence string -- silence is not consent. A new spelling of
  a permissive licence gets added to ``ALLOWED`` by a human who read it.
* an **unaudited copyleft notice** bundled inside a wheel. A declared licence
  string is metadata; the notice files a binary wheel ships are evidence about
  what is actually linked into it, and nothing reads them unless this does.
"""

from __future__ import annotations

import argparse
import importlib.metadata as metadata
import json
import pathlib
import re
import subprocess
import sys

# Substrings that disqualify a dependency from the default install. Matched
# case-insensitively against the declared licence string.
FORBIDDEN = (
    "gpl",  # catches GPL, AGPL, LGPL -- LGPL is arguable, but not silently
    "sspl",
    "commons clause",
    "elastic license",
    "business source",
    "cc-by-nc",
    "cc by-nc",
    "proprietary",
    "osl",
    "eupl",
)

# Licences that are fine in the default tree, spelled the way humans write them.
# Both these and the declared strings go through `normalise`, so "Apache 2.0",
# "Apache-2.0" and "Apache Software License" all compare equal.
ALLOWED_NAMES = (
    "Apache-2.0",
    "Apache Software License",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD License",
    "MIT",
    "MIT License",
    "MIT-CMU",
    "ISC",
    "ISC License",
    "Python Software Foundation License",
    "PSF-2.0",
    "Mozilla Public License 2.0",  # file-level copyleft; does not reach our distribution
    "MPL-2.0",
    "The Unlicense",
    "Public Domain",
    "Zlib",
    "0BSD",
    "CC0-1.0",
)

# "GPL" appears inside these as an *alternative* or a false positive, so a bare
# substring test would reject them wrongly. Any addition needs a comment saying
# which file was read.
EXEMPT_PACKAGES: dict[str, str] = {}

# --------------------------------------------------------------------------- #
# What a wheel *ships*, as opposed to what it *declares*
# --------------------------------------------------------------------------- #
#
# pypdfium2 declares `License: BSD-3-Clause, Apache-2.0, dependency licenses`.
# That last clause is a binary blob's worth of transitive licensing hidden behind
# three words, and the metadata check above can only shrug at it. So read the
# notice files the wheel actually installs.

COPYLEFT_NOTICE = re.compile(
    r"gnu (?:general public|lesser general public|library general public"
    r"|affero general public) licen[cs]e|server side public licen[cs]e",
    re.IGNORECASE,
)

#: A copyleft notice is not automatically an infection: a bundled NOTICE file
#: covers a whole upstream source tree, while a wheel ships a fraction of it.
#: Each entry is `(distribution, notice file, section)` -> why it is harmless,
#: written by a human who opened the file. An unlisted section fails the build,
#: so a future pdfium build that links something new cannot pass silently.
AUDITED_NOTICES: dict[tuple[str, str, str], str] = {
    ("pypdfium2", "icu.txt", "File: aclocal.m4 (only for ICU4C)"): (
        "GPL-2.0+ with the Autoconf exception, and ICU's own notice states the "
        "exception's condition is met. It is pkg.m4 from ICU4C's autotools "
        "plumbing: a build-time script, not linked into libpdfium.so, and no file "
        "of that name is installed (checked below)."
    ),
    ("pypdfium2", "icu.txt", "File: config.guess (only for ICU4C)"): (
        "GPL-3.0+ with the same Autoconf exception, same reasoning: a build-time "
        "script of ICU4C, absent from the wheel."
    ),
}

#: The load-bearing half of the two audits above, made checkable rather than
#: asserted: the GPL-covered files must not be installed by anything.
AUDITED_ABSENT_FILES = ("aclocal.m4", "config.guess", "install-sh")


def notice_files(dist: metadata.Distribution) -> list[pathlib.Path]:
    """Every licence/notice file a distribution installs alongside its metadata."""
    base = pathlib.Path(str(dist.locate_file("")))
    out = []
    for entry in dist.files or ():
        name = pathlib.PurePath(str(entry)).name.upper()
        if "LICENSE" in str(entry).upper() or name.startswith(("COPYING", "NOTICE")):
            path = base / str(entry)
            if path.is_file():
                out.append(path)
    return out


def copyleft_sections(text: str) -> list[str]:
    """Which section of a multi-part notice file carries each copyleft mention.

    A bundled NOTICE concatenates dozens of upstream licences under `File:`
    headers -- ICU's runs to 27 KB. Reporting the *file* would say "pypdfium2
    mentions the GPL" every time and teach the reader to skip the line; reporting
    the section says which four hundred bytes to go and read.
    """
    found: list[str] = []
    for match in COPYLEFT_NOTICE.finditer(text):
        section = "<no section header>"
        for line in reversed(text[: match.start()].splitlines()):
            if line.strip().lower().startswith("file:"):
                section = line.strip()
                break
        if section not in found:
            found.append(section)
    return found


def audit_notices() -> tuple[list[tuple[str, str, str]], list[str], int]:
    """Returns (unaudited findings, files that should not exist but do, files read)."""
    findings: list[tuple[str, str, str]] = []
    read = 0
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        for path in notice_files(dist):
            read += 1
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for section in copyleft_sections(text):
                key = (name, path.name, section)
                if key not in AUDITED_NOTICES:
                    findings.append(key)

    present = []
    for dist in metadata.distributions():
        for entry in dist.files or ():
            if pathlib.PurePath(str(entry)).name in AUDITED_ABSENT_FILES:
                present.append(f"{dist.metadata['Name']}: {entry}")
    return findings, present, read


def normalise(text: str) -> list[str]:
    """One licence string may name several licences. Split and slugify each.

    Dual grants ("Apache-2.0 OR BSD-3-Clause", pypdfium2's) split into both
    alternatives, and a package passes if *any* alternative is allowed -- which is
    what "or" means legally: we may take the permissive one.
    """
    parts = re.split(r"\s*(?:;|,| OR | or |/)\s*", text.strip())
    out = []
    for part in parts:
        slug = re.sub(r"[^a-z0-9]+", "-", part.strip().lower()).strip("-")
        slug = re.sub(r"^the-", "", slug)
        slug = re.sub(r"-licen[cs]e$", "", slug)
        if slug:
            out.append(slug)
    return out


ALLOWED = {slug for name in ALLOWED_NAMES for slug in normalise(name)}


def collect() -> list[dict]:
    raw = subprocess.run(
        [
            sys.executable,
            "-m",
            "piplicenses",
            "--format=json",
            "--with-urls",
            "--with-system",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show-all", action="store_true")
    args = parser.parse_args()

    packages = collect()
    forbidden: list[tuple[str, str, str]] = []
    unknown: list[tuple[str, str]] = []
    vague: list[tuple[str, str, tuple[str, ...]]] = []

    for package in packages:
        name = package["Name"]
        licence = package.get("License", "UNKNOWN") or "UNKNOWN"
        if name in EXEMPT_PACKAGES:
            continue
        lowered = licence.lower()

        hit = next((bad for bad in FORBIDDEN if bad in lowered), None)
        if hit:
            forbidden.append((name, licence, hit))
            continue
        slugs = normalise(licence)
        if not any(slug in ALLOWED for slug in slugs):
            unknown.append((name, licence))
            continue
        # A package passes on any *one* recognised alternative, because that is
        # what a dual grant means. But a comma-separated list is an "and", and a
        # bundled binary can hide licences behind a phrase like "dependency
        # licenses" -- pypdfium2 does exactly that for PDFium's vendored
        # components. Passing that silently would hollow out the whole gate.
        leftover = tuple(s for s in slugs if s not in ALLOWED)
        if leftover:
            vague.append((name, licence, leftover))

    print(f"checked {len(packages)} installed distributions")
    if args.show_all:
        for package in sorted(packages, key=lambda p: p["Name"].lower()):
            print(f"  {package['Name']:<34} {package.get('License', '?')}")

    if forbidden:
        print(f"\nFORBIDDEN licences ({len(forbidden)}):")
        for name, licence, hit in forbidden:
            print(f"  {name:<34} {licence}   [matched {hit!r}]")
    if unknown:
        print(f"\nUNRECOGNISED licences ({len(unknown)}) -- a human must classify these:")
        for name, licence in unknown:
            print(f"  {name:<34} {licence}")
    if vague:
        print(
            f"\nunclassified components ({len(vague)}) -- these name a permissive licence "
            f"*and* something no metadata field explains. What they actually ship is "
            f"checked by the notice audit below, not taken on trust:"
        )
        for name, licence, leftover in vague:
            print(f"  {name:<34} {licence}   [unclassified: {', '.join(leftover)}]")

    unaudited, wrongly_present, notices_read = audit_notices()
    print(
        f"read {notices_read} bundled licence/notice files; "
        f"{len(AUDITED_NOTICES)} copyleft mentions audited and accounted for"
    )
    if unaudited:
        print(f"\nUNAUDITED copyleft notices ({len(unaudited)}) -- a human must read these:")
        for name, filename, section in unaudited:
            print(f"  {name:<20} {filename:<16} {section}")
    if wrongly_present:
        print("\nA file an audit assumed was absent is installed after all:")
        for entry in wrongly_present:
            print(f"  {entry}")

    if forbidden or unknown or unaudited or wrongly_present:
        print("\nlicense-check FAILED")
        return 1
    print("license-check passed: default install is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
