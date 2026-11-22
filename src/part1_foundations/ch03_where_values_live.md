# Chapter 3: Where Values Live: Size Classes and Data Stacks

Every rule you will meet in the rest of this book—why Goose has seven array types, why references survive `push`, why non-fixed values never copy silently, and why recursive functions cannot hold growable locals—flows from one foundation: **where values live in memory**.

In most languages, memory management is an implementation detail hidden behind `new`, `Box`, or a garbage collector. In Goose, memory placement is simple, static, and completely transparent. Once you picture how the compiler maps variables onto **data stacks**, the entire language snaps into focus.

By the end of this chapter, you will be able to:

- Classify any Goose type into its size class (**fixed**, **variable**, **resizable**, or **root**) and determine whether it is **flat**.
- Explain Goose's three memory regions and the stack invariant that makes bump allocation safe.
- Inspect stack assignments with `goose check --stacks` (or the **Stacks** button in the playground) and predict when scopes share or separate stacks.
- Rely on explicit `copy(...)` and the **in-place construction guarantee** to eliminate hidden O(n) copies.

---

## 3.1 The Four Size Classes (and Flat Types)

Every type in Goose belongs to exactly one **size class**, which tells the compiler whether the value's byte size is known before the program runs, fixed at construction, or allowed to change later:

| Size Class | Byte Size | Examples | Typical Storage |
|---|---|---|---|
| **fixed** | Compile-time constant | Scalars (`i64`, `f32`, `bool`), fixed arrays `T[k]`, limited arrays `T[..k]`, structs of fixed fields, fixed-mode enums `E`, references `T&`, slices `T[:]` | Native call stack / CPU registers (or inline in a container) |
| **variable** | Chosen at construction, frozen forever after | `T[]`, `T[varint]`, `T[..]`, structs with a `varint` or variable field, variable-mode enums `E..` | Data stack (or inline in a variable/resizable container) |
| **resizable** | May grow (or shrink) after construction | Grow-only `T[>..]`, grow-shrink `T[>..<]`, structs ending in a resizable tail | Top of its own data stack |
| **root** | No single contiguous layout | `root struct` bundling independently stored fields (such as multiple resizable arrays) | Owned by a local or global declaration; each field uses its own storage |

For any ordinary compound type (a struct or array), its size class is the **maximum** of its parts (`fixed < variable < resizable`):

- `struct Point { x: f64, y: f64 }` is **fixed** (16 bytes).
- `struct Tag { id: i32, name: u8[..16] }` is still **fixed** (capacity 16 lives inline!).
- `struct Record { id: i32, name: u8[] }` is **variable** (size depends on `name`'s length at construction).
- `struct Buffer { header: i32, data: u8[>..] }` is **resizable** (its tail can grow).
- A `root struct` always keeps the **root** class, even if all its fields happen to be fixed.

### Flat Types

Orthogonal to size class is a single yes/no property: a type is **flat** if it contains **no references, slices, or relative references at any depth**, and is not a `root struct`.

A flat value is completely self-contained bytes—even if it is variable-sized or resizable (such as a `struct Row { y: i32, pixels: u8[] }`). Flatness matters whenever bytes leave their current memory domain: sending messages across threads (Chapter 14) or copying raw data cleanly without dangling addresses.

---

## 3.2 The Three Memory Regions and the Stack Invariant

A running Goose program has **no general-purpose heap**. Its entire memory model consists of three regions:

```text
+-------------------------------------------------------------------+
| 1. Static Read-Only Data                                          |
|    String literals ("hello"), compile-time `const` globals        |
|    Shared across all threads; never freed until process exit      |
+-------------------------------------------------------------------+

+-------------------------------------------------------------------+
| 2. Native Call Stack (& CPU Registers)                            |
|    Fixed-size locals, parameters, return addresses, and the       |
|    length headers of outermost resizable locals                   |
+-------------------------------------------------------------------+

+-------------------------------------------------------------------+
| 3. N Data Stacks  (N computed statically by the compiler)         |
|                                                                   |
|   Data Stack 0:  [  2 GB reserved virtual space  ][1 MB guard]    |
|                  [==== live values ====>          ]               |
|                                         ^ top0 (bump pointer)     |
|                                                                   |
|   Data Stack 1:  [  2 GB reserved virtual space  ][1 MB guard]    |
|                  [== active T[>..] ==>            ]               |
|                                       ^ top1 (bump pointer)       |
|                                                                   |
|   Data Stack 2:  [  2 GB reserved virtual space  ][1 MB guard]    |
|                  [^ top2 (reused by sequential scopes)            |
+-------------------------------------------------------------------+
```

Each data stack is a large virtual address-space reservation (2 GB by default, adjustable via `--stack-reserve 16G`) separated by a 1 MB unmapped guard gap. On 64-bit operating systems, reserving virtual address space costs essentially no physical RAM—pages are committed lazily by the OS only when your program actually writes to them.

> **Memory Model:** There is no runtime allocator metadata, no free list, and no object header per allocation. At runtime, a data stack is literally just a bump pointer.

### The Stack Invariant

How can a language offer arbitrary dynamic arrays without `malloc` or moving memory? By enforcing four rules **statically at compile time**:

1. **Strictly nested lifetimes.** Values on any single data stack are pushed in scope order and popped *all at once* when their owning scope exits. Releasing them is one instruction: resetting `top` back to its saved mark.
2. **At most one live resizable per stack, always on top.** For the entire lifetime of a `T[>..]` or `T[>..<]`, nothing else may be allocated above it on the same stack. Its length header lives in the owning stack frame (outside the data stack), so growing the array simply writes at `top` and bumps `top` forward—no capacity check, no reallocation, no moving.
3. **Simultaneous resizables get separate stacks; sequential ones reuse stacks.** If three growable arrays are alive at the same time, the compiler assigns them three distinct data stacks. As soon as a block ends and frees a stack, the very next block can reuse that same stack.
4. **Variable values can be buried once finished.** A variable-class value (`u8[]`, `Order`) grows on a stack while it is being constructed; the instant construction finishes, its size is frozen, so later values can safely live above it on the same stack.

You never name stacks in Goose code—there is no stack syntax anywhere in the language. The compiler computes `N` automatically from the program's call graph.

### Watching the Compiler Assign Stacks

Click **Stacks** on the example below (or run `goose check --stacks`) to see the compiler's exact stack assignment:

```goose
fn main() {
    // Two simultaneously live grow-only arrays -> 2 distinct data stacks.
    var a: i64[>..] = [];
    var b: i64[>..] = [];
    for i in 5 {
        a.push(i);
        b.push(i * 10);
    }

    // Two sequential blocks -> they reuse the SAME third data stack!
    {
        var tmp1: u8[>..] = [];
        format(tmp1, "first=", a.len);
        print(tmp1);
    }
    {
        var tmp2: u8[>..] = [];
        format(tmp2, "second=", b.len);
        print(tmp2);
    }
}
```

```
first=5
second=5
```

Notice what `goose check --stacks` reports: `tmp1` and `tmp2` never overlap in time, so adding a third, fourth, or hundredth sequential block does **not** add any extra data stacks!

### Releasing Peak Memory to the OS

When a scope exits and rewinds a data stack's bump pointer, the memory immediately becomes reusable by your program, while the OS normally keeps those committed pages mapped for speed (so the next loop iteration doesn't pay page-fault overhead). If your program finishes an unusually huge transient workload and wants to return unused physical pages to the operating system, call `trim_memory()` from the `os` module:

```goose
import os;

fn main() {
    {
        var huge: u8[>..] = [];
        for i in 200_000 {
            huge.push((i % 256) as! u8);
        }
        print("peak=", huge.len, " bytes");
    }
    // `huge` is already freed; now ask the OS to discard its unused pages.
    trim_memory();
    print("trimmed unused pages");
}
```

```
peak=200000 bytes
trimmed unused pages
```

`trim_memory()` preserves every live value and every virtual reservation; subsequent growth continues working seamlessly.

---

## 3.3 Nothing Moves: O(1) Cleanup and Stable References

Two practical superpowers fall directly out of the stack invariant.

### Scope Exit Is an O(1) Free

A growable buffer declared inside a loop body starts fresh on every iteration and disappears completely at the closing `}`—no matter how large or deeply nested it is:

```goose
fn main() {
    for round in 3 {
        var scratch: u8[][>..] = [];
        for i in 500 {
            scratch.push(str("item-", round, "-", i));
        }
        print("round ", round, ": ", scratch.len, " inline strings");
    } // One store resets the stack top — zero per-string teardown!
}
```

```
round 0: 500 inline strings
round 1: 500 inline strings
round 2: 500 inline strings
```

### References Survive Unlimited Growth

Because a growable array sits at the top of a dedicated virtual reservation, pushing elements **never reallocates or relocates** existing elements. `push` returns a direct reference to the newly added element, and that reference stays valid for as long as the array lives:

```goose
struct Entry { id: i32, score: f32 }

fn main() {
    var entries: Entry[>..] = [];
    let first .= entries.push(Entry { id: 1, score: 10.0 });
    let second .= entries.push(Entry { id: 2, score: 20.0 });

    for i: i32 in 3..50_001 {
        entries.push(Entry { id: i, score: 0.0 });
    }

    // Both references still point straight at elements 0 and 1:
    first.score += 5.5;
    second.score *= 2.0;
    print("count=", entries.len, " first=", entries[0], " second=", entries[1]);
}
```

```
count=50000 first=Entry { 1, 15.5 } second=Entry { 2, 40.0 }
```

> **Coming from C++ / Rust:** In C++, holding `&entries[0]` across `push_back` causes undefined behavior as soon as the vector reallocates unless you pre-call `reserve` with a known upper bound. In safe Rust, holding `&mut entries[0]` across `entries.push(...)` is a borrow-checker error, forcing you to use integer indices instead. In Goose, stable typed references across growth are the default—and Chapters 12 and 13 build whole trees and graphs directly on top of this guarantee.

---

## 3.4 Copies Are Visible

In C++, writing `auto b = a;` on a `std::vector` silently triggers an O(n) heap allocation and deep copy. In Goose, **costly copies are never silent**:

- **Fixed-size values copy implicitly** on assignment and pass-by-value (scalars, fixed structs, `T[k]`, `T[..k]`, references, slices)—just like C.
- **Non-fixed values never copy implicitly.** When an existing variable, field, or element of variable or resizable class is bound to an un-annotated `let`/`var`, passed to a reference/slice/untyped parameter, or iterated in a `for` loop, it **binds by reference**—costing O(1).
- Whenever you genuinely want a second independent copy of a non-fixed value, you write `copy(x)` explicitly.

```goose
struct Point { x: i32, y: i32 }

fn main() {
    // Fixed-size struct: assignment copies silently.
    var p = Point { 1, 2 };
    var q = p;
    q.x = 99;

    // Resizable array: `let alias = a` binds by reference (NO copy!).
    var a: i64[>..] = [10, 20, 30];
    let alias = a;
    alias.push(40);

    // Explicit O(n) copy: `b` gets its own data stack and storage.
    var b = copy(a);
    b.push(999);

    print("p=", p, " q=", q);
    print("a=", a, " b=", b);
}
```

```
p=Point { 1, 2 } q=Point { 99, 2 }
a=[10, 20, 30, 40] b=[10, 20, 30, 40, 999]
```

If a destination *requires* an owned value of a non-fixed type (such as a by-value non-fixed parameter or a typed local `var b: i64[>..] = ...`), handing it an existing variable without `copy(...)` is a **compile error**:

<!-- compile-fail -->
```goose
fn main() {
    var a: i64[>..] = [1, 2, 3];
    var b: i64[>..] = a;             // error: non-fixed values never copy implicitly; use copy(a)
    print(b);
}
```

Look how clean the rule is: if you do not see `copy(...)` in Goose source code, no non-fixed value is being copied behind your back.

---

## 3.5 The In-Place Construction Guarantee

If non-fixed values never copy implicitly, how can a function return a growable array `i64[>..]` or a fresh string `u8[>..]` by value without copying it?

Through Goose's **in-place construction guarantee**:

> **Memory Model:** Every freshly constructed non-fixed value—whether a literal, a `str(...)` call, a `copy(...)`, or a function's return value—is constructed **directly in its final destination**, through any depth of calls and branches. No intermediate temporary is created and copied.

Consider these three lines:

```text
1. let nums = make_squares(5);
   Caller allocates the stack destination for `nums` and passes it into `make_squares`.
   `make_squares` pushes elements straight into `nums`. Zero copies on return!

2. words.push(str("word", i));
   `words.push(...)` opens the new element slot right at `words`'s bump pointer and passes
   that destination to `str(...)`. `str` writes the length and bytes straight into `words`!

3. orders.push(Order { id: 1, customer: "alice", items: parse_items(...) });
   `orders.push` writes `id` and `customer` directly at `orders`'s top, then hands the
   exact next byte address to `parse_items(...)` to stream `Item`s straight into `orders`!
```

When a function builds a single local variable (say `var out: i64[>..] = [];`) and every `return` returns that same local, `out` *is* the caller's destination from the line it is declared:

```goose
fn make_squares(n: i64) -> i64[>..] {
    var out: i64[>..] = [];          // built directly in the caller's destination!
    for i in n {
        out.push(i * i);
    }
    return out;
}

fn format_label(prefix: const u8[:], id: i64) -> u8[>..] {
    str(prefix, "#", id)             // built directly in whatever receives the result
}

fn main() {
    let squares = make_squares(6);
    var labels: u8[][>..] = [];
    for s, i in squares {
        labels.push(format_label("sq", i));
    }
    print("squares=", squares);
    print("labels=", labels);
}
```

```
squares=[0, 1, 4, 9, 16, 25]
labels=["sq#0", "sq#1", "sq#2", "sq#3", "sq#4", "sq#5"]
```

This guarantee changes how you design APIs in Goose: returning a freshly constructed `T[>..]` by value is **just as fast as passing an out-parameter**, so you only need an out-parameter (`out: u8[>..]&`) when you want to *append* into an existing buffer across multiple calls (like `format(buf, ...)`).

> **Watch Out (Construction in Progress):** Because `xs.push(expr)` constructs the new element directly at `xs`'s stack top, nothing inside `expr` (or any function `expr` calls) may also grow `xs` while that element is half-built. The compiler rejects `xs.push(grow_and_return(xs))` statically—compute the value in a local first if you need to mutate `xs` beforehand.

---

## 3.6 Worked Example: A Windowed Sensor Processor

Let us combine everything from Chapter 3 into a realistic data-processing pipeline:

- `generate_samples` constructs a `i32[>..]` directly in `main`'s variable (in-place return).
- `summarize_windows` processes fixed-size windows using a **loop-scoped scratch buffer** freed in O(1) at the end of every window, while streaming `WindowSummary` structs directly into its returned `WindowSummary[>..]`.
- `main` also takes a **stable reference** to the first summary before pushing an extra baseline record, and demonstrates explicit `copy(...)` for a snapshot.

```goose
struct WindowSummary {
    window_id: i32,
    min_val: i32,
    max_val: i32,
    avg_val: f64,
}

fn generate_samples(count: i64) -> i32[>..] {
    var out: i32[>..] = [];          // built in place at caller's destination
    for i in count {
        // Deterministic sawtooth wave in [10, 49]
        let v = 10 + ((i * 7 + 3) % 40) as i32;
        out.push(v);
    }
    return out;
}

fn summarize_windows(samples: i32[:], win_size: i64) -> WindowSummary[>..] {
    var results: WindowSummary[>..] = [];
    var pos = 0;
    var win_id: i32 = 0;

    while pos < samples.len {
        let end = if pos + win_size < samples.len { pos + win_size } else { samples.len };

        // Scoped scratch buffer: lives only for this window, freed in O(1) at `}`.
        var scratch: i32[>..] = [];
        for i in pos..end {
            scratch.push(samples[i]);
        }

        var lo = scratch[0];
        var hi = scratch[0];
        var sum = 0;
        for v in scratch {
            if v < lo { lo = v; }
            if v > hi { hi = v; }
            sum += v;
        }

        results.push(WindowSummary {
            window_id: win_id,
            min_val: lo,
            max_val: hi,
            avg_val: sum as f64 / scratch.len,
        });

        pos = end;
        win_id++;
    }
    return results;
}

fn main() {
    let raw = generate_samples(12);
    var summaries = summarize_windows(raw, 4);

    // Explicit copy before we append an extra record to `summaries`.
    let snapshot = copy(summaries);

    // Reference into `summaries` survives subsequent pushes!
    let first .= summaries[0];
    summaries.push(WindowSummary { window_id: 99, min_val: 0, max_val: 0, avg_val: 0.0 });
    first.max_val += 1;

    print("raw=", raw);
    print("snapshot len=", snapshot.len, " first.max=", snapshot[0].max_val);
    print("live     len=", summaries.len, " first.max=", summaries[0].max_val);
    for s in summaries {
        print("  win ", s.window_id, ": min=", s.min_val, " max=", s.max_val, " avg=", s.avg_val);
    }
}
```

```
raw=[13, 20, 27, 34, 41, 48, 15, 22, 29, 36, 43, 10]
snapshot len=3 first.max=34
live     len=4 first.max=35
  win 0: min=13 max=35 avg=23.5
  win 1: min=15 max=48 avg=31.5
  win 2: min=10 max=43 avg=29.5
  win 99: min=0 max=0 avg=0.0
```

Click **Stacks** on this worked example in the playground! Notice how few total data stacks the entire multi-function pipeline needs, even though `generate_samples`, `summarize_windows`, `scratch`, `snapshot`, and `summaries` all manage dynamic arrays.

---

## Exercises

1. **Experiment with stack reuse:** Open the Section 3.2 example in the playground editor, move `tmp2` *inside* `tmp1`'s `{ ... }` block so both are alive simultaneously, and click **Stacks**. Compare the reported stack count before and after.
2. **Trigger the construction-in-progress check:** Write a small function `fn push_and_zero(xs: i64[>..]&) -> i64 { xs.push(0); return 0; }` and try calling `xs.push(push_and_zero(xs));` inside `main`. Read the exact error message Goose prints, then rewrite it safely in two lines.
3. **Verify zero-copy binding vs. explicit `copy`:** Modify the Section 3.6 worked example by changing `let snapshot = copy(summaries);` to `let snapshot = summaries;`. Run it and observe how both `snapshot.len` and `snapshot[0].max_val` change—and explain why.

---

## Key Takeaways

- Every type belongs to one of four size classes (**fixed**, **variable**, **resizable**, **root**); a type without references, slices, or relative references (and not a `root struct`) is **flat**.
- Goose replaces the heap with **N statically assigned data stacks**: at most one live resizable per stack on top, pointer-bump growth, zero relocation, and single-store O(1) scope-exit deallocation.
- Fixed-size values copy silently; non-fixed values **never copy implicitly**—binding them creates a reference, and O(n) duplication always requires explicit `copy(x)`.
- The **in-place construction guarantee** builds fresh non-fixed values directly in their final destination through any depth of calls, making by-value returns of growable arrays zero-copy.

With the data-stack mental model firmly in place, **Chapter 4** explores the entire **array family and strings**, showing how to pick the exact container layout—from fixed `T[k]` and inline `T[..k]` to `T[varint]` and `T[>..<]`—for any problem.
