# Security policy

## Reporting

Report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/gergopool/diceo/security/advisories/new)
rather than a public issue. Expect an acknowledgement within a week.

## The threat model

diceo parses **untrusted files** — that is its whole job. If you are indexing a SharePoint
library, a mail archive or a web crawl, assume some of those documents are hostile, because
some of them are.

What that means for this package:

- **A malformed or malicious document must not crash the process, hang it, or exhaust memory.**
  It must produce a `DiceoError` or partial output plus diagnostics.
  `tests/test_robustness.py` holds this over several hundred deliberately broken files on every
  commit. A file that segfaults the interpreter or spins forever is a **security bug**, not a
  compatibility issue, and is in scope.
- **Guards that exist today**: ZIP members over 2 GB uncompressed are refused from the central
  directory before inflating; members over 64 MB that expand more than 200× are refused as zip
  bombs; an archive declaring more than 4 GB across *all* its members, or more than 65,536
  members, is refused whatever any single member claims.
- **A document type declaration is refused outright, in every part we parse**, since 0.1.1.
  This entry used to hand the whole problem to libexpat, and that was too generous. libexpat
  ≥ 2.4.0 does carry an input-amplification limiter, but it is a **ratio** — roughly 100×,
  armed only past 8 MiB of output — so padding an entity with literal filler that deflates to
  nothing buys expansion in proportion to the padding while keeping the ratio legal. Measured
  before the fix: a **19.8 KB `.docx`** declaring a 20 MB part reached **2.0 GB** of resident
  memory and returned a chunk *with no error at all*, with `Limits(max_seconds=1)` set — the
  cost is paid inside one parser call, before the first block exists, where no budget reaches.
  No office format writes a doctype, so refusing it costs a real document nothing and does not
  depend on which libexpat the host links. General entities are still expanded where a
  declaration is legal; there is no longer anywhere in an office document that it is.
- **Nesting and unclosed-element breadth are bounded per streamed part** (256 deep, 262,144
  unclosed), also since 0.1.1. `iterparse` retains one element per start tag until something
  clears it, and neither shape touches a byte ceiling: 100 KB of nested `w:tbl` reached 1.2 GB,
  and a single `<row>` of two million `<c>` reached 623 MB. Both are `CorruptDocument` now.
- **External entities and external DTDs are never fetched** — no `ExternalEntityRefHandler` is
  registered, so an XXE payload resolves to nothing. Pinned by a test, not by inspection.
- **Three algorithmic denials of service are closed** in 0.1.2, all of them shapes where a small
  file bought hours or gigabytes and no `Limits` value could reach the cost, because it was
  spent inside one call before the first block existed. PDF de-hyphenation was quadratic in the
  page (a 3 MB text layer, ~150 s for one page; now 0.070 s at 32,000 markers). `html.parser`
  rescans an unfinished construct on every block fed to it, so an unterminated tag was quadratic
  (16 MB: 9.48 s, now 0.35 s) — the unread tail is reported as `unterminated_markup`. HTML
  per-tag state was unbounded and amplified input 26–75x (6.7 MB of `<table>`: 540 MB, now
  85 MB), with anything past 65,536 open elements reported as `nesting_untracked`.
- **`csv.field_size_limit` is a process global, and diceo no longer holds it raised.** A caller
  who ran `diceo.chunk()` on a CSV had their own field-size guard removed for the length of the
  read, and concurrent readers left it raised permanently. The ceiling now rises only for a row
  about to need it and falls before that row is yielded; an ordinary file never writes it at all.
  One window is deliberately not claimed: while one thread is inside such a row, another thread
  can observe the raised value. Closing that would mean serialising all CSV parsing.
- **We call into PDFium** (C++, via `pypdfium2`). A memory-safety bug there is upstream's, but
  reachable through us, so report it here too and we will coordinate. PDFium is not
  thread-safe; diceo serialises all PDFium calls behind a mutex, and removing that mutex
  would be a security regression.

## Out of scope

- **Resource use proportional to a legitimately large document.** A 404 MB, 2,409-page PDF
  takes time, and peaks at **502 MB** of RAM with the default 100-page reopen window — against
  1,163 MB with the window switched off, because PDFium caches every indirect object it parses
  for the document's lifetime. A 208 MB, 1,500-page one peaks at 173 MB against 1,395 MB
  (experiment 015 (bounded memory at scale)). Peak RSS is bounded by the window, not by the page
  count. Use `Limits(max_pages=..., max_rows=..., max_chars=...)`
  to bound work on untrusted input; every limit that bites is reported.
- **Content-level trust.** diceo extracts text. It does not sanitise it. If you render
  extracted text as HTML, escape it — a document can contain anything.
- **Encrypted documents.** We report `EncryptedDocument` and do not attempt to break, guess, or
  work around protection.
- **The `dev` extra.** Test tooling and fixture generation, never a runtime dependency, and not
  covered by this policy. The AGPL comparison baselines are not an extra here at all — they are
  installed only by the research repository, so no install of diceo can reach them.

## Supply chain

Two runtime dependencies, both permissively licensed, no system binaries and
no network access at runtime. `just license-check` builds a clean environment and fails if a
GPL/AGPL/SSPL package appears transitively. diceo never downloads a model, phones home, or
reads configuration from the environment.
