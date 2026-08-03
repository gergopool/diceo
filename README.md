# ⚡ Diceo

### Document in, chunks out.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](https://github.com/gergopool/diceo/blob/main/LICENSE)

Fast, accurate, free — pick two. The quick parsers hand your embedder a wall of flat text. The
accurate ones take days to get through a corpus. The ones that do both charge per page and want
your documents on their servers.

**Diceo is 11 MB and gives you all three.** One function call, and what comes back is chunks
shaped for retrieval rather than a wall of text.

![Retrieval quality against CPU cost on PDF, with Diceo in the top-right corner of a field of ten extractors.](https://raw.githubusercontent.com/gergopool/diceo/main/docs/assets/positioning-mean-pdf.svg)

*In-house evaluation: every tool runs its own extraction and its own chunker, each chunk is
embedded with `nvidia/Nemotron-3-Embed-1B-BF16`, and quality is recall: how often the chunk
holding the answer comes back in the top five. [The full numbers](https://github.com/gergopool/diceo/blob/main/docs/benchmarks.md) — per format, per chunk
size, and where we lose — and [the methodology](https://github.com/gergopool/diceo/blob/main/docs/methodology.md).*

- 🔥 **Nearly as fast as the fastest, nearly as good as the best** — and the only tools ahead of it
  on either axis are copyleft, a problem in most commercial products. Among the tools that are
  free for commercial use, Diceo leads on both axes at once.
- 🌊 **It streams, in bounded memory** — chunks come out while the file is still being read, and a
  404 MB, 2,409-page PDF peaks at 502 MB.
- 🪶 **11 MB installed, two dependencies, 20 ms to import.** No GPU, no model download, no network,
  no server — it runs in your own process and nothing is uploaded.
- 📚 **Eleven formats, one call, free for commercial use** — pdf, docx, xlsx, pptx, xls, xlsb, ods,
  csv, html, eml, text.

## 📄 How it works

![One document goes into diceo.chunk() and chunks stream out. There is no intermediate file.](https://raw.githubusercontent.com/gergopool/diceo/main/docs/assets/how-it-works.webp)

No intermediate markdown, no document object, no temporary file — readers are generators yielding
blocks and the chunker consumes them lazily, so chunks come out while the file is still being read.

## 📦 Install

```bash
pip install diceo
```

Both dependencies ship prebuilt wheels for Linux (glibc and musl), macOS and Windows, on x86-64
and arm64. Nothing compiles at install time.

## 🚀 Quickstart

```pycon
>>> import diceo
>>> chunk = next(diceo.chunk("report.docx"))
>>> print(chunk.embed_text)
Quarterly results
Revenue rose to 1200 million in EMEA.
Costs were flat.
>>> chunk.meta
{'index': 0, 'doc_id': 'report', 'title': 'Quarterly results',
 'kinds': ['heading', 'paragraph'], 'char_start': 0, 'char_end': 72}
```

That is the whole product: `embed_text` goes to your embedding model, `meta` to your vector store.
`chunk()` is a generator — a 1,000-page report starts yielding immediately and never exists in
memory whole. It takes a path, bytes, or any seekable binary file, and detects the format from
content:

```python
diceo.chunk("report.pdf")                          # a path
diceo.chunk(response.content, name="report.pdf")   # bytes
```

There is a CLI too:

```bash
diceo report.pdf                 # chunks as JSON lines
diceo slides.pptx --diagnostics  # what would be missing from your index
```

## 🐍 The library

Local, not a service: no server, no network call, nothing uploaded. Six names to learn, plus the
exceptions below, and `chunk` is the only one most people need.

| name | what it gives you |
|---|---|
| `chunk(source, **opts)` | the chunks, as a generator |
| `extract(source, **opts)` | the blocks, before they are packed into chunks |
| `sniff(source)` | the format it detects — `"pdf"`, `"xlsx"`, … |
| `Diagnostics()` | what got left out; hand one in, read it after |
| `Limits(...)` | the knobs — `target_chars`, `max_pages`, `max_rows` |
| `FORMATS` | the eleven names it accepts |

Every failure reading a document subclasses `DiceoError`, so one handler is the whole contract:

```python
try:
    for chunk in diceo.chunk(path):
        index(chunk)
except diceo.DiceoError as exc:
    quarantine(path, exc.reason)
```

We check that by running it: 930 real documents and 565 deliberately broken ones — truncated,
bit-flipped, zip-bombed — no crash, no hang, nothing raised that was not a `DiceoError`.

Parallelism is yours: cheap import, picklable chunks, no hidden thread pool. Use **processes, not
threads**, for PDFs — PDFium is not thread-safe. Diceo refuses zip bombs before inflating them,
but it is a parser, not a sandbox: isolate untrusted input in a worker with limits
([SECURITY.md](https://github.com/gergopool/diceo/blob/main/SECURITY.md)). Chunk boundaries are stable within a version, not across them — plan
to re-embed on upgrade until 1.0.

## 🔦 It tells you what it could not read

With most tools, a scanned PDF or an image-only deck just yields nothing — the document is silently
absent from your index. Diceo hands it back as data:

```python
report = diceo.Diagnostics()
chunks = list(diceo.chunk("scanned.pdf", diagnostics=report))

if report.needs_ocr:      # image-dominated pages that yielded almost nothing
    route_to_ocr(path)
if report.lost_data:      # a limit bit, a part was unreadable, an attachment stayed closed
    log.warning(report.as_dict())
```

**Not an OCR.** A page with no text layer is flagged, never reconstructed, and no bundled engine or
system binary is called — the output does not depend on what happens to be installed on the box.

## 🙏 Credits

The two dependencies do the heavy lifting, and both are permissively licensed — which is the only
reason a wheel this small can read these formats at all.

- **[pypdfium2](https://github.com/pypdfium2-team/pypdfium2)**, binding Google's
  **[PDFium](https://pdfium.googlesource.com/pdfium/)** — the engine Chrome renders PDFs with.
  Two thirds of the cost of reading a PDF here is PDFium's C++, not ours.
- **[python-calamine](https://github.com/dimastbk/python-calamine)**, binding Rust's
  **[calamine](https://github.com/tafia/calamine)** — what makes a million-row sheet stream a row
  at a time instead of loading whole.

Three more permissively-licensed projects were read closely enough that a design idea here traces
to them. **Diceo contains no code from any of them** — nothing copied, ported or adapted, and no
copyleft source consulted at any point. [CREDITS.md](https://github.com/gergopool/diceo/blob/main/CREDITS.md) is the longer version.

- **[Unstructured](https://github.com/Unstructured-IO/unstructured)** — deciding that a line is a
  label from its shape rather than from a list of English words.
- **[LiteParse](https://github.com/run-llama/liteparse)** — joining wrapped table header lines by
  walking back from the body, and emitting a tabular region verbatim when its columns will not
  resolve.
- **[Xberg](https://github.com/xberg-io/xberg)** — whether the *previous* line ended in
  sentence-final punctuation separates a wrapped continuation from a real heading.

Closest published support for the thesis:
[arXiv 2603.06976](https://arxiv.org/abs/2603.06976) (paragraph-group chunking nearly doubles
nDCG@5 over fixed-size) and [arXiv 2602.16974](https://arxiv.org/abs/2602.16974) (simple
structure-based chunking beats LLM-guided).

## ☕ Support

Starring the repo, opening an issue when Diceo mishandles a document, or telling someone it
exists all help. If it saves you real time:
[**buy me a coffee**](https://buymeacoffee.com/gergopool) ☕ — a donation, no tiers, no perks.

Contributions welcome: [CONTRIBUTING.md](https://github.com/gergopool/diceo/blob/main/CONTRIBUTING.md), and
[docs/principles.md](https://github.com/gergopool/diceo/blob/main/docs/principles.md) holds the five rules
that settle arguments here.
