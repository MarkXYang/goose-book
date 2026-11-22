# *Programming in Goose* — Book Outline & Writing Plan

> **Working Title:** *Programming in Goose: Memory-Safe Systems Without a Heap*  
> **Reference Source:** [`aardappel/goose`](https://github.com/aardappel/goose) (`../goose`, Spec v4 draft)

---

## 1. Book Vision & Positioning

Goose is a memory-safe systems language with **no heap allocator, no garbage collector, no reference counting, no destructors, and no lifetime annotations**. Although its surface syntax feels familiar to C and Rust programmers, writing idiomatic Goose requires a genuine shift in how you think about data layout, ownership, and lifetimes.

The existing documentation in the `goose` repository (`tutorial.md`, `reference.md`, `goose_spec.md`, `stdlib.md`, `implementation.md`, and the 31 samples) is an excellent technical baseline. This book complements those docs by providing a **progressive, narrative-driven learning path**:

- **Mental-model first:** Teach *where the bytes live* before drowning the reader in rules. Use consistent byte-level memory diagrams throughout.
- **Idiomatic modern Goose:** Teach current best practices (`root struct`, `slot_pool<T>`, `slice_pool<T>`, `defer`, `guard`, `const for`/`const if`, index types, `checkfree fn`, `extern "lib"`, `export fn`) rather than deprecated features (`reusable` / `reusable[]` appear only in a brief migration note).
- **Show the trade-offs honestly:** Explain *why* Goose makes each choice, where it beats C++ and Rust (and by how much), and where its constraints require different program architectures.
- **Every example verified:** Every code listing is extracted and tested automatically against the `goose` compiler (`goose check`, `goose run`, and `goose build`), following the discipline of `goose`'s own `scripts/check_doc_examples.py`.

### Target Audience

1. **Primary:** Systems, game, and performance-oriented programmers with experience in C, C++, Rust, Zig, or Go who want to master Goose's zero-heap memory model and build real software with it.
2. **Secondary:** Programming language enthusiasts and compiler engineers interested in static data-stack allocation, inferred lifetime roots, variable-mode ADTs, and bounds-check elimination.

---

## 2. Book & Repository Organisation

We recommend authoring the book in **Markdown** and building it with **mdBook** (the same workflow used by *The Rust Programming Language*), with optional PDF/EPUB export via Pandoc or Typst later.

```text
goose-book/
├── README.md                  # Project overview, how to build & test the book
├── OUTLINE.md                 # This outline and writing plan
├── book.toml                  # Book builder configuration (e.g., mdBook)
├── style-guide.md             # Terminology, diagram style, code listing conventions
├── scripts/
│   └── check_examples.py      # Extracts & runs/checks every ```goose block in src/
├── assets/
│   └── diagrams/              # SVG/Mermaid memory layout & stack diagrams
├── examples/                  # Full multi-file capstone projects referenced by chapters
│   ├── ch20_json_query/
│   ├── ch21_parallel_filters/
│   └── ch22_mini_engine/
└── src/
    ├── SUMMARY.md             # Table of contents
    ├── 00_preface.md
    ├── part1_foundations/
    │   ├── ch01_why_goose.md
    │   ├── ch02_syntax_and_control_flow.md
    │   ├── ch03_where_values_live.md
    │   └── ch04_arrays_and_strings.md
    ├── part2_memory_and_types/
    │   ├── ch05_slices_refs_optionals.md
    │   ├── ch06_lifetimes_without_annotations.md
    │   ├── ch07_enums_in_two_sizes.md
    │   └── ch08_structs_roots_and_pools.md
    ├── part3_abstraction_and_data/
    │   ├── ch09_functions_generics_and_metaprogramming.md
    │   ├── ch10_errors_and_long_distance_returns.md
    │   ├── ch11_recursion_under_the_stack_model.md
    │   ├── ch12_relative_refs_and_data_structures.md
    │   └── ch13_verified_serialization.md
    ├── part4_systems_and_ecosystem/
    │   ├── ch14_share_nothing_concurrency.md
    │   ├── ch15_c_interoperability.md
    │   ├── ch16_programs_packages_and_tooling.md
    │   └── ch17_standard_library_tour.md
    ├── part5_performance/
    │   ├── ch18_index_types_and_checkfree.md
    │   └── ch19_writing_fast_goose.md
    ├── part6_projects_and_internals/
    │   ├── ch20_project_json_parser.md
    │   ├── ch21_project_parallel_pipeline.md
    │   ├── ch22_project_interactive_app.md
    │   └── ch23_inside_the_compiler.md
    └── appendices/
        ├── app_a_quick_reference.md
        ├── app_b_from_cpp_and_rust.md
        ├── app_c_compiler_diagnostics.md
        └── app_d_limitations_and_roadmap.md
```

---

## 3. Detailed Chapter Outline

### Front Matter
- **Preface:** Why a systems language without a heap makes sense today; Goose's origin story (Wouter van Oortmerssen's design & priorities: *Speed, Safety, Expressiveness*).
- **How to Read This Book:** Recommended reading tracks (fast-track systems dev vs. thorough cover-to-cover), code listing markers (`run`, `build`, `check-only`, `compile-fail`), and setting up `goose`.

---

### Part I: Foundations — Thinking Without a Heap
*Goal: Get the reader writing and running Goose immediately while building rock-solid intuition for data stacks and inline layout.*

#### Chapter 1. Why Goose?
- **Core Idea:** The hidden costs of heap allocation, pointer chasing, and teardown; Goose's answer (N statically assigned data stacks + bump pointers + scope-exit rewind).
- **Topics:**
  - Installing or building `goose`; verifying the toolchain (`goose toolchain`, `goose version`).
  - The developer loop: `goose run` (in-process TinyCC JIT) vs. `goose build` (native AOT via C) vs. `goose check`.
  - First program: `fn main()`, `print`, formatting with `goose format`, editor setup (`goose lsp`).
  - A taste of Goose: an array of strings in one contiguous block vs. `std::vector<std::string>` / `Vec<String>`.
- **Source References:** `README.md`, `docs/tutorial.md` §1–2, `docs/command_line.md`, `samples/01_tour.goose`.

#### Chapter 2. Familiar Syntax, Deliberate Choices
- **Core Idea:** Move briskly through familiar syntax while spotlighting Goose's deliberate ergonomics.
- **Topics:**
  - `let`, `var`, and `const` (binding reassignability vs. content writability), definite initialization, named constants.
  - Exact-width scalars (`i8..i64`, `u8..u64`, `f32`, `f64`, `bool`), literal adaptation, `as` vs. `as!`, Euclidean `%` vs. `rem`/`div_euclid`, debug overflow checks.
  - Expression-oriented control flow: `if`, `match`, `block { ... break value; }`, bare `{ }`, loops (`for i in n`, `for i in a..b`, `for x in xs`, `for &x in xs`, `for x, i in xs`, `while`, `loop`).
  - `guard` for flat happy paths and `defer` for deterministic scope-exit cleanup.
  - Multiple return values as a calling convention (no tuple types), trailing parameter defaults, and UFCS (`x.f(a)`).
- **Source References:** `docs/tutorial.md` §3, `docs/reference.md` §1–3, §16–17, `samples/01_tour.goose`.

#### Chapter 3. Where Values Live: Size Classes and Data Stacks
- **Core Idea:** Understand the heart of Goose's memory model so every later rule feels inevitable rather than arbitrary.
- **Topics:**
  - The four size classes: **fixed**, **variable**, **resizable** (grow-only / grow-shrink), and **root**; plus **flat** types.
  - Program memory map: native call stack, N compiler-assigned data stacks (virtual reservations committed on demand, `--stack-reserve`), static read-only data.
  - The stack invariant: strictly nested lifetimes, at most one live resizable per stack on top, pointer-bump growth without capacity checks, single-store scope-exit deallocation, and `os::trim_memory()`.
  - Inspecting stack assignments with `goose check --stacks`.
  - **The in-place construction guarantee:** returning growable arrays and building nested values directly at their final destination; explicit `copy(x)` when you truly want duplication.
- **Visuals:** Stack-frame & multi-data-stack timeline diagrams; in-place construction trace.
- **Source References:** `docs/goose_spec.md` §1, §4, `docs/tutorial.md` §4, `docs/reference.md` §4, `samples/02_memory.goose`.

#### Chapter 4. The Array Family and Strings
- **Core Idea:** Choosing the right container shape is choosing the exact memory layout.
- **Topics:**
  - The complete array family:
    - Fixed compile-time size: `T[k]` and `[v; k]`.
    - Variable frozen arrays: `T[]` and compact `T[varint]`.
    - Limited-capacity inline arrays: `T[..k]` and `T[..]`.
    - Grow-only arrays: `T[>..]` (the default workhorse).
    - Grow-shrink arrays: `T[>..<]` (stacks, queues, heaps).
  - Strings are `u8` arrays: `const u8[:]` literals, `"""` multi-line raw strings, `u8[>..]` builders, `u8[]` inline strings, `u8[..16]` small strings.
  - Text formatting: `print`, `str`, `format`, and writing custom `format(out, value)` overloads.
  - Nested inline data: structs with `varint` and `T[varint]` fields packed with zero pointers; sequential vs. indexable arrays.
- **Worked Mini-Example:** Compact inline Order Book (29 bytes, 0 allocations) with byte-by-byte diagram.
- **Source References:** `docs/tutorial.md` §5–6, §8, `docs/reference.md` §5–6, `samples/03_strings.goose`, `samples/16_records.goose`.

---

### Part II: References, Types, and Safe Memory
*Goal: Master Goose's reference safety, lifetime inference, two-mode ADTs, and patterns for non-nested lifetimes.*

#### Chapter 5. Slices, References, and Optionals
- **Core Idea:** Safe, transparent, zero-overhead indirection without `*`, `->`, or null pointer dereferences.
- **Topics:**
  - Slices `T[:]`: universal views across every array kind without copying.
  - References `T&`: transparency in value contexts, `.=` to bind/rebind vs. `=` to write target, `.==`/`.!=` identity comparison, decay rules for fixed vs. non-fixed values.
  - Optionals `T?`: zero-overhead nullable references (`null`), flow-sensitive narrowing via `if`, `while`, `guard`, and `assert`.
  - Writability and `const`: `const T&`, `const T[:]`, why read-only data (such as string literals) cannot be laundered into writable references.
- **Source References:** `docs/tutorial.md` §7, `docs/reference.md` §7, §10, §12.

#### Chapter 6. Lifetimes Without Annotations
- **Core Idea:** How the compiler proves references never dangle—without `'a` annotations or aliasing/exclusivity rules—and how to design code that works smoothly with it.
- **Topics:**
  - Roots and candidates: how the compiler infers what variable bounds every reference and specializes functions automatically.
  - Why references survive `push` on `T[>..]` (memory never moves).
  - The shrink rules:
    - When `T[>..]` *can* shrink (`pop`, `clear`, `resize`, whole-array reassignment `xs = [...]`) and how shrink-tracking works across calls.
    - `T[>..<]` trade-off: references stay in variables only, never in stored fields/elements; **balanced calls**.
    - Construction-in-progress rules (why `xs.push(f(xs))` is rejected when `f` grows `xs`).
  - How to read and resolve Goose lifetime and shrink diagnostics.
- **Source References:** `docs/reference.md` §12–13, `docs/goose_spec.md` §5, §9, `samples/02_memory.goose`.

#### Chapter 7. Enums in Two Sizes and Tag Dispatch
- **Core Idea:** Dynamic polymorphism without inheritance, vtables, or wasted memory.
- **Topics:**
  - Declaring ADTs (C-like tags, struct-like payloads, generic enums, self-referential enums).
  - **Fixed mode (`Shape`):** tag + max payload, indexable, variant-overwritable, match by value only.
  - **Variable mode (`Shape..`):** exact variant size, sequential, immutable tag, interior references (`match s { Rect &r => ... }`) for in-place mutation.
  - Exhaustive `match` (over enums and integer ranges/constants).
  - **Case functions:** overloading on variant types (`Shape.Circle`, `Shape.Rect&`) for exhaustive, jump-table virtual dispatch.
- **Worked Mini-Example:** Variable-mode expression tree & shape scene with an index of narrow links.
- **Source References:** `docs/tutorial.md` §9, `docs/reference.md` §9, `samples/05_shapes.goose`, `samples/19_vm.goose`.

#### Chapter 8. Structs, Root Structs, and Non-Nested Lifetimes
- **Core Idea:** Composing complex program state cleanly, and handling objects whose lifetimes do not nest.
- **Topics:**
  - Ordinary structs: field defaults, `..` partial literals, memberwise equality & arithmetic, explicit `pad`, the single-resizable-tail rule, generic structs.
  - **`root struct`:** grouping multiple independently stored resizable arrays/containers under one owning lifetime (local or global owners, passing by reference, naming fields like `pool.items`).
  - **Standard library pools** for non-nested lifetimes:
    - `slot_pool<T>`: `alloc_index`, `alloc_ref`, `alloc_with`, `free`, `index_of`.
    - `slice_pool<T>`: `alloc_slice`, `free_slice`, `realloc_slice` for variable-length contiguous runs.
  - Understanding type-safe reuse: why stale pool references are logic bugs rather than memory corruption, and techniques (e.g., generation counters) when you want strict handle validation.
  - *Side Note:* Legacy `reusable` / `reusable[]` syntax and migrating existing code to `slot_pool`/`slice_pool`.
- **Source References:** `docs/reference.md` §8, §14, `docs/stdlib.md` (`slot_pool`, `slice_pool`), `docs/tutorial.md` §11, `samples/26_file_tree.goose`.

---

### Part III: Abstraction, Control, and Data Structures
*Goal: Write expressive, generic, well-structured Goose libraries and pointer-free data structures.*

#### Chapter 9. Functions, Generics, and Compile-Time Expansion
- **Core Idea:** High-level abstraction that monomorphizes down to handwritten loops with zero closure or runtime reflection overhead.
- **Topics:**
  - Overloading rules, resolution ranking, and nested functions sharing enclosing locals.
  - Generics: untyped parameters (`fn twice(x)`), explicit `<T>`, literal parameters, generic `.=` for reference/value uniformity, floating-point context inference.
  - **Function values & trailing blocks:** `xs.filter() { it > 0 }`, `fold`, `sort`, non-local `return` out of blocks into the enclosing function, and why function values cannot escape.
  - **Specialization-time expansion (`const for` and `const if`):** iterating over `fields<T>`, `variants<E>`, `types<...>`, and `[c1, c2, ...]` in statements, constructors, and `match` arms (`same`, `has_default`).
- **Worked Mini-Example:** Generic struct printer/serializer or validation helper built with `const for <N, A> in fields<T>`.
- **Source References:** `docs/tutorial.md` §13, `docs/reference.md` §17–18, `docs/design/const_for.md`, `samples/06_functions.goose`.

#### Chapter 10. Error Handling and Long-Distance Returns
- **Core Idea:** Clean error propagation without `Result` boilerplate, `?` noise, or runtime unwinding tables.
- **Topics:**
  - Choosing the right error idiom:
    1. Trailing `bool` (`let v, ok = parse_int(s);`) for local two-outcome calls.
    2. Optional `T?` for search/lookup ("found a reference, or none").
    3. **`return E from f`** for deep failures across many call frames.
  - Static call-graph checking of `return … from`, zero-cost hidden discriminant implementation, in-place error construction at the target's destination, and interaction with `defer`.
  - Using the `exception` module (`exception::try` / `exception::throw`) when you prefer structured try/catch style.
  - Unrecoverable failures and diagnostics: `assert`, `abort`, `exit`, and caller-location defaults with `src_file()` / `src_line()`.
- **Source References:** `docs/tutorial.md` §12, `docs/reference.md` §20–21, `docs/stdlib.md` (`exception`), `samples/04_errors.goose`, `samples/17_calc.goose`.

#### Chapter 11. Recursion Under the Stack Model
- **Core Idea:** How to structure recursive algorithms cleanly when activations cannot own persistent data stacks.
- **Topics:**
  - `recursive fn` and why explicit signatures are required across recursive cycles.
  - The core rule: no non-fixed locals or temporaries alive across a recursive call.
  - Four idiomatic patterns:
    1. Block-scoped scratch that ends before recursing.
    2. Caller-owned pools/tables passed in by reference (or bundled in a `root struct`).
    3. Nested recursive functions inside a non-recursive driver sharing its locals (ideal for parsers).
    4. Shared grow-shrink scratch stack with `let mark = scratch.len; ... scratch.resize(mark);` (balanced calls).
  - Polymorphic recursion bounds and recursion depth safety.
- **Worked Mini-Example:** Recursive merge sort and recursive-descent expression evaluator.
- **Source References:** `docs/reference.md` §19, `samples/09_sorting.goose`, `samples/17_calc.goose`, `samples/31_mini_doom.goose` (BSP walk).

#### Chapter 12. Relative References and Pointer-Free Data Structures
- **Core Idea:** Build rich linked structures—lists, trees, graphs, ASTs—with 1-, 2-, or 4-byte links that are smaller, faster, and position-independent.
- **Topics:**
  - Self-relative references (`T&<u8>`, `T&<u16>`, `T&<u32>`, optional `T&<u32>?`) vs. pool-relative references (`T&<u32 in pool>` and `T&<u32 in root.items>`).
  - Using `self` inside struct/variant literals and `alloc_with` in `slot_pool`.
  - Non-copyability of self-relative values and in-place construction rules.
  - Classic structures in Goose:
    - Binary search tree in a single grow-only array (`12`-byte nodes).
    - Doubly-linked list in a `slot_pool` with a `self` sentinel.
    - Directory / n-ary tree combining `slot_pool` (nodes) + `slice_pool` (child lists of 4-byte `in nodes.items` links).
    - Hash tables keyed by zero-copy slices (`dictionary` + word frequency analysis).
- **Source References:** `docs/tutorial.md` §10, `docs/reference.md` §11, `docs/design/relative_references.md`, `samples/12_huffman.goose`, `samples/13_linked_list.goose`, `samples/14_bst.goose`, `samples/15_word_freq.goose`, `samples/26_file_tree.goose`.

#### Chapter 13. Zero-Copy Persistence and Verified Serialization
- **Core Idea:** When data structures are flat and self-relative, the in-memory representation *is* the file format.
- **Topics:**
  - `bytes_of(a)`: zero-copy read-only byte view of an array's element region.
  - `to_bytes(a)` / `to_bytes(a, out)`: framed byte images.
  - `from_bytes<T[>..]>(bytes)`: verifying untrusted or hostile input (framing, enum tags, canonical varints, array lengths, link alignment & bounds, index types) before bytes become live values.
  - Safety vs. integrity: why `from_bytes` prevents memory corruption while checksums/hashes protect against silent data edits.
  - What is and isn't serializable (self-relative vs. `in pool` links, fixed vs. variable-size nodes).
- **Worked Mini-Example:** Saving, loading, and tamper-testing a compact word-index tree on disk.
- **Source References:** `docs/tutorial.md` §15, `docs/reference.md` §23, `docs/design/serialization.md`, `samples/25_serialize.goose`.

---

### Part IV: Concurrency, Interop, and Ecosystem
*Goal: Scale across CPU cores safely, integrate with C libraries and hosts, and organize multi-file projects and packages.*

#### Chapter 14. Share-Nothing Concurrency
- **Core Idea:** Eliminate data races, locks, atomics, and memory ordering bugs by isolating workers completely.
- **Topics:**
  - `thread_fn`: separate compiled worker programs with private data stacks and snapshotted globals.
  - Flat types as messages: passing whole variable-size structs/buffers via a single `memcpy`.
  - Typed queues: `qput`, `qget<T>` (blocking & timed), `qpoll<T>`, `qlimit<T>` for backpressure.
  - Lifecycle & sizing: `hardware_threads()`, `thread_spawn`, `thread_wait`, shutdown sentinels.
  - Fault isolation & supervision: `thread_spawn_contained` and capturing worker abort diagnostics with `thread_wait(id, msg)`.
  - Practical notes: why threaded programs require `goose build` (not TinyCC JIT) and thread-safety boundaries with external C code.
- **Worked Mini-Example:** Multi-threaded Mandelbrot renderer / job pipeline with graceful supervision.
- **Source References:** `docs/tutorial.md` §14, `docs/reference.md` §22, `samples/23_mandelbrot_threads.goose`.

#### Chapter 15. Interoperating with C (Both Directions)
- **Core Idea:** Tap directly into the C ecosystem and embed Goose libraries inside existing C/C++ applications.
- **Topics:**
  - **Calling C (`extern fn`):** C-compatible shapes (scalars, flat packed structs, `T&` pointers, `T[:]` as `{ data, len }`, `u8[>..]&` builders via `gs_bld_append`), symbol renaming (`= "sym"`).
  - **Dynamic shared libraries (`extern "lib" { ... }`):** zero-toolchain FFI, opaque `u64` handles, `ffi::cstr`/`ffi::bytes`, matching C struct alignment with `pad`, platform library mapping via `dylibs_*` in `.build` sidecars.
  - **Bundling your own C:** `foo.build` sidecars (`c_sources`, `c_include`, `c_headers`, `c_defines`, `c_flags_*`, `c_libs_*`) and `--include`.
  - **Calling Goose from C (`export fn`):** `--standalone --header`, `goose build --shared`, `goose_init`/`goose_exit`, slice returns, hot-reloading, and threading rules.
- **Source References:** `docs/tutorial.md` §16, §19, `docs/reference.md` §24, `docs/packages.md` §3–4, `samples/24_call_c.goose`.

#### Chapter 16. Program Structure, Packages, and Tooling
- **Core Idea:** Move from single-file scripts to maintainable multi-module codebases and reusable packages.
- **Topics:**
  - Whole-program compilation from a root file; `import a.b` and relative `import .helper`; dual-purpose runnable/importable modules (`fn main` ignored on import).
  - Namespaces: `namespace foo;`, `foo::name`, `::global`, file-local aliases (`namespace g = long_name;`), UFCS and `format` lookup following types.
  - Globals and initialization order; compile-time assets with `embed_file` and `embed_shader`.
  - Packages: flat, pinned root-file dependencies (`import lib from "owner/repo@v1"` and local paths), `goose fetch`, `--offline`, blessed packages, authoring and publishing your own package.
  - Mastering the `goose` CLI: `run`, `build`, `check`, `format`, `lsp`, `toolchain`, `--diagnostics json`, `--dry-run`, emitting standalone C for external build systems.
- **Source References:** `docs/reference.md` §21, `docs/packages.md`, `docs/command_line.md`, `docs/editors.md`.

#### Chapter 17. Tour of the Standard Library
- **Core Idea:** Know what ships out of the box—all written in readable Goose—before writing your own utilities.
- **Topics:**
  - Core utilities (`std`): math/bit ops (`popcount`, `clz`, `rotl`, `isqrt`, `gcd`), hashing, `rng` (splitmix64 & Gaussian), slice searching/sorting/heaps, SIMD-accelerated `ByteSet` (`find_any`/`find_pair`), string helpers & `ascii`.
  - Collections & memory: `dictionary`, `slot_pool`, `slice_pool`.
  - Math & geometry: `vec`, `mat`, `quat`, `math`.
  - System & data formats: `os` (args, files, time, `resource_dir`, `trim_memory`), `ffi`, `binary`, `base64`, `csv`, `json`, `regex`.
  - Interactive & multimedia packages: overview of `gfx` (SDL3 GPU), `audio` (PCM mixer), `physics` (Box3D), and `ui` (Nuklear widgets & text/HUDs).
- **Source References:** `docs/stdlib.md`, `stdlib/`.

---

### Part V: Performance Engineering
*Goal: Understand exactly where Goose's speed comes from and how to engineer provably check-free, cache-friendly, vectorized code.*

#### Chapter 18. Index Types, Bounds-Check Elimination, and `checkfree`
- **Core Idea:** Move bounds checks out of hot loops and have the compiler *guarantee* zero checks remain.
- **Topics:**
  - How Goose's bounds-check elimination (BCE) works and how to inspect it (`--stats`, `--bce-lines`).
  - Guiding BCE with preconditions (`assert`), Euclidean `%`, and grow-only monotonicity.
  - **Index and bound types:** `uN in a` and `uN upto a` (including global `root struct` fields like `u32 in graph.nodes`), moving validation to graph/data construction time.
  - **`checkfree fn`:** enforcing zero remaining bounds checks as a compiler-verified contract across callers, helpers, and blocks.
- **Worked Mini-Example:** Check-free Compressed Sparse Row (CSR) graph BFS and row-slice image blur kernel.
- **Source References:** `docs/reference.md` §15, `docs/tutorial.md` §17, `docs/design/index_types.md`, `bench/summary.md`, `samples/07_sieve.goose`, `samples/22_image_filters.goose`.

#### Chapter 19. Writing Fast Goose
- **Core Idea:** Practical systems performance tuning grounded in real benchmark data.
- **Topics:**
  - Data layout engineering: inline records vs. parallel arrays, narrow relative links, variable-mode enums vs. fixed-mode tables.
  - Function tuning: `inline fn`, `simd fn` (AVX2/AVX-512 multi-versioning & FMA `mul_add`), exact-width arithmetic for auto-vectorization.
  - Inspecting generated C (`goose -o out.c`) and comparing MSVC vs. clang/GCC backend behavior.
  - Benchmarking fairly (whole-process timing including teardown, `--debug` vs. release builds).
  - Case studies from the Goose benchmark suite: why `records`, `interp`, `bintrees`, and `graph` win big—and why flat numeric kernels (`particles`) or specific patterns (`calc`, `lru`) tie or lose.
- **Source References:** `bench/summary.md`, `bench/results.md`, `bench/design.md`, `docs/implementation.md` §9.

---

### Part VI: Capstone Projects & Under the Hood
*Goal: Synthesize every concept in realistic, multi-feature systems programs, then peek inside the compiler.*

#### Chapter 20. Capstone 1: A Compact JSON Parser, Query Engine, and Binary Cache
- **Concepts Combined:** Variable-mode ADTs (`Json..`), 4-byte self-relative links, nested recursive-descent functions, `return … from` syntax errors with line/column reporting, `const for` inspection, and `to_bytes`/`from_bytes` verified binary caching.
- **Inspiration:** `samples/18_json.goose` + `samples/25_serialize.goose`.

#### Chapter 21. Capstone 2: A Multi-Threaded Image Processing Pipeline
- **Concepts Combined:** `checkfree fn` + `simd fn` row-slice convolution filters (blur, Sobel edge detection), flat row messages over `qlimit`-bounded typed queues, `thread_fn` worker pool, `thread_spawn_contained` fault recovery, and CLI argument handling via `os`.
- **Inspiration:** `samples/22_image_filters.goose` + `samples/23_mandelbrot_threads.goose`.

#### Chapter 22. Capstone 3: Building a Real-Time Interactive Application / Mini-Game
- **Concepts Combined:** `root struct` world state, `slot_pool` entity management, `defer` resource cleanup, `embed_shader` GLSL shaders, `gfx` rendering + `ui` HUD/controls + `audio`, fixed-timestep simulation, and validated atomic save files.
- **Inspiration:** `samples/27_gfx_cube.goose`, `samples/29_ui_todo.goose`, `samples/30_mini_minecraft.goose`, `samples/31_mini_doom.goose`.

#### Chapter 23. Under the Hood: How the Goose Compiler Works
- **Core Idea:** Give readers a clear architectural map of the compiler so its rules, diagnostics, and performance characteristics feel transparent.
- **Topics:**
  - Whole-program pipeline: Parse → Resolve → Monomorphizing Typecheck → Optimize → BCE → C Codegen → TinyCC JIT / Native C Build.
  - How root inference, provenance tracking, and static data-stack assignment (coloring non-overlapping lifetimes onto N stacks) work.
  - C representation of arrays, variable-mode ADTs, relative links, and `return … from`.
  - Current limitations, design trade-offs, and active research directions (coroutines, `par for`, ownership without destructors, debugging/DAP, WebAssembly).
- **Source References:** `docs/implementation.md`, `docs/goose_spec.md` §10 & Appendix B–C, `docs/design/`.

---

### Appendices
- **Appendix A. Quick Reference:** Keywords, contextual keywords, operator precedence, array-type matrix, size-class matrix, and builtin summary.
- **Appendix B. Goose for C++ and Rust Programmers:** Direct translation guide (e.g., `std::vector` / `Vec` → `T[>..]`, `std::variant` / `enum` → `E` vs `E..`, `Result`/`?` → `return … from`, RAII → data stacks + `defer`, arenas/indices → `slot_pool` + relative refs + index types).
- **Appendix C. Field Guide to Compiler Diagnostics:** Real compiler errors (lifetime root mismatch, shrink conflict, recursion with live growable, construction-in-progress, failed `checkfree`) with step-by-step fixes.
- **Appendix D. Language Status, Limitations, and Further Reading:** Explicit checklist of current v1 omissions and pointers to the specification and design notes.

---

## 4. Writing & Execution Plan

### Phase 0: Setup & Verification Harness (Milestone 1)
1. **Finalize book tooling & conventions:**
   - Choose builder (recommended: `mdBook`) and initialize `book.toml` and `src/SUMMARY.md`.
   - Write `style-guide.md` (terminology matching `docs/reference.md` Glossary, voice/tone, diagram conventions, code fence tags such as `goose`, `goose,build`, `goose,check-only`, `goose,compile-fail`).
2. **Build `scripts/check_examples.py`:**
   - Automatically extracts every code block from `src/**/*.md`, compiles/runs it against the local `goose` compiler (`../goose/build/goose` or `goose` on `PATH`), verifies expected output and expected `compile-fail` diagnostics.
   - Add a quick single-command test target so every chapter is verified as it is written.

### Phase 1: Vertical Slice — Part I (Milestone 2)
- Draft **Preface** + **Chapters 1–4** (`Why Goose?`, `Syntax`, `Where Values Live`, `Arrays & Strings`).
- Create the foundational SVG/Mermaid diagrams for data stacks, inline nested arrays, and in-place construction.
- **Review Gate:** Read Part I end-to-end as a newcomer; confirm the mental model clicks before moving to Part II.

### Phase 2: Core Memory & Type System — Part II (Milestone 3)
- Draft **Chapters 5–8** (`Slices/Refs/Optionals`, `Lifetimes`, `Enums in Two Sizes`, `Structs/Roots/Pools`).
- Build a set of intentional `compile-fail` listings in Chapters 6–8 to demonstrate exact compiler diagnostics.

### Phase 3: Expressiveness & Data Structures — Part III (Milestone 4)
- Draft **Chapters 9–13** (`Generics/Blocks/const for`, `Errors & return…from`, `Recursion`, `Relative Refs & Data Structures`, `Verified Serialization`).
- Ensure every data structure in Chapter 12 includes a byte-layout diagram and runnable test output.

### Phase 4: Systems, Concurrency & Ecosystem — Part IV (Milestone 5)
- Draft **Chapters 14–17** (`Threads`, `C Interop`, `Packages & Tooling`, `Standard Library Tour`).
- Verify `goose build` (native AOT) examples for Chapter 14 (threads) and Chapter 15 (C FFI + `export fn` + `--shared`).

### Phase 5: Performance, Capstones & Compiler — Parts V–VI & Appendices (Milestone 6)
- Draft **Chapters 18–19** (Index types, BCE/`checkfree`, performance engineering) with real `--stats` / `--bce-lines` outputs.
- Build and verify the three full capstone projects under `examples/` and write **Chapters 20–22**.
- Draft **Chapter 23** (Compiler Internals) and **Appendices A–D**.

### Phase 6: Technical Edit, Sync & Publishing (Milestone 7)
1. **Compiler Sync Pass:** Pull latest `goose`, run `scripts/check_examples.py` across the entire book and `examples/`, and update any syntax/stdlib changes.
2. **Cross-Reference & Consistency Audit:** Verify glossary consistency, forward/backward chapter links, and diagram accuracy.
3. **Editorial Polish & Export:** Tighten prose, run spell/link checks, and publish HTML (e.g., GitHub Pages) and optional PDF/EPUB builds.

---

## 5. Quality & Sync Principles

- **Zero broken examples:** No chapter is considered "done" until `scripts/check_examples.py` passes on all of its listings (including `compile-fail` checks).
- **Prefer small, complete programs:** Whenever possible, make listings self-contained and directly runnable with `goose run`.
- **Teach one hard idea per chapter:** Keep each chapter focused on one conceptual leap, reinforced by 1–2 concrete exercises at the end of each chapter.
- **Track Goose evolution:** Record the pinned `goose` commit/version tested in `README.md` so readers always know which compiler version the book targets.
