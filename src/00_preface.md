# Preface

Welcome to *Programming in Goose* — a practical guide to writing fast, memory-safe systems software without a heap allocator, garbage collector, or lifetime annotations.

Goose looks familiar at first glance, borrowing braces, `let`, `fn`, `struct`, and `match` from C and Rust. Underneath, however, it takes a radically different approach to memory: every dynamic value lives inline on compiler-managed **data stacks**, growth is a pointer bump, and deallocation is a single pointer store at scope exit.

Here is a taste of Goose — five strings built inline inside one contiguous array, with zero heap allocations:

```goose
fn main() {
    var words: u8[][>..] = [];
    for i in 5 {
        words.push(str("word", i));
    }
    print(words);
}
```

```
["word0", "word1", "word2", "word3", "word4"]
```

## How This Book Is Organized

- **Part I (Chapters 1–4):** Core syntax, size classes, data stacks, arrays, and inline strings.
- **Part II (Chapters 5–8):** Slices, references, optionals, inferred lifetimes, two-mode enums, `root struct`, and stdlib pools (`slot_pool` / `slice_pool`).
- **Part III (Chapters 9–13):** Generics, static function values, `const for`, `return … from` errors, recursion, relative references, and verified serialization.
- **Part IV (Chapters 14–17):** Share-nothing threads, bidirectional C interop, packages and tooling, and a tour of the standard library.
- **Part V (Chapters 18–19):** Index types, bounds-check elimination, `checkfree fn`, `simd fn`, and performance engineering.
- **Part VI (Chapters 20–23):** Three end-to-end capstone projects and an architectural tour inside the Goose compiler.
- **Appendices A–D:** Quick reference, C++/Rust translation guide, compiler diagnostics field guide, and current limitations.

## Conventions & Verification

Every Goose code listing in this book is checked automatically against the Goose compiler (`scripts/check_examples.py --run`). Output blocks placed immediately after a complete program show the exact output produced by running it.
