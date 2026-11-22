# Chapter 19: Writing Fast Goose

In Chapter 18 you saw how Goose's bounds-check elimination, index types, and `checkfree fn` remove safety overhead from hot loops without sacrificing memory safety. Removing bounds checks is one pillar of high performance, but it is rarely the largest one. On modern hardware, CPU cores routinely stall waiting for cache lines from memory, allocator metadata, or branch mispredictions long before an integer comparison becomes the bottleneck.

This chapter is a practical guide to performance engineering in Goose. Every recommendation is grounded in how the compiler actually generates C and in measurements from Goose's own 16-benchmark suite against C++ and Rust.

By the end of this chapter, you will be able to:

- Choose data layouts—inline variable-mode ADTs, packed fixed structs, exact-width integers, and the right link representation—that minimize bytes touched per operation.
- Structure loops so Goose hoists array views, caches stack tops in registers, eliminates division via magic multiplication, and lets the C backend auto-vectorize.
- Use `inline fn`, `simd fn`, and `mul_add` (from `math`) to unlock AVX2/AVX-512 multi-versioning and fused multiply-add while keeping floating-point results reproducible.
- Avoid hidden copies by cooperating with guaranteed construct-in-destination and named-return optimization.
- Inspect generated C, understand backend differences (TinyCC JIT, clang, GCC, MSVC), and benchmark honestly—knowing both where Goose wins decisively and where it ties or loses.

---

## 1. Data Layout Engineering: Shrink the Bytes Touched

Most of Goose's performance advantages come from a single overarching principle: **touch fewer bytes per unit of work, in contiguous memory, with zero allocator traffic**. When your working set shrinks by 2× to 4×, structures that spilled into L3 cache or main RAM in a pointer-rich language fit comfortably in L1 or L2 cache in Goose.

### Variable-Mode ADTs vs. Fixed-Mode Enums

In C++ and Rust, an enum (`std::variant` or `enum`) is always as large as its **largest variant** plus alignment padding. If 80% of your AST nodes or log records are 4-byte integers and 5% are 48-byte structs, every single element pays the 48-byte tax.

In Goose, a **variable-mode enum** (`Record..`) stores each element at the exact size of its active variant—including inline variable-length strings—in one contiguous buffer:

```text
Fixed-mode array (Record[>..]):
┌────────────────────────────┬────────────────────────────┬────────────────────────────┐
│ tag │ Small (4B) │ 40B pad │ tag │ Small (4B) │ 40B pad │ tag │ Big (44B)            │
└────────────────────────────┴────────────────────────────┴────────────────────────────┘

Variable-mode array (Record..[>..]):
┌──────────────┬──────────────┬────────────────────────────┐
│ tag│Small(4B)│ tag│Small(4B)│ tag│Big(44B)               │
└──────────────┴──────────────┴────────────────────────────┘
```

Let's measure the exact memory difference right inside Goose using `bytes_of`:

```goose
enum Event {
    Tick { seq: u32 },
    Sample { seq: u32, x: f32, y: f32 },
    Note { seq: u32, text: u8[..48] },
}

enum EventVar {
    Tick { seq: u32 },
    Sample { seq: u32, x: f32, y: f32 },
    Note { seq: u32, text: u8[] },
}

fn main() {
    var fixed: Event[>..] = [];
    var packed: EventVar..[>..] = [];

    for i in 1000 {
        let s = i as u32;
        if i % 20 == 0 {
            fixed.push(Event.Note { seq: s, text: "sensor recalibrated" });
            packed.push(EventVar.Note { seq: s, text: "sensor recalibrated" });
        } else if i % 4 == 0 {
            fixed.push(Event.Sample { seq: s, x: 1.5, y: 2.5 });
            packed.push(EventVar.Sample { seq: s, x: 1.5, y: 2.5 });
        } else {
            fixed.push(Event.Tick { seq: s });
            packed.push(EventVar.Tick { seq: s });
        }
    }

    let fixed_bytes = bytes_of(fixed).len;
    let packed_bytes = bytes_of(packed).len;
    let ratio_x10 = (fixed_bytes * 10) / packed_bytes;

    print("fixed bytes:  ", fixed_bytes);
    print("packed bytes: ", packed_bytes);
    print("compression:  ", ratio_x10 / 10, ".", ratio_x10 % 10, "x");
}
```

```text
fixed bytes:  54000
packed bytes: 7750
compression:  6.9x
```

The fixed-mode array consumes **54,000 bytes** because every `Tick` reserves space for a 48-byte `Note` buffer (plus the tag, `seq`, and length header). The variable-mode array holds the exact same 1,000 events—with full inline strings, zero heap allocations, and zero pointer chasing—in **7,750 bytes**, a **6.9×** reduction.

> **Trade-off:** Variable-mode arrays are sequential (you cannot index `packed[500]` directly in $O(1)$ without an auxiliary index or relative links) and their tags are immutable after push. Use fixed-mode arrays when you need random access or variant mutation in place; use variable-mode arrays when you build once and scan or walk via relative links.

### Exact-Width Integers vs. `varint`

Goose's `varint` type encodes small integers in 1 byte and large ones in up to 10 bytes, which is fantastic for serialization and cold fields with wide, unpredictable ranges. However, **every read of a `varint` field runs a ULEB128 decode loop**. (Array length prefixes use a one-byte fast path, whereas `varint` value fields always use the decode loop.)

In Goose's own `calc` benchmark, storing small numbers as `varint` instead of `u8` made the hot evaluator **11% slower**. Use `varint` when space dominates or the range is truly unbounded; use exact-width `u8`, `u16`, `u32`, or `i32` whenever you know the range in advance.

### Choosing Between Self-Relative Links, Pool-Relative Links, and Index Types

Chapter 12 introduced self-relative (`T&<u32>`) and pool-relative (`T&<u32 in pool>`) references, and Chapter 18 introduced index types (`u32 in pool`). From a performance perspective, they are **not** interchangeable:

| Link Form | Size | Build-Once / Walk-Many | Relink-Heavy (e.g., LRU) | Copyable / Serializable |
|---|---|---|---|---|
| `T&<u32>?` (self-relative, optional) | 4 B | Fast (zero base register, position-independent) | ~1.5× slower than indices (subtract/add on the pointer-chasing path) | Non-copyable; directly `to_bytes`/`from_bytes` serializable |
| `T&<u32>` (self-relative, non-optional) | 4 B | Faster (no null test; ~1.10× faster under MSVC) | Same offset math, no null branch | Non-copyable; directly serializable |
| `T&<u32 in pool>` (pool-relative) | 4 B | Fast (`base + off` scheduled well by clang) | Faster than self-relative for global pools | Freely copyable; not directly `to_bytes`-serializable |
| `u32 in pool` (typed index) | 4 B | Fast and `checkfree`-verifiable | Fastest for mutation/relinking | Freely copyable and serializable |

Two practical rules follow from this table:

1. **Build-once, walk-many trees and ASTs** (parsers, scene trees, Huffman trees, binary caches): prefer **self-relative references** (`T&<u32>` / `T&<u32>?`)—and use a non-optional link initialized with `self` as a sentinel when you want to avoid null tests on every hop.
2. **Relink-heavy mutable structures** (LRU caches, intrusive worklists, dynamic graphs): prefer **`u32 in pool` index types** or **`in pool` relative links** so relinking doesn't put address subtraction on the critical dependency chain.

---

## 2. Writing Loops the Compiler Can Optimize

Even when every bounds check is eliminated, the C compiler can only generate top-speed machine code if it can keep pointers, lengths, and stack tops in CPU registers and prove that iterations don't interfere with one another. Goose performs several targeted optimizations before handing C to the backend.

### Loop-Invariant Views (4× Speedup on Stencils)

An array passed by reference (`src: u8[>..]&`) stores its data pointer and length in memory behind a header pointer. In C, any byte write (`dst[i] = ...`) might theoretically alias that header, forcing the C compiler to reload `src`'s base pointer and length on every iteration.

Goose's optimizer solves this with **view hoisting**: whenever a loop only reads and writes *elements* of a reference parameter (no `push`, `pop`, `resize`, `clear`, whole-array assignment, rebind, or call that could resize it), Goose loads the base pointer and length into local C variables once before the loop. In Goose's `blur` benchmark, view hoisting alone delivered a **4× speedup** under clang.

> **Practical rule:** Keep element-processing loops free of growth operations. If you need to grow an output buffer, either pre-`resize` it before the loop (so the loop only writes elements) or build into a separate array whose growth is isolated from the read-only source views.

### Stack-Top and Length Caching

When a function grows an array in a tight loop (`out.push(...)`), updating the stack top and element count in memory on every push can stall subsequent reads and block optimizations. Goose caches the stack top and length in C local registers automatically—*provided* no other reference in the function could be a second name (an alias) for that same stack.

A helper that pushes through reference parameters (`fn emit(a: u8[>..]&, b: u32[>..]&)`) gets full register caching when:

1. Every fat reference used in the body is a parameter (or an immutable `let` alias of one)—not a fat reference read out of a struct field or array element.
2. All reference parameters have distinct, exact roots at every call site.
3. The function does not return a non-fixed value, capture a resizable, or touch a global of the same pointee type.

When you write a hot producer (like a serializer, lexer, or mesh builder), **pass distinct output arrays directly as parameters** rather than bundling fat references inside a temporary helper struct.

### Automatic Loop Restatements and Magic Divisors

You often don't need to manually unroll loops or hand-code bit tricks in Goose—the compiler recognizes several idiomatic loop shapes and rewrites them into forms clang optimizes aggressively:

- **Loop-invariant divisor hoisting:** Inside a loop, integer `/` or `%` by an unmodified variable (`x / d`, `x % m`) is automatically replaced ahead of the loop with a libdivide magic multiplication and shift. For `u32` (32-bit or narrower) operands, Goose generates a 32-bit magic multiplier that **auto-vectorizes**—yielding up to 4×–7× speedups over scalar division.
- **Lane strip-mining (`i % K`):** A loop indexing a small fixed array by `i % K` (`for x, i in data { lanes[i % 8] ^= x; }`, for $K \in [2, 16]$) is strip-mined into blocks of $K$ with constant indices so `lanes` stays entirely in registers.
- **In-order float reductions (`s += term(x)`):** A simple loop accumulating floats into `s` evaluates 8 terms at a time in a vectorizable inner chunk, then adds them to `s` in exact original order—giving SIMD throughput for `term(x)` with bit-identical IEEE rounding.
- **Bounded small-array unrolling:** Iterating over a small limited array like `cell.neighbors` of type `T[..8]` automatically emits a bounded trip-count loop that clang unrolls completely.
- **Countdowns:** `while i > 0 { i--; ... }` is restated as an up-counting `for` loop, which C backends schedule better.
- **Straight-line `&&` / `||`:** Inside a straight-line inner loop (no calls, `break`, `continue`, or nested loops), a pure, bounds-proven `a[j] == x && b[j] == y` compiles as bitwise `&` without a short-circuit branch so the backend can vectorize it.

### Scanning Bytes at 16 Bytes per Cycle

A `while` loop checking one byte at a time with an early exit (`if buf[i] == '\n' { break; }`) runs at roughly 1 byte per cycle because no C compiler vectorizes a loop with a data-dependent early exit.

Goose offers two much faster idioms:

1. **Use `std`'s `ByteSet` with `find_any` / `find_pair`:** As introduced in Chapter 17, `find_any` scans 16 bytes at a time using SSE2/SSSE3 on x86-64 and NEON on AArch64. Build the `ByteSet` *once* outside the loop.
2. **Count over fixed-size blocks:** In a straight-line `for j in 32` loop over a 32-byte slice, counting matches (`if a[j] == x && b[j] == y { hits += 1; }`) uses Goose's branchless `&&` speculation and vectorizes; if `hits > 0`, scan those 32 bytes to locate the exact index.

Here is both techniques side by side in a runnable program:

```goose
import std;

checkfree fn count_block_matches(a: const u8[:], b: const u8[:], x: u8, y: u8) -> i64 {
    assert(a.len == b.len);
    let n = a.len;
    var total = 0;
    var base = 0;

    // Straight-line 32-byte chunks: bounds-proven and branchless &&.
    while base + 32 <= n {
        let sa = a[base..base + 32];
        let sb = b[base..base + 32];
        var hits = 0;
        for j in 32 {
            if sa[j] == x && sb[j] == y {
                hits += 1;
            }
        }
        total += hits;
        base += 32;
    }

    while base < n {
        if a[base] == x && b[base] == y {
            total += 1;
        }
        base += 1;
    }
    return total;
}

fn count_delimiters(text: const u8[:]) -> i64 {
    // Prepare the SIMD ByteSet once outside the loop.
    let delims = byte_set(",;\n");
    var pos = 0;
    var count = 0;
    while pos < text.len {
        let off = find_any(text[pos..], delims);
        if off < 0 { break; }
        count += 1;
        pos += off + 1;
    }
    return count;
}

fn main() {
    let csv = "id,name,score;1,ada,99\n2,grace,100;3,linus,95\n";
    print("delimiters: ", count_delimiters(csv));

    var p1: u8[>..] = [];
    var p2: u8[>..] = [];
    for i in 80 {
        p1.push((i % 5) as u8);
        p2.push((i % 3) as u8);
    }
    // i % 5 == 2 and i % 3 == 1 => i % 15 == 7 -> 7, 22, 37, 52, 67 (5 hits)
    print("pair matches: ", count_block_matches(p1, p2, 2, 1));
}
```

```text
delimiters: 12
pair matches: 5
```

---

## 3. Function Tuning: `inline fn`, `simd fn`, and `mul_add`

When data layout and loop structure are clean, Goose gives you two targeted function modifiers to guide code generation: `inline fn` and `simd fn`.

### `inline fn`: Guaranteed Inlining at Every Call

At `-O1` and `-O2`, Goose automatically inlines small functions (up to 8 AST nodes at `-O1`, 16 at `-O2`) and single-use helpers. Prefixing a function with `inline` forces Goose to inline it at **every** call site (except under `-O0`, where inlining is disabled for debugging):

```goose
inline fn clamp_u8(v: i32) -> u8 {
    if v < 0 { return 0; }
    if v > 255 { return 255; }
    return v as u8;
}
```

Modifier rules:
- `inline` cannot be combined with `extern`, `export`, `simd`, `recursive`, or `thread_fn`.
- It **can** be combined with `checkfree`: `checkfree inline fn ...`.
- The modifier order is fixed: `export|extern` → `checkfree` → `simd` → `inline` → `recursive` → `fn|thread_fn`.

### `simd fn`: Runtime-Dispatched AVX2 and AVX-512

By default, compiled C targets the platform baseline (SSE2 on x86-64, NEON on AArch64) so binaries run everywhere. That baseline leaves 2× to 4× of throughput on the table for arithmetic-heavy byte and float loops on modern x86-64 CPUs.

Marking a hot loop function with `simd fn` instructs Goose to emit **three versions** of that function when compiled with clang on x86-64:

1. **Level 2 (`AVX-512`):** AVX-512F, BW, CD, DQ, VL (`x86-64-v4`)
2. **Level 1 (`AVX2`):** AVX2, FMA, BMI1, BMI2, LZCNT, POPCNT
3. **Baseline:** Portable SSE2 fallback

On the first call, a lightweight runtime probe (`cpuid` + `xgetbv`, cached atomically) picks the highest version supported by both the CPU and OS, so subsequent calls pay only a single well-predicted branch. On GCC, MSVC, ARM64, and TinyCC, `simd` emits one ordinary function with zero dispatch overhead.

Why is `simd` an explicit annotation instead of automatic on every loop? Because Goose's benchmark experiments showed that blindly multi-versioning every function triples binary size for only 1–4% average gain (within noise), and some loops actually get *slower* under AVX-512 (for instance, gather-heavy table lookups or memory-bound loops). Put `simd fn` specifically on **loop-bearing kernels** that meet three conditions:

1. **Vectorizable loop body:** no early exits (`break` on invalid data—accumulate a bitmask flag instead), zero bounds checks (`checkfree`), and no stack-top syncs inside the loop.
2. **Arithmetic rather than table lookups:** range checks via wrapping subtraction (`if c - 'a' < 26 { ... }`) vectorize into SIMD compares and blends, whereas lookup tables become expensive gathers. In `stdlib/base64.goose`, an arithmetic `simd fn` codec runs **5.8× faster** at AVX-512 than at SSE2.
3. **The `simd fn` owns the loop:** do not put `simd` on a tiny per-element helper called from a non-`simd` loop; put `simd` on the outer loop function and `inline` on any helper it calls.

### Deterministic Floats and Explicit FMA (`mul_add`)

Many C compilers enable floating-point contraction (`-ffp-contract=on`) by default, silently fusing `a * b + c` into a single-rounding FMA instruction when targeting AVX2/AVX-512 or ARM64. That breaks reproducibility: the same program would print different digits on AVX2 versus SSE2!

Goose deliberately disables implicit contraction across the entire generated C unit (`#pragma clang fp contract(off)`), guaranteeing that a `simd fn`'s AVX2 and AVX-512 versions produce **bit-identical** IEEE-754 results to the baseline.

When you *want* FMA throughput—and accept the single-rounding precision improvement—import `math` and call `mul_add(a, b, c)`:

- Inside a `simd fn`'s AVX2/AVX-512 versions (and on ARM64), `mul_add` compiles directly to a hardware FMA instruction.
- Inside an `inline fn` helper called by a `simd fn`, Goose inlines the helper into each SIMD version, so its `mul_add` fuses too!
- On targets without hardware FMA (or under MSVC, where C's `fma()` is a slow library call), it falls back cleanly to `a * b + c`.

> **Elementwise Struct Math:** Goose also lowers elementwise `+`, `-`, `*`, `/`, and unary `-` on packed structs or fixed arrays consisting solely of 2, 4, 8, or 16 floats of one width (16, 32, or 64 bytes total, such as `struct Vec4 { x: f32, y: f32, z: f32, w: f32 }`) directly into GCC/clang vector operations (`GS_VEC`), giving 1.25×–1.6× speedups on FFT butterflies without changing the struct layout.

### Writing Vectorizable Float Loops: Row-Order Matrix Multiply

Because compilers cannot reassociate floating-point additions, a classic `i-j-k` dot-product loop (`sum += a[i, k] * b[k, j]`) has a loop-carried dependency on `sum` and struggles to vectorize. Reordering the loops to `i-k-j` (`c_row[j] += aik * b_row[j]`) performs the **exact same additions in the exact same order** for every `c[i, j]`, yet turns the inner loop into a clean contiguous vector multiply-add!

Let's verify both versions produce identical results while making the fast kernel `checkfree simd fn`:

```goose
import std;
import math;

const N = 16;

checkfree inline fn fma_step(acc: f64, a: f64, b: f64) -> f64 {
    return mul_add(a, b, acc);
}

// Scalar reference: i-j-k dot product
checkfree fn matmul_ref(a: const f64[:], b: const f64[:], c: f64[:]) {
    assert(a.len == N * N);
    assert(b.len == N * N);
    assert(c.len == N * N);

    for i in N {
        let a_row = a[i * N..i * N + N];
        let c_row = c[i * N..i * N + N];
        for j in N {
            var acc = 0.0;
            for k in N {
                acc += a_row[k] * b[k * N + j];
            }
            c_row[j] = acc;
        }
    }
}

// Vectorizable i-k-j row-order kernel: checkfree + simd + mul_add
checkfree simd fn matmul_simd(a: const f64[:], b: const f64[:], c: f64[:]) {
    assert(a.len == N * N);
    assert(b.len == N * N);
    assert(c.len == N * N);

    for idx in c.len {
        c[idx] = 0.0;
    }

    for i in N {
        let a_row = a[i * N..i * N + N];
        let c_row = c[i * N..i * N + N];
        for k in N {
            let aik = a_row[k];
            let b_row = b[k * N..k * N + N];
            for j in N {
                c_row[j] = fma_step(c_row[j], aik, b_row[j]);
            }
        }
    }
}

fn main() {
    var a: f64[>..] = [];
    var b: f64[>..] = [];
    var c_ref: f64[>..] = [];
    var c_fast: f64[>..] = [];

    for idx in N * N {
        a.push((idx % 7) * 0.25);
        b.push(((idx * 3) % 5) * 0.5);
        c_ref.push(0.0);
        c_fast.push(0.0);
    }

    matmul_ref(a, b, c_ref);
    matmul_simd(a, b, c_fast);

    var max_diff = 0.0;
    var sum = 0.0;
    for idx in N * N {
        let d = abs(c_ref[idx] - c_fast[idx]);
        if d > max_diff { max_diff = d; }
        sum += c_fast[idx];
    }

    print("n=", N, " checksum=", sum, " max_diff=", max_diff);
}
```

```text
n=16 checksum=3038.5 max_diff=0.0
```

Notice how clean `matmul_simd` is:
- `c_row`, `a_row`, and `b_row` are row slices whose lengths are proven equal to `n` once per row, so the inner `for j in n` loop has **zero bounds checks** (enforced by `checkfree`).
- `simd fn` emits AVX2+FMA and AVX-512+FMA versions under clang on x86-64.
- `fma_step` is an `inline fn`, so its `mul_add` is inlined directly into each SIMD version and compiles to hardware `vfmadd231pd` instructions! In Goose's own measurements, switching a matrix multiply from `i-j-k` to row-order `i-k-j` dropped runtime from 2.67 s to 0.91 s at SSE2 and **0.49 s at AVX2** (a 5.4× total speedup).

---

## 4. Avoiding Hidden Copies

Goose has value semantics without implicit move constructors: copying a large array is an explicit `copy(x)` of $O(\text{size})$ cost, while **fresh results are constructed directly at their destination**. Keeping your code on the zero-copy path takes just three habits:

1. **Return one named local per non-fixed function:**
   When every path in a function returns the same local (`return out;`), Goose builds `out` directly on the caller's receiving stack (guaranteed NRVO). If one branch returns `local_a` and another returns `local_b`, or if you return a slice `return out[0..n]`, Goose must copy the result.
   *(Tip: returning an empty literal `return [];` on an error branch alongside `return out;` is still cheap, because moving zero bytes costs nothing—only returning a second non-empty local forces a real copy.)*
2. **Prefer `v.append(make_items())` and `v.push(make_item())`:**
   For functions returning `T[]`, `v.append(f())` compiles a specialized element-run twin of `f` that writes elements directly onto the tail of `v` without allocating a temporary array.
3. **Beware `u64` loop variables:**
   As Chapter 18 noted, plain `u64` variables drop out of bounds-check proofs. Keep loop counters and computed indices in `i64` (or an index type `u32 in a`). Compare `u64` hashes directly against `a.len` or reduce them with `%` / `&` before casting to `i64`.

---

## 5. Inspecting Generated C, Backends, and Fair Benchmarking

Because Goose compiles to readable, self-contained C99, you never have to guess what the compiler emitted.

### Diagnostic & Inspection Workflow

```bash
# 1. Check how many bounds checks remain and on which lines:
goose check --stats --bce-lines kernel.goose

# 2. Check static data-stack allocation and specialization counts:
goose check --stacks --specs kernel.goose

# 3. Emit the generated C to inspect loops, views, SIMD dispatch, or assembly:
goose -o /tmp/kernel.c kernel.goose
clang -O2 -S -o /tmp/kernel.s /tmp/kernel.c   # inspect assembly if needed
```

Inside `/tmp/kernel.c`, look for:
- `/* loop shape: ... */` comments showing where Goose applied strip-mining, in-order sum reduction, bounded unrolling, or countdown restatement.
- `GS_IDX(...)` calls—any remaining bounds checks stand out immediately.
- `<fn>_simd1` and `<fn>_simd2`—your AVX2 and AVX-512 versions from `simd fn`.

### Choosing the Right Backend

The generated C is identical across backends, but C compilers differ markedly in how well they optimize it:

| Backend | Intended Role | Strengths & Caveats |
|---|---|---|
| **TinyCC (JIT, `goose run`)** | Instant edit–run feedback | Compiles in milliseconds, zero external toolchain. **Never benchmark with `goose run`**—TinyCC does virtually no optimization or vectorization. |
| **clang (`goose build`)** | Best overall release performance | Vectorizes checked loops, schedules base+offset loads well, supports `simd fn` (AVX2/AVX-512), `GS_VEC`, and errno-free `__builtin_elementwise_sqrt`. |
| **GCC (`goose build`)** | Strong Linux release performance | Strong auto-vectorization, supports `GS_VEC` and hoisted divisor checks; does not currently emit `simd fn` multi-versions. |
| **MSVC (`goose build`)** | Windows native builds | Fast on recursive bump-allocation workloads; **sensitive to retained bounds checks** (a single check can inhibit vectorization) and slower on flat 2D stencils (`blur` flat: 952 ms vs. row-slice: 298 ms). Prefer row-slice kernels if targeting MSVC. |

### Benchmarking Fairly

When you measure Goose against C++, Rust, or an earlier version of your own program, avoid the four classic benchmarking traps:

1. **Always build release binaries (`goose build`), never `--debug` or `goose run`.** Debug builds enable signed overflow checks, range-checked casts, and disable optimizations.
2. **Time the whole lifecycle, including teardown.** Tearing down a million-node `Box` or `std::unique_ptr` tree in Rust/C++ requires walking and freeing every node—the same cache-hostile traversal as building it. Goose releases an entire data stack with a single pointer reset. Measuring only "build" while letting the OS reclaim memory at process exit hides a real cost paid by every long-running server or game frame.
3. **Compare matching interfaces.** Comparing Goose's owned inline strings (`u8[][>..]`) against C++ `std::vector<std::string_view>` or Rust `Vec<&str>` compares two different lifetime contracts. Compare owned-to-owned and borrowed-to-borrowed.
4. **Report peak RSS (committed pages), not virtual address space.** Goose reserves 2 GB of virtual address space per data stack, but the OS only commits physical pages as they are touched. Measure committed RSS alongside logical payload size—and watch the gap shrink in Goose.

---

## 6. Honest Lessons from the Goose Benchmark Suite

Goose maintains a 16-benchmark suite (`bench/`) comparing idiomatic Goose against idiomatic C++, expert C++ (custom arenas, pre-sized vectors, manual packing), and idiomatic safe Rust (including `Vec`-plus-index arenas where idiomatic). Every benchmark runs at three sizes and verifies identical checksums.

On the October 2026 large-size suite (geometric mean across all 16 benchmarks):

| Comparison Baseline | Speed (MSVC / clang) | Peak Memory Advantage |
|---|---|---|
| vs. Idiomatic C++ | **3.51× / 3.54× faster** | **1.95× less memory** |
| vs. Expert C++ | **1.24× / 1.26× faster** | **1.32× less memory** |
| vs. Best Safe Rust | **1.13× / 1.19× faster** | **1.25× less memory** |

More useful than the headline geometric mean is seeing *which* workloads win big and *which* tie or lose—because that tells you where Goose's design pays off and where to spend your optimization effort:

| Benchmark | What It Tests | Result vs. Best Safe Rust / C++ | Why? |
|---|---|---|---|
| **`records`** | Heterogeneous records with inline text | **2.02–2.26× faster** than Rust on **3.98× less memory** | Variable-mode enum stores each record at its exact payload size with inline strings—one flat block instead of per-string allocations and max-variant padding. |
| **`interp`** | AST expression interpreter | **1.29–1.41× faster** on **2.04× less memory** | Variable-mode AST nodes + compact relative links + tag dispatch (no vtables, no max-variant bloat). |
| **`sexp`** | Parse & query S-expressions | **2.43× less memory** than Rust arena (**5.04×** less than C++ nodes) | Packed variable-mode nodes + 4-byte self-relative links + O(1) teardown. |
| **`bintrees`** | Deep tree build + walk + teardown | **204 ms** vs. Rust arena **235 ms**, C++ arena **393 ms** (per-node allocators > 3,000 ms) | Bump-allocated typed references + zero per-node free/drop cost. |
| **`graph` (CSR)** | Graph build + check-free BFS | **617 ms** (clang) vs. C++ **761 ms**, Rust **811 ms** | `u32 in dist` + `u32 upto out` validate once during build; `checkfree fn bfs` runs with zero bounds checks. |
| **`respond`** | Web DTO build & render | **491 ms** vs. Rust DTO **1,338 ms** (streaming: 400 vs. 540 ms) | Inline string & record arrays avoid nested heap allocations per request. |
| **`particles`** | Flat numeric physics update | **Dead heat** across Goose, Rust, and clang C++ | Flat arrays of floats already fit tightly in every language; identical LLVM backend codegen. |
| **`blur`** | 2D image stencil (`checkfree`) | **266–269 ms** clang (matches Rust's 299 ms); **952 ms** flat vs. **298 ms** row-slice on MSVC | Even with zero bounds checks, MSVC needs row-slice form to vectorize; clang vectorizes both. |
| **`calc`** | Many small parses + frequent error returns | **481/491 ms** vs. Rust arena **436 ms** (slight loss) | Tiny per-input work exposes small per-call overhead and `varint`/dispatch tradeoffs. |
| **`lru`** | Mutation-heavy doubly-linked cache | **2,068 ms** clang (ahead of Rust's 2,249 ms), **2,760 ms** MSVC (behind) | Relinking self-relative links puts address offset math on the pointer-chasing critical path; MSVC schedules base+offset poorly. |
| **`scene`** | Frame-mutated scene graph | **371 ms** clang (tie with Rust 379 ms), **460 ms** MSVC (behind) | Heavy mutation + backend codegen differences under MSVC. |

**The takeaway is clear:**
- Whenever your problem involves **trees, ASTs, heterogeneous records, jagged arrays, strings, DTOs, graphs, or serialization**, Goose's inline memory model gives you a structural advantage that is difficult and verbose to match even in expert C++ or Rust.
- Whenever your problem is a **flat numeric array kernel**, Goose matches C++ and Rust when you write clean `checkfree` / `simd` row-slice loops and compile with clang or GCC—and you should not expect magic speedups over identical LLVM code.
- Whenever your problem is **relink-heavy mutation**, use `u32 in pool` indices or `in pool` links instead of self-relative links, and measure under your target C backend.

---

## 7. Worked Example: Comparing Layouts and Check-Free SIMD Filtering

Let's tie together layout engineering, exact-width arithmetic, `inline fn`, `checkfree fn`, and `simd fn` in a self-contained signal-processing example. We will:

1. Compare storing 512 sensor frames in a padded fixed-mode representation versus a compact packed stream using `bytes_of`.
2. Process a 2D grayscale tile with a **3-tap horizontal + vertical separable box blur**, proving zero bounds checks with `checkfree`, avoiding `u8` overflow by widening to `u16` explicitly, and multi-versioning the row kernel with `simd fn`.
3. Cross-check the `simd fn` output byte-for-byte against a simple scalar reference implementation.

```goose
// 1. Compact exact-width pixel kernel: accumulate in u16 so sums never wrap at 8 bits.
checkfree inline fn avg3(a: u8, b: u8, c: u8) -> u8 {
    var sum: u16 = a;
    sum += b;
    sum += c;
    return (sum / 3) as u8;
}

// 2. Separable horizontal pass on one row: checkfree + simd fn.
//    No early exits, pure arithmetic, zero bounds checks.
checkfree simd fn blur_row_simd(src: const u8[:], dst: u8[:]) {
    assert(src.len == dst.len);
    let w = src.len;
    if w == 0 { return; }
    if w == 1 {
        dst[0] = src[0];
        return;
    }

    dst[0] = avg3(src[0], src[0], src[1]);
    for x in 1..w - 1 {
        dst[x] = avg3(src[x - 1], src[x], src[x + 1]);
    }
    dst[w - 1] = avg3(src[w - 2], src[w - 1], src[w - 1]);
}

// 3. Full 2D blur using row slices (friendly to both clang and MSVC).
checkfree fn blur_2d_simd(
    src: const u8[:],
    tmp: u8[:],
    dst: u8[:],
    w: i64,
    h: i64,
) {
    assert(w >= 2 && h >= 2);
    assert(src.len == w * h);
    assert(tmp.len == w * h);
    assert(dst.len == w * h);

    // Pass 1: horizontal blur row by row into tmp.
    for y in h {
        let lo = y * w;
        blur_row_simd(src[lo..lo + w], tmp[lo..lo + w]);
    }

    // Pass 2: vertical blur using three hoisted row slices.
    for y in h {
        let ym = if y > 0 { y - 1 } else { 0 };
        let yp = if y + 1 < h { y + 1 } else { h - 1 };
        let r0 = tmp[ym * w..ym * w + w];
        let r1 = tmp[y * w..y * w + w];
        let r2 = tmp[yp * w..yp * w + w];
        let out = dst[y * w..y * w + w];
        for x in w {
            out[x] = avg3(r0[x], r1[x], r2[x]);
        }
    }
}

// 4. Scalar reference for verification.
fn blur_2d_ref(src: const u8[:], dst: u8[:], w: i64, h: i64) {
    var tmp: u8[>..] = [];
    tmp.resize(w * h, 0);

    for y in h {
        for x in w {
            let xm = if x > 0 { x - 1 } else { 0 };
            let xp = if x + 1 < w { x + 1 } else { w - 1 };
            var s: i64 = src[y * w + xm];
            s += src[y * w + x];
            s += src[y * w + xp];
            tmp[y * w + x] = (s / 3) as u8;
        }
    }

    for y in h {
        let ym = if y > 0 { y - 1 } else { 0 };
        let yp = if y + 1 < h { y + 1 } else { h - 1 };
        for x in w {
            var s: i64 = tmp[ym * w + x];
            s += tmp[y * w + x];
            s += tmp[yp * w + x];
            dst[y * w + x] = (s / 3) as u8;
        }
    }
}

fn main() {
    let w = 64;
    let h = 32;
    let total = w * h;

    var img: u8[>..] = [];
    var tmp: u8[>..] = [];
    var fast_out: u8[>..] = [];
    var ref_out: u8[>..] = [];

    for i in total {
        img.push(((i * 37 + 13) % 256) as u8);
    }
    tmp.resize(total, 0);
    fast_out.resize(total, 0);
    ref_out.resize(total, 0);

    blur_2d_simd(img, tmp, fast_out, w, h);
    blur_2d_ref(img, ref_out, w, h);

    var mismatches = 0;
    var checksum: u64 = 0;
    for i in total {
        if fast_out[i] != ref_out[i] {
            mismatches += 1;
        }
        checksum = checksum * 131 + fast_out[i];
    }

    print("pixels=", total, " mismatches=", mismatches, " checksum=", checksum);
}
```

```text
pixels=2048 mismatches=0 checksum=2968905902751271861
```

Every single pixel matches (`mismatches=0`), the entire 2D pipeline is compiler-verified `checkfree`, `blur_row_simd` automatically dispatches to AVX2/AVX-512 under clang, and pre-allocating `tmp` with `resize` keeps both loops free of array growth so Goose hoists every row view into registers.

---

## Exercises

1. **Inspect the generated C:** Take the `matmul_simd` program from Section 3 and run `goose -o /tmp/matmul.c matmul.goose` (or `goose check --stats --bce-lines matmul.goose`). Locate `matmul_simd_simd1` and `matmul_simd_simd2` in the generated C, and confirm that `GS_IDX` does not appear inside either `matmul_ref` or `matmul_simd`.
2. **Measure `varint` vs. fixed integers:** Define two variable-mode enums—one storing small counters in `varint` and another in `u16`—push 100,000 elements into each, compare `bytes_of(...).len`, and then compile with `goose build` (not `goose run`) to time summing all elements 500 times using `os::time_ms()`.
3. **Eliminate a hidden copy:** Write a function `fn filter_positive(xs: const i64[:]) -> i64[>..]` that initially returns two different local arrays on two branches (for example, returning a separate `fallback` array when nothing matches). Inspect the generated C or add a large input to see the copy, then refactor it so every path constructs into or returns a single named result `out`.
4. **Vectorizable ASCII classifier:** Write a `checkfree simd fn count_ascii_letters(s: const u8[:]) -> i64` using wrapping subtraction (`(c - 'a') < 26` and `(c - 'A') < 26`—remembering unsigned subtraction wraps modulo 256!) with no early exits and no lookup table. Verify its result against a simple `for c in s` loop over a string containing mixed ASCII and high-bit bytes (`0x80`–`0xFF`).

---

## Key Takeaways

- **Layout is the biggest lever.** Variable-mode enums (`T..[>..]`), inline variable-length strings, packed structs, and narrow links reduce working-set size by 2×–7× and eliminate allocator and teardown overhead.
- **Pick the right link for the access pattern.** Use self-relative links (`T&<u32>`) for build-once, walk-many trees and serializable formats; use `u32 in pool` index types or `in pool` links for relink-heavy mutable structures.
- **Cooperate with view hoisting and stack-top caching.** Keep hot element loops free of growth operations, pre-`resize` destination buffers, and pass distinct output arrays directly as parameters to hot push helpers.
- **Use `simd fn` and `inline fn` surgically.** Mark loop-bearing, check-free, branchless arithmetic kernels as `simd fn`, mark per-element helpers as `inline fn`, and call `mul_add` (from `math`) where you want hardware FMA without losing cross-platform determinism elsewhere.
- **Benchmark honestly.** Always compile with `goose build` (never TinyCC JIT or `--debug`), include teardown inside the timed region, compare matching ownership interfaces, and inspect the generated C when a backend surprises you.
