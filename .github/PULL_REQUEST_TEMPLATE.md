<!-- Thanks. CONTRIBUTING.md has the five rules; this is just the checklist. -->

## What this changes, and why

## Evidence

Delete what does not apply. An empty Evidence section is fine for a docs or typo fix and is not
fine for anything else.

- [ ] **Bug fix**: includes the test that would have caught it.
- [ ] **New format reader**: added to `tests/fixtures.py` *and* to `every_format()`, so the whole
      robustness suite covers it (truncation, bit flips, determinism, path/bytes/stream agreement).
- [ ] **Performance**: measured on a quiet box, with the command and the numbers pasted below.
- [ ] **Retrieval or chunking**: measured on *every* format, not only the one it was designed
      for -- a change can be +0.8pp on DOCX and -1.8pp on PDF and still look like a win. The
      corpora and the harness live in the research repository, so **if you cannot run them,
      say so and tick this anyway**: the maintainer runs the gate and posts the table here,
      win or lose. Say what you expect it to move, so the prediction is on the record first.

```
paste measurements here
```

## Checklist

- [ ] `just check` is green: tests, lint, format, and the licence gate.
- [ ] No new default dependency, or one that is permissive, argued in an issue first, and still
      green under `just license-check` -- which reads what wheels bundle, not just what they declare.
- [ ] Nothing new is materialised: crackers stay generators, the chunker consumes them lazily.
      No `"".join(...)` over a whole document.
- [ ] Anything truncated, skipped or unparseable lands on the caller's `Diagnostics` with a count.
      A document that is silently not in the index is the worst failure this package can have.
- [ ] No new name in `diceo.__all__`, or one the README and `CHANGELOG.md` both document --
      the public surface is deliberately tiny and every addition is permanent.
