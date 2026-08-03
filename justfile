# diceo task entry points. `just` with no argument lists them.
#
# Everything runs through uv -- never pip, and never a bare `python`, so the
# environment is always the locked one.
#
# This is the *package* repo and it is deliberately small: the library, its tests,
# and the gates that keep its promises honest. Benchmarks, the retrieval harness and
# the research record live in the parent repository, which carries this one as a
# submodule. docs/principles.md says why.

[doc('List the available recipes.')]
default:
    @just --list

# ---------------------------------------------------------------------------- #
# Quality gates
# ---------------------------------------------------------------------------- #

# Full gate, in the order that fails fastest.
[doc('Lint, tests and the licence gate, fail-fast order.')]
check: lint test license-check

# `--group dev` on every recipe that needs a tool rather than the library: ruff
# and pytest live there, and without it a fresh clone's first command is `uv run
# ruff` against an environment that has no ruff in it.
[doc('Ruff check, ruff format --check, then the link checker.')]
lint:
    uv run --group dev ruff check src scripts tests
    uv run --group dev ruff format --check src scripts tests
    uv run --group dev python scripts/check_links.py

[doc('Format and autofix with ruff.')]
fmt:
    uv run --group dev ruff format src scripts tests
    uv run --group dev ruff check --fix src scripts tests

# CI runs this exact command on every matrix cell, so green here and green there
# mean the same thing. Skips print with their reason (`-rs`, set in pyproject):
# the suites that need the private corpus under `data/` are meant to skip on a
# fresh clone, and a skip nobody can see is indistinguishable from a hole.
[doc('Run the test suite exactly as CI runs it.')]
test:
    uv run --group dev pytest -q

# Rule 1, mechanised: no copyleft in the *default* install. Runs in a throwaway
# environment holding only the default dependencies -- checking the dev venv would
# pass or fail depending on which dependency groups happened to be synced.
#
# It reads the licence and NOTICE files the wheels *bundle*, not just their declared
# metadata, because a binary wheel can carry copyleft text its own metadata never
# mentions. Every copyleft mention has to be individually audited and accounted for.
#
# CI runs *this recipe*, not a reimplementation of it: the gate the README claims
# and the gate that runs are the same code, or the claim decays into a badge.
[doc('Rule 1, mechanised: no copyleft in the default install.')]
license-check:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    uv venv --quiet "$tmp/venv"
    VIRTUAL_ENV="$tmp/venv" uv pip install --quiet . pip-licenses
    VIRTUAL_ENV="$tmp/venv" uv run --no-project python scripts/license_check.py

# What the default install actually pulls in, with licences.
[doc("List the default install's dependency licences.")]
license-list:
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    uv venv --quiet "$tmp/venv"
    VIRTUAL_ENV="$tmp/venv" uv pip install --quiet . pip-licenses
    VIRTUAL_ENV="$tmp/venv" uv run --no-project pip-licenses --format=markdown \
      --with-urls --order=license

# ---------------------------------------------------------------------------- #
# Test fixtures
#
# Generated, never committed: `data/` is gitignored. Everything here is built by a
# write-only library from the `dev` group, so the documents have known content and
# known pathologies and no test depends on a file anyone has to keep safe. The one
# exception is the PDF seed, a public arXiv paper the script downloads on first run
# and checks against a pinned digest.
# ---------------------------------------------------------------------------- #

# Born-digital PDFs with real tables, plus the degenerate-font cases.
[doc('Born-digital PDF and Office fixtures.')]
fixtures:
    uv run --group dev python scripts/make_pdf_fixtures.py
    uv run --group dev python scripts/make_office_fixtures.py

# Spreadsheet fixtures: 100k and 1M rows in both XLSX string dialects, plus the
# small type-coverage workbooks the correctness gate needs. ~70 s, ~90 MB.
[doc('Spreadsheet fixtures: 100k and 1M rows. ~70 s, ~90 MB.')]
xlsx-fixtures:
    uv run --group dev python scripts/make_xlsx_fixtures.py

# DOCX/PPTX speed fixtures: 5,000 paragraphs, a 500-slide deck, and the run-dense
# twin that tells a per-node parser apart from a per-run scanner. ~12 s, ~2 MB.
[doc('DOCX/PPTX speed fixtures, run-dense twin. ~12 s, ~2 MB.')]
ooxml-fixtures:
    uv run --group dev python scripts/make_ooxml_fixtures.py

# Everything the tests can build for themselves.
[doc('Build every fixture set: PDF, Office, XLSX, OOXML.')]
all-fixtures: fixtures xlsx-fixtures ooxml-fixtures

# ---------------------------------------------------------------------------- #
# Packaging
# ---------------------------------------------------------------------------- #

# Build, then prove the wheel holds the package and nothing else. The check is
# here rather than in a reviewer's head because "the wheel accidentally shipped the
# benchmark corpus" is discovered by users, not by authors.
[doc('Build the wheel and prove it holds only the package.')]
build:
    #!/usr/bin/env bash
    set -euo pipefail
    rm -rf dist
    uv build
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    listing="$tmp/wheel.txt"
    uv run --no-project python -m zipfile -l dist/*.whl > "$listing"
    if grep -E '(^|/)(bench|data|docs|scripts)/' "$listing"; then
      echo "ERROR: the wheel contains files that are not the package" >&2; exit 1
    fi
    grep -q 'diceo/py.typed' "$listing" || { echo "ERROR: py.typed missing" >&2; exit 1; }
    echo "wheel is clean:"; cat "$listing"

# Install the built wheel into a throwaway venv and run it, the way a user would.
# Import time is printed because it is a number users notice and authors never do.
[doc('Install the built wheel in a fresh venv and run it.')]
smoke: build
    #!/usr/bin/env bash
    set -euo pipefail
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    uv venv --quiet "$tmp/venv"
    VIRTUAL_ENV="$tmp/venv" uv pip install --quiet dist/*.whl
    VIRTUAL_ENV="$tmp/venv" uv run --no-project python -c \
      "import time; t=time.perf_counter(); import diceo; \
       print(f'import diceo: {1000*(time.perf_counter()-t):.0f} ms, v{diceo.__version__}')"
    VIRTUAL_ENV="$tmp/venv" uv run --no-project diceo --version
