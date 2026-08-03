# Credits

## What diceo runs on

Two dependencies do the work this package could not sensibly do itself, and both are permissively
licensed, which is the only reason a wheel this small can read these formats at all.

| project | licence | what it does here |
|---|---|---|
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | Apache-2.0 / BSD-3-Clause | Python bindings to **[PDFium](https://pdfium.googlesource.com/pdfium/)** (BSD-3-Clause), the PDF engine Chrome renders with. Every PDF diceo reads is parsed, decrypted, decoded and laid out by PDFium; diceo reads characters and their boxes back out and decides what is a heading, a table row or a paragraph. |
| [python-calamine](https://github.com/dimastbk/python-calamine) | MIT | Python bindings to **[calamine](https://github.com/tafia/calamine)** (MIT), a Rust spreadsheet reader. It is what makes xlsx, xls, xlsb and ods streamable row by row instead of loaded whole. |

Both are named here because the README's "two dependencies" is a boast about *their* work as much
as ours: the measured cost of reading a PDF is about two-thirds PDFium's C++ and one-third
diceo's Python (experiment 027, in the research repository), and no amount of work on our side
changes which of those two is doing the heavy lifting.

## Where the ideas came from

diceo is built by measuring itself against other people's work. Three projects were read
closely enough that specific design ideas here can be traced to them, and they are named for that
reason. This file records where an idea came from, not when it landed; `CHANGELOG.md` tracks that.

**diceo contains no code from any of these projects.** Nothing was copied, ported, translated or
adapted. What was taken is a design idea, described in prose in the experiment record
(`047-what-the-field-does-better`, in the research repository) and then implemented from scratch.
No GPL-, AGPL- or SSPL-licensed source was consulted at any point — that is a hard rule here, and
the reason this package exists rather than a wrapper around an AGPL converter.

| project | licence | credited for |
|---|---|---|
| [Unstructured](https://github.com/Unstructured-IO/unstructured) | Apache-2.0 | For `is_possible_title` — deciding that a line is a label from its word count, terminal punctuation and alphabetic ratio rather than from a list of English words, which is why its table chunks begin at the caption on documents in languages it has never been taught. |
| [LiteParse](https://github.com/run-llama/liteparse) | Apache-2.0 | For `absorb_header_lines` — walking backwards from a table's body and joining each wrapped header line into its column, so several visual header lines become one header row — and for emitting a tabular region verbatim in a fenced block when its columns will not resolve, instead of a pipe table with the wrong column count. |
| [Xberg](https://github.com/xberg-io/xberg) (formerly Kreuzberg) | MIT | For `demote_continuation_headings` — the observation that whether the *previous* line ended in sentence-final punctuation is what tells a wrapped continuation line from a real heading; diceo uses the same signal in the opposite direction, to rejoin a heading's severed tail. |

Several other ideas from the same reading were tried and rejected on measurement rather than on
principle. That record is in the research repository, not here.

diceo is Apache-2.0. See `LICENSE`.
