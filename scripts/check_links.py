"""Do the relative links in the docs and docstrings actually resolve?

Written after four broken links reached the docs in one sitting -- a file renamed, a
path guessed from memory. Broken links here are worse than in ordinary docs, because
the whole value of a claim is that you can click through to the measurement that
produced it. A dead link turns a cited number back into a vibe.

Checks markdown links in `docs/`, `*.md` at the root, and the `[label](...)` style
references embedded in `src/`, `scripts/` and `tests/` docstrings, which no markdown
linter would look at::

    uv run python scripts/check_links.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: `[text](target)`, ignoring images and anything with a scheme or a mailto.
_LINK = re.compile(r"(?<!!)\[([^\]]{1,120})\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SKIP_SCHEME = re.compile(r"^(?:https?:|mailto:|#)")
#: Only targets that are plausibly a document reference. Without this, Python source
#: is full of false positives -- `timings["load_doc"](path)` and the character class
#: `[xX](0-9a-fA-F]+)` are both a perfect match for the markdown link grammar.
_DOC_TARGET = re.compile(r"(?:^\.{1,2}/|\.md$|\.md#)")

#: Every directory this repository actually has. Listing one it does not have would
#: make the checker quietly cover less than its output claims.
ROOTS = ("docs", "src", "scripts", "tests")
ROOT_FILES = ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md")


def _targets(path: Path) -> list[tuple[str, str, int]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    out = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, target in _LINK.findall(line):
            if _SKIP_SCHEME.match(target) or not _DOC_TARGET.search(target):
                continue
            out.append((label, target, line_number))
    return out


def main() -> int:
    repo = Path.cwd()
    files: list[Path] = [repo / name for name in ROOT_FILES if (repo / name).exists()]
    for root in ROOTS:
        base = repo / root
        if not base.exists():
            continue
        files += sorted(base.rglob("*.md"))
        files += sorted(base.rglob("*.py"))

    broken: list[str] = []
    checked = 0
    for path in files:
        for label, target, line_number in _targets(path):
            checked += 1
            # Strip an anchor: we check that the *file* exists, not the heading,
            # because heading slugs drift and a wrong anchor still lands the reader
            # on the right document.
            file_part = target.split("#", 1)[0]
            if not file_part:
                continue
            resolved = (path.parent / file_part).resolve()
            if not resolved.exists():
                rel = path.relative_to(repo)
                broken.append(f"{rel}:{line_number}: [{label[:44]}] -> {target}")

    print(f"checked {checked} relative links across {len(files)} files")
    if broken:
        print(f"\n{len(broken)} broken:")
        for item in broken:
            print(f"  {item}")
        return 1
    print("all resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
