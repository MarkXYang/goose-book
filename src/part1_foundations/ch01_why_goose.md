# Chapter 1: Why Goose?

Every systems programming language makes a bet about dynamic memory. C and C++ hand you `malloc` and `free` (or `new` and `delete`)—fast in theory, yet fragile in practice and quietly expensive once allocations fragment and destructors walk deep object graphs. Go, Java, and C# add a garbage collector—safe and convenient, at the cost of pause pressure, tracing overhead, and higher memory consumption. Rust eliminates the garbage collector while preserving safety, yet it still relies on a general-purpose heap allocator and asks you to navigate ownership moves, exclusivity rules, and lifetime annotations whenever data structures grow interconnected.

Goose makes a different bet: **what if there were no heap at all?**

In Goose, there is no `malloc`, no garbage collector, no reference counting, no destructors, and no lifetime annotations—yet the language is **memory safe** (no out-of-bounds reads, no dangling references, no type confusion, and no `unsafe` escape hatch). Even more surprisingly, removing the heap does not slow programs down; across Goose's benchmark suite, it routinely makes them faster and smaller than both C++ and safe Rust.

By the end of this chapter, you will be able to:

- Explain the core idea behind Goose's data-stack memory model and why it eliminates heap overhead.
- Install or verify the `goose` compiler and inspect its toolchain.
- Write, run, typecheck, format, and natively build your first Goose programs.
- Recognize the signature features—stable references across growth, inline nested data, and scope-exit cleanup—that set Goose apart.

---

## 1.1 The Hidden Tax of the Heap

Consider one of the most ordinary tasks in programming: building a list of five strings, `"word0"` through `"word4"`.

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

On the surface, this looks like a routine loop in any modern language. Underneath, however, the memory layout is completely different from C++ or Rust.

In C++ (`std::vector<std::string>`) or Rust (`Vec<String>`), a list of strings is typically a web of heap allocations and pointers: one heap buffer for the vector's array of string headers (pointer, length, capacity per element), plus separate heap allocations for every string that exceeds small-string optimization. When the vector grows past its capacity, it allocates a bigger buffer, moves every header, and frees the old buffer—invalidating pointers into it. When the vector leaves scope, destructors walk every element to free each string's buffer before freeing the outer buffer.

In Goose, `words` is **one contiguous block of bytes** with zero pointers:

```text
C++ / Rust (Vec<String> — pointer indirection & heap allocations):

  stack frame          heap (vector buffer)               heap (strings)
  +----------+        +--------------------+             +-------------+
  | ptr  ----|------->| ptr | len | cap    |------------>| w o r d 0   |
  | len = 5  |        +--------------------+             +-------------+
  | cap = 8  |        | ptr | len | cap    |-------+     +-------------+
  +----------+        +--------------------+       +---->| w o r d 1   |
                      | ...                |             +-------------+

Goose (u8[][>..] — one flat, contiguous block on a data stack):

  frame header         data stack (bump-allocated, zero pointers)
  +----------+        +------+---------+------+---------+------+---------+
  | len = 5  |        | 05   | w o r d | 05   | w o r d | 05   | w o r d | ...
  | base ----|------->|      | 0       |      | 1       |      | 2       |
  +----------+        +------+---------+------+---------+------+---------+
                                                                         ^
                                                                   bump pointer
```

Notice what happens at every stage of this variable's life:

1. **Construction:** Pushing each string simply writes its length and bytes directly at the bump pointer—no allocator lookup, no free-list locking, no capacity check, and no temporary string copied into the array.
2. **Reading:** Iterating over `words` streams linearly through CPU cache lines with zero pointer chasing.
3. **Destruction:** When `main` returns (or any enclosing `{ ... }` block ends), freeing `words` is a **single store** that rewinds the data stack's bump pointer. Whether `words` holds five strings or five million, teardown takes the same constant time: one instruction.

> **Memory Model:** Goose organizes memory into three regions: the native call stack (for fixed-size values and headers), static read-only data (for string literals and compile-time constants), and **N data stacks**—large virtual address-space reservations whose count `N` is computed statically by the compiler. Virtual pages are committed by the OS only as you touch them.

### Four Pillars of Goose

Every major design choice in Goose flows from four interlocking properties:

1. **Data stacks replace the heap.** At most one resizable value is live on any given data stack at a time, and it always sits at the top. Growth is a raw pointer bump; memory never relocates.
2. **Scope exit is the only deallocation.** Every dynamic value is owned by a variable (local or global). When that variable's scope ends, its stack rewinds in O(1) time.
3. **Nested data stays inline.** A string, an array of strings, or a struct containing variable-size fields is stored as one flat run of bytes—not a graph of pointers.
4. **Safe references with zero annotations.** Because memory on a data stack never moves, references into a growing array remain valid for the life of that array. The compiler infers what variable *roots* every reference and rejects any reference that could outlive its owner—with no `'a` lifetime syntax and no borrow-checker exclusivity rules.

> **Why It Matters in Practice:** Across Goose's 16-benchmark suite (measured whole-process, including startup and teardown), these layout and allocation properties put Goose at roughly **3.5x the speed of idiomatic C++**, **1.25x the speed of hand-tuned expert C++**, and **1.13–1.19x the speed of the best safe Rust**, while using **1.95x, 1.32x, and 1.25x less peak memory** respectively. We will examine both the wins and the losses in detail in Chapter 19.

---

## 1.2 Installing and Verifying Goose

Goose ships as a single executable, `goose`, that bundles the compiler, an in-process JIT runner (TinyCC), a native build driver, a package fetcher, a code formatter, and a Language Server Protocol (LSP) server.

### Installing a Prebuilt Release

Download an archive for Linux (x64), macOS (Apple Silicon or Intel), or Windows (x64) from the [Goose releases page](https://github.com/aardappel/goose/releases), unpack it, and add its directory to your `PATH`. On macOS you can also install via Homebrew:

```bash
brew install goose-lang
```

On Linux, running programs in-process needs your system's C library headers (`libc6-dev` on Debian/Ubuntu, or `glibc-devel` on Fedora/RHEL). Building native executables with `goose build` additionally uses a C compiler (`gcc` or `clang` on Linux/macOS; Visual Studio 2022+ or `clang` on Windows).

### Building from Source

If you prefer to build Goose yourself, you need CMake 3.27+, a C++20 compiler, and Git:

```bash
git clone https://github.com/aardappel/goose
cd goose
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGOOSE_BUILTIN_LIBS=all
cmake --build build --config Release
```

The `-DGOOSE_BUILTIN_LIBS=all` flag compiles the graphics (`gfx`), sound (`audio`), widget (`ui`), and rigid-body physics (`physics`) packages directly into the `goose` binary.

### Checking Your Toolchain

Run `goose version` and `goose toolchain` to verify that everything is wired up:

```bash
goose version
goose toolchain
```

You should see output similar to:

```text
goose v2026.4-64-ga5ab89f3 (runtime 41e336e5d1ee)
C compiler:   gcc 15.2.0, found on PATH
              /usr/bin/cc
C++ compiler: /usr/bin/g++
style:        cc
cmake:        /usr/bin/cmake
git:          /usr/bin/git
built in:     physics, gfx, audio, ui
GOOSE_HOME:   /home/you/.goose
```

`GOOSE_HOME` (defaulting to `~/.goose`) is where Goose caches compiled runtime objects, fetched packages, and build artifacts.

---

## 1.3 Your First Goose Program

Create a file named `hello.goose`:

```goose
fn main() {
    print("hello, goose");
}
```

```
hello, goose
```

Run it immediately—with no build step—by passing the file directly to `goose`:

```bash
goose hello.goose
```

A few things are already worth noting in this three-line program:

- `fn main()` is the entry point of a root file. It takes no parameters (command-line arguments live in the `os` module) and returns nothing.
- `print(...)` is a builtin, not a printf-style wrapper. It accepts any number of arguments of any type, converts each to text, appends a newline, and emits the entire line in a single atomic `write` call (so output from concurrent threads never interleaves).

Every Goose value—scalars, booleans, arrays, structs, enums—knows how to print itself out of the box:

```goose
struct Point { x: f64, y: f64 }

fn main() {
    let answer = 42;
    let ratio = 0.75;
    let nums: i64[3] = [10, 20, 30];
    let origin = Point { x: 1.5, y: -2.0 };
    print("answer=", answer, " ratio=", ratio, " ok=", answer > 0);
    print("nums=", nums, " origin=", origin);
}
```

```
answer=42 ratio=0.75 ok=true
nums=[10, 20, 30] origin=Point { 1.5, -2.0 }
```

No format specifiers, no `#[derive(Debug)]`, and no `operator<<` boilerplate are required just to inspect your data. (In Chapter 4 you will learn how to define custom `format` overloads when you want tailored output.)

---

## 1.4 The Goose Developer Loop

As you work through this book, four commands form your daily workflow:

| Command | What It Does | When to Use It |
|---|---|---|
| `goose file.goose` *(or `goose run file.goose`)* | Compiles to C and runs immediately in-process via TinyCC | Fast edit-run iteration while learning and prototyping |
| `goose build file.goose` | Compiles to a native executable beside the source via your C compiler (`-O2` / `/O2`) | Benchmarking, shipping binaries, and programs using threads |
| `goose check file.goose` | Parses, typechecks, and checks lifetimes and bounds without running | Fast verification, inspecting `--stacks` or `--bce-lines` |
| `goose format file.goose` | Rewrites source files in canonical Goose formatting | Keeping code clean and consistent (use `--check` in CI) |

### In-Process Run vs. Native Build

Goose compiles every program to a single C file. When you run `goose hello.goose`, Goose hands that C directly to its embedded TinyCC backend in memory—there is no linker invocation and no executable written to disk.

When you run `goose build hello.goose`, Goose invokes your machine's optimizing C compiler (`gcc`, `clang`, or MSVC) to produce a fast native binary (`./hello` or `hello.exe`):

```bash
goose build hello.goose && ./hello
```

Keep two differences in mind between `goose run` and `goose build`:

1. **Optimization:** TinyCC does not optimize generated C. Always use `goose build` before measuring performance.
2. **Threads:** TinyCC cannot place thread-local storage for worker data stacks, so programs that spawn threads (`thread_fn`) must be built with `goose build`.

> **Compiler Tip:** Add `--debug` to `goose run`, `goose build`, or `goose check` to enable runtime checks for integer overflow, narrowing `as` casts, and enum tags, each reporting its exact source line. Add `--stacks` to `goose check` to see how many data stacks the compiler assigned to your program.

### Editor Support

Because the compiler itself implements the Language Server Protocol, running `goose lsp` gives any LSP-capable editor live diagnostics, hover types, go-to-definition, completion, rename, inlay hints, and format-on-save:

- **VS Code:** Install the bundled extension from the Goose repository or release archive (`code --install-extension vscode/goose-language.vsix`).
- **Neovim, Helix, Emacs:** Point your editor's built-in LSP client (`vim.lsp.config`, `languages.toml`, or `eglot`) at `goose lsp`. Full snippets are included in `docs/editors.md` in the Goose repository.

---

## 1.5 A First Taste of What Makes Goose Different

Before diving into syntax and rules in Chapter 2, let us run one compact program that showcases three ideas you will see again and again in Goose:

1. **References that survive array growth** (no reallocation invalidation).
2. **Inline nested records** (strings and child arrays packed with zero pointers).
3. **Deterministic scope-exit cleanup** (scratch memory reclaimed in one store).

```goose
struct Item  { sku: u8[varint], qty: varint, cents: varint }
struct Order { id: varint, customer: u8[varint], items: Item[varint] }

struct Node { id: i32, weight: f32 }

fn main() {
    // 1. References survive growth on a grow-only array (`T[>..]`).
    var nodes: Node[>..] = [];
    let first .= nodes.push(Node { id: 1, weight: 0.5 });
    for i: i32 in 2..1001 {
        nodes.push(Node { id: i, weight: 0.0 });
    }
    first.weight = 99.5;
    print("nodes=", nodes.len, " first=", nodes[0]);

    // 2. Nested variable-size records pack inline with zero pointers.
    var book: Order[>..] = [];
    book.push(Order {
        id: 1001,
        customer: "alice",
        items: [
            Item { sku: "SKU-441", qty: 2, cents: 1999 },
            Item { sku: "SKU-7", qty: 1, cents: 500 },
        ],
    });
    print("orders=", book.len, " packed bytes=", bytes_of(book).len);

    // 3. Scratch data lives only until the closing brace.
    {
        var scratch: u8[>..] = [];
        for i in 10000 {
            scratch.push((i % 256) as! u8);
        }
        print("scratch bytes=", scratch.len);
    } // `scratch` is freed here by a single bump-pointer store.
}
```

```
nodes=1000 first=Node { 1, 99.5 }
orders=1 packed bytes=29
scratch bytes=10000
```

Pause and look closely at those three lines of output:

- **`nodes=1000 first=Node { 1, 99.5 }`:** `let first .= nodes.push(...)` bound a direct typed reference to element 0. Pushing 999 more elements never moved element 0, so mutating `first.weight` afterwards is completely safe and checked statically. (In C++, pushing without `reserve` would silently dangle `first`; in safe Rust, the borrow checker would forbid holding `first` across `push`.)
- **`orders=1 packed bytes=29`:** An entire order—an ID, the customer name `"alice"`, and a nested list of two line items with their own SKU strings—occupies **29 contiguous bytes and 0 heap allocations**. The equivalent C++ `std::string` + `std::vector<Item>` takes roughly 160 bytes and heap allocations; Rust's `String` + `Vec<Item>` takes roughly 153 bytes across four allocations.
- **`scratch bytes=10000`:** Inside the `{ ... }` block, `scratch` grows by bumping a stack pointer. At the closing `}`, its entire 10,000-byte buffer vanishes in one instruction.

> **Watch Out:** Do not worry if tokens like `[>..]`, `.=`, `varint`, or `as!` look unfamiliar right now. Chapters 2 through 6 will unpack every single one of them step by step.

---

## 1.6 What Goose Gives Up (An Honest Preview)

No memory model is free. Understanding Goose's trade-offs on day one prevents frustration later:

- **You think about ownership scope explicitly.** Most data belongs to the function that constructs it, which requires zero ceremony. When objects have overlapping, non-nested lifetimes (such as graph nodes deleted in arbitrary order or open editor documents), you place them in a standard-library `slot_pool<T>` or `slice_pool<T>` (Chapter 8).
- **Recursive functions cannot hold growable locals across recursive calls.** Scratch buffers must end before the recursive call, and persistent containers are owned outside the recursion and passed in (Chapter 11).
- **Compact variable-size arrays are sequential.** Packing `Order` into 29 bytes means elements have differing byte sizes, so you can iterate `book` in order, but you cannot jump to `book[42]` in O(1) without keeping a separate index of references beside it (Chapters 4 and 12).
- **Closed-world compilation.** Goose compiles whole programs at once—there are no escaping closures, no function pointers, and no dynamic dispatch beyond enum tags (Chapters 7 and 9). That closed world is precisely what enables static stack assignment, inferred lifetimes, and zero-overhead higher-order functions.

Every one of these constraints buys you something concrete: predictable latency, compact cache-friendly layouts, zero allocator overhead, and memory safety without annotation fatigue.

---

## Exercises

1. **Inspect your compiler's stack assignment:** Save the worked example from Section 1.5 as `taste.goose`, then run:
   ```bash
   goose check --stacks taste.goose
   ```
   Observe how many data stacks `main` requires (`nodes`, `book`, `scratch`, plus a print scratch buffer). Next, add a second `{ var scratch2: u8[>..] = []; ... }` block *after* the first `scratch` block closes and run `goose check --stacks taste.goose` again. Notice that the total number of data stacks does not increase—sequential scopes reuse stacks automatically.
2. **Test the in-process vs. native build:** Run `goose taste.goose`, then build it natively with `goose build taste.goose && ./taste`. Try `goose build --dry-run taste.goose` to inspect the exact C compiler command Goose generates under the hood.
3. **Explore `print` and formatting:** Add a second `Order` to `book` in `taste.goose` with three items, print `book` directly with `print(book)`, and check how `bytes_of(book).len` changes. Run `goose format taste.goose` to see Goose's canonical formatting.

---

## Key Takeaways

- Goose is a memory-safe systems language built on **statically assigned data stacks** rather than a heap allocator or garbage collector.
- Memory on a data stack grows by bumping a pointer, never relocates, and is freed in O(1) time when the owning scope exits.
- Nested strings, structs, and arrays pack **inline** as contiguous bytes with no pointers, cutting both allocation overhead and cache misses.
- One executable (`goose`) handles instant in-process execution (`goose run`), optimized native compilation (`goose build`), static verification (`goose check`), formatting (`goose format`), and editor integration (`goose lsp`).

In **Chapter 2**, we will walk briskly through Goose's syntax, exact-width scalars, expression-oriented control flow, `guard`, `defer`, and multiple return values so you have all the everyday building blocks at your fingertips.
