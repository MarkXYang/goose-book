# Style Guide & Authoring Conventions

This guide keeps tone, terminology, diagrams, and code examples consistent across all chapters of *Programming in Goose*.

## 1. Voice & Pedagogy

- **Mental model first:** Before stating a rule, show *where the bytes live*. A rule like "grow-shrink references stay in variables" makes immediate sense once the reader pictures a bump pointer rewinding.
- **Direct, concrete prose:** Write in second person ("you") and present tense. Prefer short paragraphs, concrete numbers (e.g., "29 bytes, 0 allocations"), and honest trade-offs.
- **Modern idiomatic Goose:** Always teach `root struct`, `slot_pool<T>`, `slice_pool<T>`, `defer`, `guard`, `const for`/`const if`, index types, `checkfree fn`, `extern "lib"`, and `export fn`. Only mention deprecated `reusable` / `reusable[]` in brief migration notes.
- **Use official terminology:** Match the glossary in `goose/docs/reference.md`:
  - *data stack* (not "arena" unless comparing with C++/Rust)
  - *size classes*: **fixed**, **variable**, **resizable** (*grow-only* / *grow-shrink*), **root**
  - *flat* type (contains no references, slices, or relative references at any depth, and is not a root struct)
  - *fixed mode* (`Shape`) vs. *variable mode* (`Shape..`)
  - *relative reference* (self-relative `T&<u32>` vs. pool-relative `T&<u32 in pool>`)
  - *root* and *candidate* (for inferred lifetime bounds)
  - *function value* (not "closure" or "lambda", since Goose function values are compile-time entities)
  - *case function* (tag-dispatched overload set over enum variants)

## 2. Standard Chapter Structure

Each chapter should generally follow this rhythm:

1. **Opening hook & Learning Goals:** 2–4 concise bullets stating what the reader will be able to do.
2. **Core Concept(s):** Incremental sections building from intuition → memory layout → syntax & rules → gotchas.
3. **Worked Example:** A realistic, complete, runnable program applying the chapter's concepts together.
4. **Exercises:** 2–3 focused hands-on prompts (from small tweak to mini design challenge).
5. **Key Takeaways:** Short bullet recap before moving on.

## 3. Code Listing Conventions

Every Goose code block uses the `goose` language tag:

````markdown
```goose
fn main() {
    print("hello, goose");
}
```

```
hello, goose
```
````

### Expected Output Blocks

Place a bare ```` ``` ```` block immediately after a complete `goose` program (one containing `fn main()`) to show its output. `scripts/check_examples.py --run` runs the program and verifies that stdout matches line for line.

### Verification Markers

Place an HTML comment on the line immediately preceding the opening ```` ```goose ```` fence when a listing needs special handling:

| Marker | Meaning |
|---|---|
| *(none)* | Checked with `goose check` (as-is, with `import std;`, or wrapped in `fn main()`). Complete programs with `fn main()` are also executed under `--run`. |
| `<!-- fragment -->` | Syntax sketch or partial excerpt not meant to compile on its own. Use sparingly. |
| `<!-- check-only -->` | Verified with `goose check`, never executed (e.g., interactive I/O, GPU windows, or infinite loops). |
| `<!-- build -->` | Verified with `goose check`; under `--run --build`, compiled natively with `goose build` and executed (required for `thread_fn` examples, since TinyCC cannot place TLS). |
| `<!-- compile-fail -->` | Must fail to compile in every wrapper form. A live (non-comment) line containing `// error` also marks a block as compile-fail automatically. |

### Listing Quality Rules

- **Zero compiler warnings:** `scripts/check_examples.py` treats any compiler warning as a failure.
- **Formatting:** Format listings with `goose format` conventions (4-space indentation, aligned declarations where natural).
- **Self-contained whenever practical:** Prefer complete runnable snippets with `fn main()` over fragments.

## 4. Diagrams & Callouts

- **Byte-layout diagrams:** Use clean ASCII box diagrams inside ` ```text ` blocks for inline layouts (so they render crisply in every terminal, browser, and dark/light theme), and SVG under `assets/diagrams/` for richer architectural visuals.
- **Callouts:** Use blockquotes with bold labels consistently:
  - `> **Memory Model:** ...` — explains where bytes live or how stacks move.
  - `> **Coming from C++ / Rust:** ...` — brief cross-language contrast.
  - `> **Compiler Tip:** ...` — explains a diagnostic or CLI flag (`--stacks`, `--bce-lines`, `--stats`).
  - `> **Watch Out:** ...` — highlights a subtle constraint or pitfall.
