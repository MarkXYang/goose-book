# Programming in Goose

*Memory-Safe Systems Programming Without a Heap*

A comprehensive book for the [Goose programming language](https://github.com/aardappel/goose), built with [mdBook](https://rust-lang.github.io/mdBook/).

- **Status:** Complete — Preface, 23 Chapters across 6 Parts, and Appendices A–D (143 verified code blocks, 0 warnings)
- **Outline & Writing Plan:** [OUTLINE.md](OUTLINE.md)
- **Authoring & Code Conventions:** [style-guide.md](style-guide.md)
- **Tested Goose Version:** `goose v2026.4-64-ga5ab89f3-dirty` (runtime `41e336e5d1ee`)

## Reading & Running Examples Live in the Book

Start the interactive Goose book playground (builds the book, serves it locally, auto-rebuilds on edits, and enables **▶ Run**, **Stacks**, and **Edit** buttons on every Goose code block):

```bash
python3 scripts/serve_playground.py --open
```

Or use `mdBook` directly for static preview (read-only):

```bash
# Build static HTML into book/
mdbook build

# Serve locally with live reload
mdbook serve --open
```

## Verifying Code Examples

Every `goose` code block under `src/` is verified automatically against the `goose` compiler:

```bash
# Typecheck all code listings
python3 scripts/check_examples.py

# Typecheck and run complete programs (comparing expected output blocks)
python3 scripts/check_examples.py --run

# Also compile and run native-build examples (e.g., threaded programs)
python3 scripts/check_examples.py --run --build

# Check a single chapter with verbose output
python3 scripts/check_examples.py src/part1_foundations/ch01_why_goose.md --run -v
```

Pass `--exe /path/to/goose` to test against a specific compiler build.
