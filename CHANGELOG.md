# Changelog

Notable changes, newest first. [Keep a Changelog](https://keepachangelog.com/),
[semantic versioning](https://semver.org/).

Entries headed `docmill` predate the rename. They are kept because the code they describe is
the code this release ships.

## 0.1.0 — 2026-08-04

First release as `diceo`. `pip install diceo`, `import diceo`, `diceo report.pdf`.

**Renamed from `docmill`.** Nothing about the behaviour changed; the import name, the console
script and the distribution did. There is no compatibility shim and there will not be one — a
package that installs under one name and imports under another is worse than a rename you do
once.

Nothing has been published under the name `diceo` before this, so it starts where a first
release starts. The `docmill` entries below are the record of how this code got here — the
hostile-input audit in particular — and every fix in them is in this release.

### Changed

- `docmill` → `diceo` throughout: the distribution, the import name, the `diceo` console
  script, and the base exception `DocmillError` → `DiceoError`.
- **The public API is deliberately untouched.** `chunk`, `extract`, `Limits`, `Chunk`,
  `Diagnostics`, `Block` and `FORMATS` all keep their names. "Chunking" is the term the field
  and its search traffic use, and renaming it to suit a new package name would have cost that
  for nothing.

### Fixed

- **The README figure said `docmill.chunk()`, and had always said "Embedded chunks".** It is a
  raster, so its text is pixels: neither the rename nor the grep that verified the rename could
  see it. It is now rendered from `docs/assets/pipeline.svg` and says "Embed-ready chunks",
  which is the label that matters — diceo runs no model, needs no GPU and makes no network
  call, so a figure promising vectors sends a reader to install something this is not.

### Why the name changed

`docmill` was taken twice over in this exact field: a Go PDF-to-Markdown converter under BSL
1.1, benchmarked against docling, pymupdf4llm, markitdown and liteparse — the same comparison
set as ours — and a company selling document extraction at scale. Two tools with one name
measuring themselves against the same rivals is a permanent tax on every reader. Everything up
to and including 0.1.2 was released under the name `docmill`.

## docmill 0.1.2 — 2026-08-03

The rest of the hostile-input audit: the three findings 0.1.1 named as still open. Chunk
boundaries are unchanged, so no re-embedding.

### Security

- **PDF de-hyphenation was quadratic in the page.** Every line-ending hyphen cost a full-page
  scan, so a 3 MB text layer needed ~150 s for a single page and `Limits(max_seconds=)` could
  not interrupt it. Past 128 candidates on a page the reader now builds the page's vocabulary
  once and answers from it: 32,000 markers went from **4.44 s to 0.070 s**, and de-hyphenation
  decisions are unchanged — verified against the old implementation over 320,000 randomized
  pages and every real PDF fixture.
- **An unterminated tag made HTML parsing quadratic.** `html.parser` rescans an unfinished
  construct on every block fed to it, so `<a ` followed by megabytes cost 4x for every
  doubling — about 25 minutes at the 200 MB single-page export the reader advertises. Now
  linear, **9.48 s to 0.35 s at 16 MB**, and the unread tail is reported as
  `unterminated_markup` rather than silently dropped.
- **HTML per-tag state was unbounded**, amplifying input 26–75x: a million `<table>` tags in
  6.7 MB took **540 MB**, now **85 MB**. Past 65,536 open elements the extra nesting is no
  longer tracked and the count is reported as `nesting_untracked`. Deeply nested real pages
  are unaffected — the deepest page measured holds 36 open elements.
- **`csv.field_size_limit` no longer leaks.** It is process-global, and diceo raised it
  around the whole read: callers saw their own guard removed between chunks, and concurrent
  readers left it permanently raised. The ceiling now goes up only for a row that is about to
  need it and comes down before that row is yielded, so a file whose cells are ordinary never
  writes the global at all.

## docmill 0.1.1 — 2026-08-03

Hostile-input fixes. Upgrade if anything you feed diceo comes from outside your
own systems; chunk boundaries are unchanged, so no re-embedding.

### Security

- A document type declaration in any parsed part is refused. expat resolves no external
  entities, but it does expand internal ones and bounds them only as a *ratio* of input,
  which padding defeats: a 19.8 KB `.docx` reached 2.0 GB of memory and returned a chunk
  with no error. No office format writes a doctype, so none is now accepted.
- Element nesting and unclosed-element breadth are bounded per streamed part. Both grow
  the parser's tree without touching a byte ceiling: 100 KB of nested `w:tbl` reached
  1.2 GB, and one `<row>` of two million `<c>` reached 623 MB. Refused as
  `CorruptDocument` now, at a measured 1.6–4.3% throughput cost.
- CI and release workflows pin every action to a commit SHA and run with `contents: read`.
  The PyPI publisher was tracking a mutable branch in the job that mints the credential.

### Fixed

- `max_chars` now also bounds the packer's final flush, which used to go out whole.
- `Limits` rejects negative `overlap_chars`, `max_pages`, `list_group_size` and
  `reopen_every`, not only `max_rows` and `max_chars`.
- `diagnostics.blocks` no longer counts reader blocks during `chunk()`; it is `extract()`'s
  number and is now only kept there.
- The CLI exits 64 on a usage error, as its `--help` says, rather than argparse's 2.

## docmill 0.1.0 — 2026-08-03

First release.

- Eleven formats through one call, detected from the bytes rather than the extension.
- Diagnostics for anything left out: scanned pages, limits that bit, parts that would not open.
- Typed exceptions, all under `DiceoError`.
- Paths, `bytes` and file objects as input.
- A command line: `diceo file.pdf` writes one JSON object per chunk.

Chunk boundaries are stable within a version and not across them, until 1.0. An upgrade can
change where chunks split, so plan to re-embed rather than to diff.
