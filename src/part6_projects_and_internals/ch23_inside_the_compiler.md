# Chapter 23: Under the Hood — How the Goose Compiler Works

Throughout this book you have worked with Goose's distinctive rules: references that survive `push` without lifetime annotations, recursive functions that cannot keep growable locals across calls, `checkfree fn` contracts, `return … from` unwinding, and flat packed layouts that double as wire formats. None of those rules are arbitrary—every one falls directly out of a compact, transparent whole-program compiler architecture.

In this final chapter, we open the hood of the Goose compiler (`src/` and `docs/implementation.md`). By the end of this chapter, you will have a clear mental map of:

- All seven phases of the whole-program compilation pipeline, from parsing to TinyCC JIT and native C builds.
- How monomorphization, root provenance, and static data-stack assignment prove memory safety at compile time without a borrow checker or garbage collector.
- How bounds-check elimination, view hoisting, and `checkfree` verification cooperate with C code generation.
- The exact C99 memory representations emitted for packed structs, arrays, ADTs, relative references, and `return … from`.
- Where the current compiler is intentionally conservative and what active design proposals are on Goose's roadmap.

---

## 1. The Whole-Program Compilation Pipeline

Goose compiles an entire program starting from one root `.goose` file and its transitive `import` graph in a single fast C++ binary. Every compiler phase runs in sequence over one shared in-memory `Ast`:

```text
  .goose sources (+ imports)
            │
            ▼
  ┌───────────────────┐
  │ 1. Parse          │  lexer.h, parser.h -> Ast (nodes, namespaces, globals)
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 2. Resolve        │  resolve.h -> resolves type names, expands type aliases
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 3. Typecheck      │  typecheck*.h -> monomorphizes FnSpecs, infers roots &
  │    & Specialize   │  lifetimes, checks shrinks/recursion, compiles embed_shader
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 4. Optimize       │  optimize*.h -> inlining, base-case inlining, tail-rec
  │                   │  elimination (TRE), loop shaping (stripk/sumred/countdown)
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 5. BCE            │  bce.h -> proves index/slice/pop bounds (nobc), hoists
  │                   │  loop views (hoistrefs/hoistfields), verifies checkfree
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 6. C99 Codegen    │  codegen*.h -> static stack counts, packed C types,
  │                   │  SIMD versions, cached stack tops + src/runtime/
  └────┬─────────┬────┘
       │         │
       ▼         ▼
  ┌─────────┐  ┌────────────────────────────────────────────┐
  │ 7a. JIT │  │ 7b. Native Build (goose build / -o out.c)  │
  │ libtcc  │  │ C compiler (clang / gcc / MSVC) + cached   │
  │ in-proc │  │ runtime object (GS_RUNTIME_VERSION hashed) │
  └─────────┘  └────────────────────────────────────────────┘
```

Four architectural choices explain why Goose compiles so quickly:

1. **Arena-style AST with zero destructors:** Just like Goose programs themselves, the compiler stores every AST node, type expression, `VarDef`, and specialization in append-only tables owned by `Ast`. Function bodies are cloned per specialization (`FnSpec`), with every clone pointing back to its source node (`Node::origin`) for diagnostics and LSP queries.
2. **Dedicated 64 MB compile thread:** Deeply nested programs and recursive specialization paths run on a dedicated compiler thread with a 64 MB virtual stack (committed only as deep as needed), while JIT execution returns to the main thread so GUI frameworks like macOS/SDL3 can create windows safely.
3. **Split runtime object for fast native builds:** `goose build` compiles the heavy platform half of the runtime (`runtime_impl.h`, `runtime_threads.h`, `runtime_os.h`—including `windows.h` / POSIX headers, printing, OS calls, and region management) **once** into a cached object file tagged with a `GS_RUNTIME_VERSION` content hash. Your program's generated C includes only the lightweight inline header (`runtime.h` + `runtime_ext.h`), cutting native C compile times significantly. Under TinyCC JIT or `--standalone`, both halves are emitted together as one self-contained C unit.
4. **The compiler *is* the language server (`goose lsp`):** `goose lsp` runs the exact same parse → resolve → typecheck → optimize → BCE pipeline over open editor buffers, adding *exemplar checking* for generic functions that nothing calls yet so hover, completion, and go-to-definition work inside library code too.

---

## 2. Provenance, Roots, and Static Data-Stack Assignment

How does Goose guarantee that no reference ever dangles—even across `push`, helper functions, and nested scopes—without asking you to write `'a` lifetime annotations or forbidding aliasing?

### Monomorphization + Root Provenance

When Goose typechecks a call to `fn f(x: Node&, a: Node[>..]&)`, it does not check `f` once in isolation against abstract lifetime parameters. Instead, it specializes `f` (creating an `FnSpec`) keyed by both the **concrete argument types** and the **root provenance** of every reference/slice argument:

- **Exact root:** The compiler knows the *specific storage* a reference points into (for example, local variable `nodes`, global `pool`, or parameter class `P0`). Exact roots enable `index_of`, self-relative link stores (`a.next .= b`), and register stack-top caching.
- **Inexact root:** When two branches merge references from different arrays (`if cond { a[0] } else { b[0] }`), or when a reference is loaded out of a general field, Goose falls back to an **outlives bound** ("this reference points into storage at scope depth $\le d$"). It can still be read and mutated safely—it simply cannot claim identity with one specific array.
- **The store record:** Whenever a function stores a reference or slice into a struct field or array element, Goose records that root provenance against the destination container. When a caller later attempts to shrink an array (`pop`, `clear`, `resize`) or return a reference past its owning scope, the checker consults the store record and rejects any operation that could leave a live view dangling.

> **Why no exclusivity (`&mut`) rules?** Because a `T[>..]`'s backing virtual memory **never moves** on `push`, aliasing two references into the same grow-only array cannot invalidate either reference! Goose therefore permits arbitrary aliasing freely and only restricts operations that actually invalidate addresses (shrinking, scope exit, and construction-in-progress).

### Static Data-Stack Assignment (`BoundStacks`)

Every grow-only (`T[>..]`), grow-shrink (`T[>..<]`), or variable-size local array needs a backing data stack. Rather than allocating stacks dynamically at runtime, Goose assigns every function's stacks statically at compile time:

1. Within a function, **sequential non-overlapping blocks reuse the same stack index** `gs_sp + k`. Only simultaneously live resizable arrays need distinct stack indices.
2. When function `A` calls function `B`, `B`'s stacks start at `gs_sp + live_in_A`, directly above the stacks `A` currently holds.
3. Because a call into a **recursive cycle** is forbidden while any data stack is live (`NoStackAcrossCycleCall`), the call graph of stack depths is acyclic!
4. Therefore, `BoundStacks` computes the exact worst-case number of simultaneously active data stacks for `main` and for every `thread_fn` worker at compile time. Exceeding `GS_MAX_STACKS` (1024 by default) is a **compile-time error**—never a runtime surprise.

At thread startup, the runtime reserves that exact count of virtual address regions (`GS_STACK_RESERVE` = 2 GB each by default, plus an unmapped `GS_STACK_GAP` guard page region behind each). Physical RAM is committed lazily by the OS only as pages are written, and `hardware_threads()` automatically caps itself so the main program plus workers stay comfortably inside `GS_STACK_BUDGET` (32 TB of virtual address space). Calling `trim_memory()` (from `os`) asks the OS (`MADV_DONTNEED` on Linux, `MAP_FIXED` replacement on macOS, `MEM_DECOMMIT` on Windows) to reclaim unused pages above every stack top while keeping all live pointers valid.

---

## 3. Inside BCE, View Hoisting, and `checkfree`

After optimization (inlining, constant folding, tail-recursion elimination, base-case inlining for `recursive fn`, and loop-shape tagging), Goose runs its **bounds-check elimination (BCE)** pass (`bce.h`) over every live specialization.

### The Difference-Constraint Domain

BCE tracks linear inequalities between integer variables, array lengths, and constants (`0 <= i`, `i < a.len`, `a.len == b.len`, `w <= nodes.len - 1`), propagating facts forward across branches, `assert`/`guard` conditions, `for`/`while` loops, and non-recursive call sites:

- **Monotonicity of `T[>..]`:** Pushing onto a grow-only array can only *increase* its length, so every existing `i < a.len` proof survives `a.push(...)` automatically! Shrinking (`pop`, `clear`, `resize`) invalidates length upper bounds.
- **Typed kills:** Growing or shrinking a `Node[>..]` through a reference does not invalidate length facts about an `i64[>..]` of a different type.
- **Index and bound types:** A value of type `u32 in a` immediately injects the axiom `0 <= v <= a.len - 1` (and `u32 upto a` injects `0 <= v <= a.len`) whenever read—even when loaded out of an array or struct field!
- **Checking `checkfree fn`:** After BCE marks every proven access `nobc`, the driver scans all source lines inside every `checkfree` function (plus every inlined block and transitive helper call reachable from it). Any single check left unmarked becomes a compile error explaining *exactly why* it could not be proved.

### Bonus Facts BCE Hands to Codegen

Beyond removing `GS_IDX` checks, BCE also annotates loops with five facts that unlock major C-level speedups:
1. **`ForLoop::fixedlen`:** Proves the loop body never resizes the iterated array, so Codegen loads its base and length once instead of re-reading every iteration.
2. **`hoistrefs` / `hoistfields`:** Hoists base pointers and lengths of reference parameters and struct-field arrays into local C variables so byte stores never cause pointer reloads.
3. **`Binary::specidx`:** Proves the right-hand side of `&&` or `||` is safe to evaluate without the left-hand side's guard inside straight-line loops, allowing branchless `&` / `|` auto-vectorization.
4. **`Binary::nonneg`:** Proves signed `/` or `%` operands are non-negative with a positive divisor, letting Codegen emit unsigned division and libdivide magic multiplications.
5. **`ForLoop::lenbound`:** Bounds loops over small limited arrays (`T[..8]`) so clang unrolls them completely.

---

## 4. Exact C99 Representations of Goose Values

One of Goose's greatest strengths is that its memory layout is **predictable down to the exact byte**. Every generated C file targets 64-bit little-endian hosts using `#pragma pack(1)` for aggregates so there is zero hidden alignment padding unless you explicitly write `pad` or `pad n`:

| Goose Construct | Generated C99 Representation | Key Runtime Details |
|---|---|---|
| Packed `struct S { a: u8, b: u32 }` | `#pragma pack(1)` C `struct { uint8_t a; uint32_t b; }` | Exact byte size = 5 bytes; no tail padding. Empty struct = 1 byte (`gs_empty`). |
| Explicit `pad` / `pad n` | Inserted padding bytes (`uint8_t _pad[k]`) | Bare `pad` aligns the next scalar field to its natural width (8 for refs/slices); ignored in variable layouts. |
| Fixed array `T[k]` | `struct { T e[k]; }` | Value-copyable; bounds checks against constants are compile-time / BCE elided. |
| Static limited `T[..k]` | `struct { uintN_t len; T e[k]; }` | `len` is `u8` ($k \le 255$), `u16` ($k \le 65535$), `u32`, or `u64`. |
| Runtime limited `T[..]` | `[u32 cap][u32 len][cap × T]` on a data stack | Capacity fixed at allocation; assigning preserves receiver capacity. |
| Variable array `T[]` / `T[varint]` | Inline length (`u32` or ULEB128) + contiguous elements | Stored directly inside enclosing variable-size struct or stack buffer. |
| Resizable `T[>..]` / `T[>..<]` | Header (`data*`, `len`) + dedicated data stack | Fat reference `T[>..]&` is `gs_rref { hdr*, stk* }`. |
| Fixed-mode ADT `E` | `struct { uint8_t tag; union { ... } u; }` | `u8` tag ($\le 256$ variants) or `u16`; size = tag + max variant payload. |
| Variable-mode ADT `E..` | `[u8/u16 tag][active variant payload bytes]` | Exact size of active variant only; no union padding. |
| Self-relative `T&<u32>` / `T&<u32>?` | Bare `uint32_t` (or `uint8_t` / `uint16_t`) | Stores `(target_addr - field_addr)`; optional reserves `0` for `null` and rejects zero-offset non-null stores. |
| Pool-relative `T&<u32 in pool>` | Bare `uint32_t` | Stores `(target_addr - pool_base)`; freely copyable! |
| Slice `T[:]` | `struct { T* data; int64_t len; }` | Two words; empty slices use `gs_memcpy`/`gs_memeq` safely even if `data == NULL`. |
| `return E from f` | Hidden thread-local return flag + target destination | Zero overhead on normal returns; one load + predicted-not-taken branch per propagation call frame. |

Even subtle runtime details are engineered for cross-platform determinism and speed:
- **`gs_memeq` and `gs_memcpy`** run inline without a libc call for buffers up to 256 bytes (using overlapping word loads), avoiding `memcmp` dispatch overhead on short string keys.
- **Float formatting (`gs_fmt_f64` / `gs_fmt_f32`)** implements Burger–Dybvig shortest-exact decimal digits in pure integer arithmetic rather than calling libc `printf`, guaranteeing that `0.1f` prints `"0.1"` identically on Linux, macOS, Windows, and TinyCC.

### Verifying Exact Layouts Live with `bytes_of`

You don't have to take the table above on faith—let's verify every single layout rule directly in Goose using `bytes_of`:

```goose
struct Empty {}

struct Packed {
    a: u8,
    b: u32,
}

struct Aligned {
    a: u8,
    pad,      // aligns b (u32) to a 4-byte offset (+3 bytes)
    b: u32,
}

enum Msg {
    Ping,
    Move { x: f64, y: f64, z: f64 },
}

fn main() {
    let empty_arr:   Empty[1]   = [Empty {}];
    let packed_arr:  Packed[1]  = [Packed { a: 1, b: 2 }];
    let aligned_arr: Aligned[1] = [Aligned { a: 1, b: 2 }];

    // Static limited arrays choose 1-byte len for cap <= 255, 2-byte len for cap <= 65535.
    let small_lim: u8[..10][1]  = ["hi"];
    let med_lim:   u8[..300][1] = ["hi"];

    // Fixed-mode vs variable-mode ADT size for a single Ping variant.
    let fixed_ping: Msg[1]     = [Msg.Ping];
    var var_ping:   Msg..[>..] = [];
    var_ping.push(Msg.Ping);

    print("Empty[1]:     ", bytes_of(empty_arr).len,   " byte  (C gs_empty)");
    print("Packed[1]:    ", bytes_of(packed_arr).len,  " bytes (1 + 4, no padding)");
    print("Aligned[1]:   ", bytes_of(aligned_arr).len, " bytes (1 + 3 pad + 4)");
    print("u8[..10][1]:  ", bytes_of(small_lim).len,   " bytes (1B len + 10B buf)");
    print("u8[..300][1]: ", bytes_of(med_lim).len,     " bytes (2B len + 300B buf)");
    print("Msg[1] Ping:  ", bytes_of(fixed_ping).len,  " bytes (1B tag + 24B max union)");
    print("Msg.. Ping:   ", bytes_of(var_ping).len,    " byte  (1B tag only!)");
}
```

```text
Empty[1]:     1 byte  (C gs_empty)
Packed[1]:    5 bytes (1 + 4, no padding)
Aligned[1]:   8 bytes (1 + 3 pad + 4)
u8[..10][1]:  11 bytes (1B len + 10B buf)
u8[..300][1]: 302 bytes (2B len + 300B buf)
Msg[1] Ping:  25 bytes (1B tag + 24B max union)
Msg.. Ping:   1 byte  (1B tag only!)
```

Every single byte is accounted for—no hidden headers, no mystery alignment, no runtime descriptors.

---

## 5. Conservative Boundaries and Active Research Directions

Because Goose prioritizes soundness, simplicity, and fast compilation, the current implementation is intentionally conservative in a few well-documented places (`docs/implementation.md` §10):

- **Bounds-check elimination scope:** BCE does not track arbitrary values stored inside arrays (use index types `u32 in a` instead), does not track plain `u64` variables as bases (use `i64` or an index type), and does not represent disjunctions in `while` exit conditions.
- **Long-distance `return … from` references:** A `return … from` targeting a named function currently restricts returned references to globals or static data (non-local returns out of trailing blocks map caller roots cleanly).
- **Relative references across plain parameters and queues:** Outside a recursive cycle, a plain `T&` parameter gets its own root class unless linked via `in pool`. And values containing relative references cannot yet cross `qput`/`qget` directly—serialize them via `to_bytes` / `from_bytes` first.
- **`root struct` nesting and pool migration:** `root struct` owners must currently be local or global declarations (nested owning `root struct` fields are not yet supported), while legacy `reusable` / `reusable[]` builtins remain deprecated in favor of `slot_pool<T>` and `slice_pool<T>`.

### What's Ahead in `docs/design/`

Goose continues to evolve carefully around its core principle—**fast, flat, memory-safe systems programming without lifetime annotations or a garbage collector**. Active design proposals in the repository's `docs/design/` directory include:

- **Structured parallelism (`par for`):** Lightweight child threads that borrow read-only views of parent data stacks directly without copying whole arrays across queues.
- **Coroutines / generators:** Resumable state machines integrated cleanly with Goose's static data-stack model.
- **Move semantics without destructors:** Eliminating unnecessary explicit `copy(...)` operations when transferring ownership of non-fixed values, while preserving $O(1)$ stack-reset teardown.
- **Source-level debugging (`goose dap`):** A Debug Adapter Protocol server alongside `goose lsp` so editors can step through Goose variables, slices, and packed structs natively.
- **WebAssembly target:** Compiling Goose programs to compact, zero-GC Wasm binaries for browsers and sandboxed plugins.

---

## Exercises

1. **Inspect a real specialization table:** Run `goose check --specs --stacks` on your Chapter 20 JSON parser (or the layout program in Section 4). Observe how many static data stacks `main` requires and which generic helpers were monomorphized.
2. **Read the generated C of `return … from`:** Write a tiny 3-function call chain (`a` → `b` → `c`) where `c` executes `return -1 from a`, emit C with `goose -o /tmp/retfrom.c chain.goose`, and find the thread-local flag and single branch that propagates the long-distance return through `b`.
3. **Measure `GS_STACK_STATS`:** Build any program natively with `goose -o /tmp/prog.c prog.goose` (using `--standalone`) and compile it with `cc -O2 -DGS_STACK_STATS=1 /tmp/prog.c -o /tmp/prog -lm`, then run `/tmp/prog` to see the runtime print its exact stack and virtual region usage on stderr at exit.
4. **Explore `--bce-lines` on your own code:** Run `goose check --stats --bce-lines` on a sorting or graph algorithm you wrote earlier in the book, identify any remaining kept bounds checks, and see whether a single `assert` or index type eliminates them.

---

## Key Takeaways

- **Whole-program monomorphization** powers Goose's entire design: typechecking, root provenance, lifetime verification, optimization, BCE, and static stack counting all operate on concrete specializations with zero runtime overhead.
- **Exact roots vs. inexact roots** plus the **store record** give Goose annotation-free lifetime safety without prohibiting aliasing.
- **Static data-stack counting (`BoundStacks`)** turns memory management into a compile-time constant number of lazily committed virtual regions with $O(1)$ scope-exit resets.
- **Packed `#pragma pack(1)` C99 layouts** mean every byte of a Goose struct, limited array, variable-mode enum, or relative reference is transparent, predictable, and inspectable with `bytes_of` and `goose -o`.
- Knowing the compiler's conservative boundaries—and how to inspect its work via `--stats`, `--bce-lines`, `--stacks`, `--specs`, and `-o out.c`—turns Goose from a black box into a precision systems tool.

---

You have now journeyed from your first Goose program all the way through memory stacks, relative references, verified serialization, share-nothing concurrency, check-free SIMD performance engineering, three full capstones, and the internals of the compiler itself. Use **Appendices A–D** whenever you want a quick syntax card, standard-library lookup, migration guide from C++/Rust/Go, or diagnostic decoder while building your own Goose projects.
