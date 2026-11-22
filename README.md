# Programming in Goose

*Memory-Safe Systems Programming Without a Heap*

A comprehensive book for the [Goose programming language](https://github.com/aardappel/goose), built with [mdBook](https://rust-lang.github.io/mdBook/).

- **Status:** Complete — Preface, 23 Chapters across 6 Parts, and Appendices A–D in **English** & **简体中文** (143 verified code blocks per edition, 0 warnings)
- **Online Book:** [English](https://markxyang.github.io/goose-book/) · [简体中文](https://markxyang.github.io/goose-book/zh/)
- **Outline & Writing Plan:** [OUTLINE.md](OUTLINE.md)
- **Authoring & Code Conventions:** [style-guide.md](style-guide.md)
- **Tested Goose Version:** `goose v2026.4-64-ga5ab89f3-dirty` (runtime `41e336e5d1ee`)

## Reading & Running Examples From Anywhere

Every `goose` code block in the book supports **▶ Run**, **Stacks**, **Edit**, and **↺ Reset** through a **hybrid playground**:

1. **Instant Everywhere (Zero Server Needed):** All verified book outputs (`run`, `build`, `check`, and `--stacks`) are pre-computed into `theme/goose-precomputed.js` (`python3 scripts/build_precomputed.py`). Opening the book on GitHub Pages, any static host, or even offline `file://` lets readers click **▶ Run** and **Stacks** on every listing immediately.
2. **Live Execution of Edited Code Anywhere (Remote Runner):** Deploy the sandboxed Docker runner under [playground-server/](playground-server/README.md) to Fly.io, Google Cloud Run, Render, or any Docker host, then set the GitHub Actions repository variable `GOOSE_PLAYGROUND_URL` (or edit `theme/goose-config.js`). The included `.github/workflows/pages.yml` deploys the static book to GitHub Pages automatically. Readers can also connect any runner URL live via `?runner=https://...` or the inline **Connect live runner** button.
3. **Local Interactive Server (Offline Editing):** For local authoring and live auto-rebuilds:

```bash
python3 scripts/serve_playground.py --open
```

To regenerate the static verified output snapshot after editing chapters:

```bash
python3 scripts/build_precomputed.py
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
