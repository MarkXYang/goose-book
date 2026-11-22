# Chapter 6: Lifetimes Without Annotations

If you have programmed in Rust, you have written function signatures like `fn longest<'a>(a: &'a str, b: &'a str) -> &'a str` and wrestled with the rule that you cannot hold two mutable references to the same structure at once.

In Goose, you write:

<!-- fragment -->
```goose
fn longest(a: u8[:], b: u8[:]) -> u8[:] {
    if a.len >= b.len { a } else { b }
}
```

There is **no lifetime syntax**, **no borrow-checker exclusivity rule**, and **no `unsafe`**—yet Goose still statically rejects every dangling reference and every use-after-shrink.

Understanding *how* the compiler proves reference safety takes only a handful of simple concepts: **roots**, **liveness**, **shrink rules**, and **balanced calls**. Once you see how they fit together, lifetime errors become straightforward to read and fix in seconds.

By the end of this chapter, you will be able to:

- Explain how Goose infers lifetime **roots** across calls, branches, temporaries, and holders without annotations.
- Shrink grow-only arrays (`T[>..]`) cleanly by relying on liveness rather than lexical scope.
- Use grow-shrink arrays (`T[>..<]`) and **balanced calls** when pushing and popping freely.
- Read and resolve any Goose lifetime, shrink, or whole-array assignment diagnostic.

---

## 6.1 Roots: How the Compiler Infers Lifetimes

Every reference `T&`, optional `T?`, and slice `T[:]` has a static **root**: the variable (a local, a global, or a caller-supplied argument) whose storage bounds the lifetime of the target.

You never write roots down. Because Goose is a whole-program monomorphizing compiler, it **infers** roots automatically and specializes functions per distinct root:

- `&x` and `x[..]` are rooted at `x` **exactly**.
- Passing `outer` and `inner` to `longest(outer, inner)` specializes `longest` for those two roots, so the returned slice is rooted at **both** `outer` and `inner`.
- Storing a reference into a **holder** (a struct with a reference field, or an array of slices `u8[:][>..]`) records that root on the holder.

### The Core Lifetime Rules

Seven compact rules govern every reference and slice in Goose:

1. **Store:** You may store a reference into a variable, field, element, or global only if *every* one of its roots outlives the destination's owner.
2. **Return:** A returned reference must be rooted at something that survives the call (a caller argument, a global, or static data)—never a function's own local.
3. **Branches & merged values:** An `if`, `match`, `block`, or `loop` produces its value *after* its branch scopes close, so it cannot return a reference rooted at a variable declared inside a branch. When a value can come from multiple sources, it carries the union of their roots and must satisfy every rule for *all* of them.
4. **Rebinding (`.=`):** A `var` reference commits to its initial binding's scope depth; `.=` can rebind it within the same root or to another variable at the same scope depth.
5. **Temporaries:** A literal, function return value, `copy(...)`, or control-flow expression lives only until the end of its statement. You may pass a view of a temporary into a function call, but you cannot store that view in a variable that outlives the statement.
6. **Holders:** Every store into a holder is tracked so neither a scope exit nor a shrink can strand a reference inside it.
7. **Exact vs. inexact roots:** Taking `&arr[i]` gives the compiler the **exact** array `arr`. Reading a plain `T&` back out of a container only proves an outlives bound against all **candidates** in scope whose storage could hold `T`—fine for reading/mutating, whereas operations requiring exact array identity (`.index_of` or storing a self-relative link) need an exact root.

### Seeing the Checker in Action

First, watch `longest` reject a reference rooted at an inner block escaping into an outer variable `w`:

<!-- compile-fail -->
```goose
fn longest(a: u8[:], b: u8[:]) -> u8[:] {
    if a.len >= b.len { a } else { b }
}

fn main() {
    var outer: u8[>..] = [];
    outer.append("outer");
    var w = outer[..];
    {
        var inner: u8[>..] = [];
        inner.append("inner text");
        w = longest(outer, inner);   // error: rooted at inner, which dies at `}`
    }
    print(w);
}
```

Next, watch the temporary-view rule catch slicing a function's return value directly without first giving it an owning variable:

<!-- compile-fail -->
```goose
fn make_word() -> u8[>..] { str("transient") }

fn main() {
    let dangling = make_word()[..];  // error: view of a temporary dies at `;`
    print(dangling);
}
```

Giving the returned value an owning local (`let owned = make_word(); let view = owned[..];`) solves it immediately because `owned` lives until the end of `main`.

### Why There Are No Aliasing or Exclusivity Rules

Notice what Goose does **not** restrict: you can freely hold multiple writable references to the exact same variable or array elements at the same time!

```goose
fn main() {
    var scores: i64[>..] = [10, 20, 30];
    let a .= scores[0];
    let b .= scores[0];              // two writable references to the exact same element!
    let whole = scores[..];          // plus a writable slice covering it!

    a += 5;
    b += 10;
    whole[0] *= 2;
    scores.push(40);                 // growing `scores` invalidates none of them!

    print("a=", a, " b=", b, " scores=", scores);
}
```

```
a=50 b=50 scores=[50, 20, 30, 40]
```

> **Coming from Rust:** Why does Rust forbid two `&mut` references while Goose allows them? Rust uses exclusivity to prevent two hazards simultaneously: (1) iterator/reference invalidation when a `Vec` reallocates or an `enum` changes variant in place, and (2) data races across threads. Goose eliminates all three hazards structurally instead: data stacks never reallocate, fixed-mode enums disallow interior references while variable-mode enums disallow variant replacement (Chapter 7), and threads share zero mutable memory (Chapter 14). As a result, single-threaded aliasing is completely memory-safe in Goose.

---

## 6.2 Shrinking Grow-Only Arrays (`T[>..]`)

"Grow-only" names a **safety guarantee**, not a prohibition on clearing or popping: **a `T[>..]` never shrinks while any reference or slice rooted in it is still live.**

Whenever nothing rooted in the array is still used afterwards, `clear()`, `pop()`, `pop_n()`, `resize()` downward, and whole-array reassignment are all legal on a `T[>..]`.

### Liveness, Not Lexical Scope

Crucially, Goose checks **liveness** (whether a reference is ever read or written *after* the shrink), not whether the reference variable is still in scope. That means a reusable scratch buffer declared outside a loop can be cleared at the start of every iteration even after handing out slices inside the loop—as long as those slices' last use comes before the next clear!

Better still, shrink checking works **across function calls**: if a helper function `reset(buf)` calls `buf.clear()`, the compiler records that `reset` shrinks its parameter and verifies the caller's live views at every call to `reset`:

```goose
fn reset(buf: u8[>..]&) {
    buf.clear();
}

fn main() {
    var line: u8[>..] = [];
    for i in 3 {
        reset(line);                 // safe: no slice of `line` is live here
        format(line, "row-", i + 1, ":ok");
        let prefix = line[..5];
        print(prefix, " -> ", line[6..]); // last use of `prefix`
    }

    // Even after the loop, clearing is allowed once no view is live:
    line.clear();
    print("final len=", line.len);
}
```

```
row-1 -> ok
row-2 -> ok
row-3 -> ok
final len=0
```

Now watch what happens if a **holder** (`views: u8[:][>..]`) keeps a slice of `line` alive across `line.clear()`:

<!-- compile-fail -->
```goose
fn main() {
    var line: u8[>..] = [];
    line.append("hello");

    var views: u8[:][>..] = [];
    views.push(line[..]);            // `views` now holds a slice rooted in `line`

    line.clear();                    // error: `views` is still used below!
    print(views);
}
```

Notice also statement-level evaluation order: `use(a[0], a.pop())` is rejected when `use` takes its first parameter by reference (because `a.pop()` could free `a[0]` before `use` runs), whereas `a[0] + a.pop()` is valid because by-value `+` reads `a[0]` before evaluating `a.pop()`.

---

## 6.3 Grow-Shrink Arrays (`T[>..<]`) and Balanced Calls

What if you need a stack, queue, or binary heap that pops freely from anywhere—including inside a global variable or the tail of a struct—without worrying about whether some container elsewhere stored a reference into it?

Use **`T[>..<]`** (grow-shrink). Its rule is simple and strict:

> **Memory Model:** References and slices into a `T[>..<]` may live **in local variables (and parameters/returns) only**—never stored into a struct field, array element, or global. Because no heap or holder can hide a reference into it, the compiler checks every shrink purely against the local variables still in use.

Trying to store a reference into a `T[>..<]` inside a struct or another array is rejected immediately:

<!-- compile-fail -->
```goose
struct RefBox { r: i64& }

fn main() {
    var stack: i64[>..<] = [10, 20, 30];
    let b = RefBox { r: stack[0] };  // error: references into T[>..<] cannot be stored
    print(b.r);
}
```

### Balanced Calls

Sometimes a function needs temporary working space on a caller's `T[>..<]` scratch stack while the caller already holds a slice into earlier entries of that very stack.

Goose recognizes a **balanced call**: a function that saves `let mark = stack.len;`, pushes its own entries above `mark`, and ends with `stack.resize(mark);` without ever shrinking below `mark`. Because nothing below `mark` was ever freed, **a balanced call does not count as a shrink at the call site**, so the caller's existing views survive across the call!

```goose
fn sum_with_scratch(stack: i64[>..<]&, n: i64) -> i64 {
    let mark = stack.len;
    for i in 1..n + 1 {
        stack.push(i * i);
    }
    var total = 0;
    for i in mark..stack.len {
        total += stack[i];
    }
    stack.resize(mark);              // restores exact starting length -> balanced!
    return total;
}

fn main() {
    var stack: i64[>..<] = [100, 200];
    let saved = stack[..];           // view of [100, 200]

    // Safe! `sum_with_scratch` is balanced, so `saved` survives the call:
    let sq_sum = sum_with_scratch(stack, 4);
    print("saved=", saved, " sq_sum=", sq_sum, " stack.len=", stack.len);

    // `saved` is never used again after the line above, so popping is now legal:
    let top = stack.pop();
    print("popped=", top, " remaining=", stack);
}
```

```
saved=[100, 200] sq_sum=30 stack.len=2
popped=200 remaining=[100]
```

This exact `mark` / `resize(mark)` pattern is the backbone of sharing a single scratch stack across recursive calls in Chapter 11.

---

## 6.4 Whole-Array Assignment and Limited Arrays

How does reassigning an entire array (`xs = ...`) interact with lifetimes and shrinking?

1. **Whole resizable assignment (`xs = expr;`):** Clears `xs` first and then constructs `expr` directly where `xs`'s old elements sat. Therefore:
   - It obeys the exact same shrink rules as `xs.clear()` (no live views into `xs`).
   - Because `xs` is cleared *before* `expr` is built, `expr` **cannot read `xs` at all**—not even `xs.len` (`xs = f(xs);` is a compile error). Build the replacement in a separate variable and assign `copy(next)` instead.
2. **Variable-class lvalues (`u8[]`, `Order`, `varint` fields):** Frozen at construction and **never** reassignable in place; mutate their fixed-size fields or rebuild the enclosing container.
3. **Limited arrays (`T[..k]`, `T[..]`):** Capacity and element type never change, so references into a `T[..k]` remain memory-safe even across `pop()` and `clear()`—reading whatever valid `T` currently occupies that slot.

```goose
struct Rec { name: u8[], score: i32 }

fn main() {
    // Whole resizable reassignment via separate staging variable:
    var xs: i64[>..] = [1, 2, 3];
    var next: i64[>..] = [];
    for x in xs { next.push(x * 10); }
    xs = copy(next);

    // Variable-class elements: fixed fields are mutable, variable fields are frozen:
    var recs: Rec[>..] = [];
    recs.push(Rec { name: "ada", score: 10 });
    for r in recs { r.score += 5; }
    recs = [Rec { name: "grace", score: 20 }]; // whole container rebuilt cleanly

    // Limited array: fixed capacity means references survive pop/push safely:
    var lim: i64[..4] = [7, 8];
    let slot0 .= lim[0];
    lim.clear();
    lim.push(42);
    print("xs=", xs, " recs=", recs, " slot0=", slot0);
}
```

```
xs=[10, 20, 30] recs=[Rec { "grace", 20 }] slot0=42
```

---

## 6.5 Reading Goose Lifetime & Shrink Diagnostics

Whenever the checker rejects a lifetime or shrink, **read the entire error message**: Goose always names **both ends** of the conflict—the reference (or holder) *and* the line where it was bound or stored, alongside the scope exit or shrink that conflicts with it.

Use this four-step checklist to resolve any lifetime or shrink error quickly:

1. **Finish using the view before shrinking:** Often simply moving a `print(v)` or reading a scalar out of `v` *above* `buf.clear()` kills the view's liveness and makes the code compile.
2. **Widen the owner's scope:** If an inner variable's slice escapes to an outer variable, either declare the owner in the outer scope or move the consumer inside the inner block.
3. **Materialize a copy or inline value:** If a slice into a transient buffer genuinely must outlive that buffer, store an owned `u8[..k]` / `u8[]` or call `copy(...)`.
4. **Choose the right container for the lifetime pattern:** Use `T[>..<]` with balanced `mark`/`resize` for stack-disciplined scratch, or a `slot_pool<T>` / `slice_pool<T>` (Chapter 8) when object lifetimes do not nest at all.

---

## 6.6 Worked Example: A Reusable-Buffer Batch Evaluator

Let us bring Chapter 6's rules together in a small batch processor that parses lines of numbers from a feed using:

- One **reusable `u8[>..]` line buffer** cleared per batch via a helper function (exercising liveness-based shrinking on `T[>..]`).
- One **shared `i64[>..<]` scratch stack** passed into a **balanced** `batch_stats` helper while the caller holds a live view of an earlier baseline prefix!
- Multiple **aliased references** updating the running totals cleanly without borrow-checker friction.

```goose
import std;

fn reset_line(buf: u8[>..]&) {
    buf.clear();
}

// Balanced on `scratch`: pushes parsed values above `mark`, computes their sum
// and max, and restores `scratch.resize(mark)` before returning.
fn batch_stats(line: const u8[:], scratch: i64[>..<]&) -> i64, i64 {
    let mark = scratch.len;
    each_split(line, ',') {
        let v, ok = parse_int(trim(it));
        if ok { scratch.push(v); }
    };

    if scratch.len == mark {
        return 0, 0;
    }

    var sum = 0;
    var best = scratch[mark];
    for i in mark..scratch.len {
        let v = scratch[i];
        sum += v;
        if v > best { best = v; }
    }

    scratch.resize(mark);            // balanced: caller's views below `mark` stay live!
    return sum, best;
}

fn main() {
    let batches: const u8[:][3] = [
        "10, 25, 5",
        "4, 100, -10, 6",
        "7, 7, 7",
    ];

    var line_buf: u8[>..] = [];
    var scratch: i64[>..<] = [1000, 2000];
    let baseline = scratch[..];      // live across every balanced `batch_stats` call!

    var totals: i64[>..] = [];
    for raw, idx in batches {
        reset_line(line_buf);        // safe: no slice of `line_buf` survives the iteration
        format(line_buf, raw);
        let view = line_buf[..];
        let sum, best = batch_stats(view, scratch);
        totals.push(sum);
        print("batch ", idx, ": sum=", sum, " max=", best);
    }

    // Aliased references to the first total both mutate cleanly:
    let first_a .= totals[0];
    let first_b .= totals[0];
    first_a += 1;
    first_b += 2;

    print("baseline still intact: ", baseline);
    print("totals=", totals, " scratch.len=", scratch.len);
}
```

```
batch 0: sum=40 max=25
batch 1: sum=100 max=100
batch 2: sum=21 max=7
baseline still intact: [1000, 2000]
totals=[43, 100, 21] scratch.len=2
```

Notice how clean the architecture is: zero allocations per batch, zero lifetime annotations, `line_buf` safely reused on every iteration, and `baseline` proven valid across all three calls to `batch_stats` because `batch_stats` is statically verified as balanced!

---

## Exercises

1. **Break balance on purpose:** In the Section 6.6 worked example, edit `batch_stats` to change `scratch.resize(mark);` to `scratch.clear();`. Click **▶ Run** and inspect the exact compiler error pointing straight at `baseline`.
2. **Test holder shrink tracking:** In Section 6.2's working `reset(line)` loop, declare `var kept: u8[:][>..] = [];` before the `for` loop and push `prefix` into `kept` inside the loop. Observe how Goose catches the dangling slice on the next `reset(line)` call, then fix it by changing `kept` to `u8[][>..]` (storing owned inline copies).
3. **Whole-array self-assignment:** Try writing `xs = [xs[0] + 1, xs[1] + 1];` on a `var xs: i64[>..] = [1, 2];`. Read the compiler's explanation of why the right-hand side cannot read `xs`, then fix it with a temporary staging variable and `copy(...)`.

---

## Key Takeaways

- Goose infers every reference's **root** statically via whole-program specialization—giving you full dangling-reference safety with **zero `'a` annotations** and **no aliasing/exclusivity restrictions**.
- Grow-only arrays (`T[>..]`) can still shrink (`.clear()`, `.pop()`, `.resize()`, whole reassignment) at any point where no live variable or holder is rooted in them—checked by **liveness**, not lexical scope, and tracked across calls.
- Grow-shrink arrays (`T[>..<]`) keep references out of stored containers so they can pop anywhere; **balanced calls** (`let mark = s.len; ... s.resize(mark);`) preserve caller views taken before the call.
- Whole-array reassignment clears before constructing, so the right-hand side may not read the target array.

Next, in **Chapter 7: Enums in Two Sizes and Tag Dispatch**, we explore how Goose provides dynamic polymorphism without inheritance or vtables—and how choosing between **fixed mode (`Shape`)** and **variable mode (`Shape..`)** unlocks both 4x memory savings and safe in-place payload references.
