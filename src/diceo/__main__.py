"""``diceo`` on the command line: see it work before writing any code::

    diceo report.pdf                     # chunks, one JSON object per line
    diceo report.pdf --text              # just the text, readable
    diceo *.pdf --diagnostics            # what would be missing from your index
    diceo mystery.bin --sniff            # what is this file?

Exists because the first thing anyone does with an extraction library is point it
at one difficult file and look at the output. Making them write a script first is
a bad first five minutes, and a JSONL chunk stream also happens to be exactly what
a shell pipeline into an indexer wants.

Exit codes are meant for scripts: ``0`` all good, ``1`` at least one document
failed, ``2`` at least one document was incomplete (something is missing from the
index and you were told). Bad usage is ``64``, as ``sysexits.h`` says.
"""

from __future__ import annotations

import argparse
import json
import sys

from diceo import Diagnostics, Limits, __version__, chunk, sniff
from diceo.errors import DiceoError

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_INCOMPLETE = 2
EXIT_USAGE = 64
EXIT_BUG = 70  # sysexits.h EX_SOFTWARE -- ours, not the document's


class _ArgumentParser(argparse.ArgumentParser):
    """Use the usage exit code this CLI promises instead of argparse's generic 2."""

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


#: An *instance*, deliberately. ``Limits`` is a slots dataclass, so
#: ``Limits.target_chars`` is a slot descriptor rather than the default value --
#: passing it through as a default silently put a `member_descriptor` into the
#: chunker's arithmetic. Caught by tests/test_readers.py; kept as a comment because
#: it is the kind of mistake that gets rewritten back in.
DEFAULTS = Limits()


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="diceo",
        description="Turn documents into retrieval-ready chunks.",
        epilog=(
            "One JSON object per chunk by default, so `diceo *.pdf | your-indexer` "
            "works. Exit code 2 means something was left out and the diagnostics say "
            "what."
        ),
    )
    parser.add_argument("files", nargs="*", help="document paths or HTTP(S) URLs to process")
    parser.add_argument("--version", action="version", version=f"diceo {__version__}")

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--text", action="store_true", help="print chunk text instead of JSON")
    output.add_argument(
        "--sniff", action="store_true", help="print the detected format and stop"
    )
    output.add_argument(
        "--count", action="store_true", help="print one summary line per document"
    )

    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="print what was skipped, to stderr, for every document",
    )
    parser.add_argument(
        "--target",
        type=int,
        default=DEFAULTS.target_chars,
        metavar="CHARS",
        help=f"chunk size aim (default {DEFAULTS.target_chars})",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=0,
        metavar="CHARS",
        help="repeat this many characters of the previous chunk "
        "(measured to cost up to 8pp of recall; off by default)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        metavar="N",
        help="stop after this many PDF pages (reported as a truncation)",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        metavar="N",
        help="stop after this many rows per sheet (reported as a truncation)",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=DEFAULTS.download_timeout,
        metavar="SECONDS",
        help="socket timeout for URL downloads (default 30)",
    )
    parser.add_argument(
        "--max-download-bytes",
        type=int,
        default=DEFAULTS.max_download_bytes,
        metavar="N",
        help="maximum URL body size (default 128 MiB)",
    )
    parser.add_argument(
        "--meta",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="attach metadata to every chunk; repeatable",
    )
    return parser


def _parse_meta(pairs: list[str]) -> dict[str, object]:
    out: dict[str, object] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--meta needs KEY=VALUE, got {pair!r}")
        try:  # a number or a list stays typed in the index rather than becoming a string
            out[key] = json.loads(value)
        except json.JSONDecodeError:
            out[key] = value
    return out


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.files:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        limits = Limits(
            target_chars=args.target,
            overlap_chars=args.overlap,
            max_pages=args.max_pages,
            max_rows=args.max_rows,
            download_timeout=args.download_timeout,
            max_download_bytes=args.max_download_bytes,
        )
    except ValueError as exc:
        print(f"diceo: {exc}", file=sys.stderr)
        return EXIT_USAGE
    try:
        meta = _parse_meta(args.meta)
    except ValueError as exc:
        print(f"diceo: {exc}", file=sys.stderr)
        return EXIT_USAGE
    status = EXIT_OK

    for path in args.files:
        if args.sniff:
            try:
                print(f"{path}\t{sniff(path, limits=limits)}")
            except DiceoError as exc:
                print(f"{path}\t{exc.reason}", file=sys.stderr)
                status = EXIT_FAILED
            continue

        report = Diagnostics()
        count = 0
        try:
            for piece in chunk(path, limits=limits, diagnostics=report, meta=meta or None):
                count += 1
                if args.count:
                    continue
                if args.text:
                    print(piece.text)
                    print()
                else:
                    # Compact separators: a million chunks of JSONL is a real output
                    # size and the spaces are 8% of it.
                    print(
                        json.dumps(
                            {**piece.meta, "text": piece.text},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    )
        except DiceoError as exc:
            print(f"diceo: {exc}", file=sys.stderr)
            status = EXIT_FAILED
            continue
        except KeyboardInterrupt:  # pragma: no cover
            return 130
        except Exception:  # noqa: BLE001 - a bug in diceo, and it must say so
            import traceback

            traceback.print_exc()
            print(
                f"\ndiceo: the above is a BUG IN DICEO, not a problem with "
                f"{path}.\nEvery document failure is supposed to be a DiceoError. "
                f"Please report it:\n  "
                f"https://github.com/gergopool/diceo/issues/new\n"
                f"Including the file (or its first few KB) makes it fixable.",
                file=sys.stderr,
            )
            return EXIT_BUG

        if args.count:
            print(f"{path}\t{report.format}\t{count} chunks\t{report.chars} chars")
        if args.diagnostics:
            print(
                f"{path}: {json.dumps(report.as_dict(), ensure_ascii=False)}", file=sys.stderr
            )
        if report.lost_data:
            if status != EXIT_FAILED:
                status = EXIT_INCOMPLETE
            if not args.diagnostics:
                # Never silent: the whole point of the package is that you find out.
                for what in report.truncated:
                    print(f"diceo: {path}: incomplete: {what}", file=sys.stderr)

    return status


if __name__ == "__main__":
    sys.exit(main())
