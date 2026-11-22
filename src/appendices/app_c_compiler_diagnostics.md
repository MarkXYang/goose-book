# Appendix C: Field Guide to Compiler Diagnostics

Goose rejects dangling pointers, unsafe shrinks, illegal recursive stacks, hidden copies, and retained bounds checks in `checkfree` functions at compile time. Because the diagnostics talk in terms of **roots**, **storage**, and **bounds**, seeing the exact broken pattern right beside its clean fix makes every error message immediately actionable.

Every broken listing below is verified to fail compilation (`compile-fail`), and every fix is verified to compile and run with zero warnings.

---

## 1. Returning a Reference to a Local Variable

**Symptom:** You try to return a reference or slice pointing into an array declared inside the function.

<!-- compile-fail -->
```goose
fn make_greeting() -> const u8[:] {
    let buf = str("hello");
    return buf[..]; // error: slice points into local `buf`, which ends on return
}

fn main() {
    print(make_greeting());
}
```

**Why:** `buf` lives on `make_greeting`'s data stack, which resets the moment `make_greeting` returns. Returning a view into it would dangle.

**Fix:** Return the owned `u8[>..]` by value—Goose constructs it directly on the caller's stack with zero copy!

```goose
fn make_greeting() -> u8[>..] {
    return str("hello"); // built straight at the caller's destination
}

fn main() {
    print(make_greeting());
}
```

```text
hello
```

---

## 2. Shrinking an Array While a View Is Still Live

**Symptom:** You call `pop`, `clear`, `resize`, or whole-array assignment on a `T[>..]` while a reference or slice into it is still used later.

<!-- compile-fail -->
```goose
fn main() {
    var xs: i64[>..] = [10, 20, 30];
    let first .= xs[0];
    xs.clear();          // error: `first` is still used after `xs` shrinks
    print(first);
}
```

**Why:** Shrinking reclaims elements on the data stack, which would invalidate `first`.

**Fix:** Either finish using `first` *before* calling `clear()`, or copy the fixed-size value (`let first = xs[0];` without `.=`) before shrinking:

```goose
fn main() {
    var xs: i64[>..] = [10, 20, 30];
    let first = xs[0];   // copies the i64 value (not a reference)
    xs.clear();          // safe: no live views into `xs`
    print("saved=", first, " len=", xs.len);
}
```

```text
saved=10 len=0
```

---

## 3. Growing an Array While Constructing an Element Into It

**Symptom:** You call `xs.push(f(xs))` where `xs` holds variable-size elements (or relative links) and `f` also pushes onto `xs`.

<!-- compile-fail -->
```goose
struct Node { next: Node&<u32>?, val: i32 }

fn push_child(pool: Node[>..]&) -> Node& {
    return pool.push(Node { next: null, val: 1 });
}

fn main() {
    var pool: Node[>..] = [];
    // error: `push_child` grows `pool` while `pool.push(...)` is building a Node in place
    pool.push(Node { next: push_child(pool), val: 2 });
}
```

**Why:** Elements with relative links or variable size are constructed directly at the top of `pool`'s data stack. Growing `pool` inside a field initializer would overwrite the half-built element.

**Fix:** Evaluate `push_child(pool)` into a reference *before* calling `pool.push(...)`:

```goose
struct Node { next: Node&<u32>?, val: i32 }

fn push_child(pool: Node[>..]&) -> Node& {
    return pool.push(Node { next: null, val: 1 });
}

fn main() {
    var pool: Node[>..] = [];
    let child .= push_child(pool);
    let parent .= pool.push(Node { next: child, val: 2 });
    let next = parent.next;
    if next {
        print("parent=", parent.val, " child=", next.val);
    }
}
```

```text
parent=2 child=1
```

---

## 4. Holding a Growable Local Across a Recursive Call

**Symptom:** A `recursive fn` declares a `T[>..]` (or calls `str(...)`) that remains live across a recursive call.

<!-- compile-fail -->
```goose
recursive fn countdown(n: i64) {
    if n == 0 { return; }
    var tag = str("level ", n); // error: growable local alive across recursive call
    countdown(n - 1);
    print(tag);
}

fn main() { countdown(3); }
```

**Why:** Goose assigns a fixed compile-time count of data stacks; an unbounded recursive chain cannot each claim a fresh live data stack.

**Fix:** Either end the scratch buffer's scope *before* recursing (block-scoped scratch), or pass a caller-owned buffer by reference:

```goose
recursive fn countdown(n: i64) {
    if n == 0 { return; }
    {
        let tag = str("level ", n);
        print(tag);
    }                    // `tag`'s data stack resets before recursing!
    countdown(n - 1);
}

fn main() { countdown(3); }
```

```text
level 3
level 2
level 1
```

---

## 5. Mutating a Read-Only (`const`) Slice or String Literal

**Symptom:** Calling a mutating helper taking `u8[:]` with a string literal `"banana"` or a `const` variable fails with a writability error.

<!-- compile-fail -->
```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 { s[0] = 'B'; }
}

fn main() {
    capitalize_first("banana"); // error: cannot write through read-only string literal
}
```

**Why:** String literals live in read-only static memory (`const u8[:]`). While Goose allows a read-only `u8[:]` helper to accept literals if it never writes to `s`, any write through `s` (or storing `s` in a writable slot) requires writable backing storage.

**Fix:** Materialize a mutable buffer (such as `str("banana")`) before mutating in place, and mark read-only parameters `const u8[:]` so the intent is clear in the signature:

```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 { s[0] = 'B'; }
}

fn main() {
    var word = str("banana");
    capitalize_first(word);
    print(word);
}
```

```text
Banana
```

---

## 6. `checkfree fn` Retains a Bounds Check

**Symptom:** A `checkfree fn` fails compilation because BCE cannot prove an index read from an array (or an unconstrained parameter) is in bounds.

<!-- compile-fail -->
```goose
checkfree fn sum_indexed(vals: const i64[:], idxs: const u32[:]) -> i64 {
    var total = 0;
    for k in idxs {
        total += vals[k]; // error: BCE does not track arbitrary values stored in `idxs`
    }
    return total;
}

fn main() {
    let vals: i64[3] = [10, 20, 30];
    let idxs: u32[2] = [0, 2];
    print(sum_indexed(vals, idxs));
}
```

**Why:** BCE tracks integer variables and array lengths, not arbitrary integers stored inside another array.

**Fix:** Store validated index types (`u32 in vals`) in `idxs` (moving validation to construction time) or pass `assert` preconditions for length-derived indices:

```goose
var vals: i64[>..] = [10, 20, 30];

checkfree fn sum_indexed(idxs: const (u32 in vals)[:]) -> i64 {
    var total = 0;
    for k in idxs {
        total += vals[k]; // guaranteed in bounds by `u32 in vals`!
    }
    return total;
}

fn main() {
    var idxs: (u32 in vals)[>..] = [];
    idxs.push(0 as (u32 in vals));
    idxs.push(2 as (u32 in vals));
    print(sum_indexed(idxs));
}
```

```text
40
```

---

## 7. Copying Non-Fixed Values Implicitly or Queueing Non-Flat Structs

**Symptom A (missing `copy`):** Assigning or passing an existing `T[>..]` by value requires explicit `copy(x)` so $O(n)$ copies are never silent.

**Symptom B (non-flat thread message):** Passing a struct containing a slice (`const u8[:]`) or reference (`Node&`) to `qput` or `thread_spawn` fails because workers share no memory.

<!-- compile-fail -->
```goose
struct BadJob { label: const u8[:] }

fn main() {
    qput(BadJob { label: "task" }); // error: BadJob is not flat (contains a slice)
}
```

**Fix:** Replace the slice field with an owned variable-size array (`u8[]`) so the whole message crosses the queue as one flat byte block:

```goose
struct GoodJob { label: u8[] }

fn main() {
    let j = GoodJob { label: "task" };
    print("flat job ready: ", j.label);
}
```

```text
flat job ready: task
```

---

## 8. Common Compiler Warnings (and Quick Fixes)

| Warning | Cause | Quick Fix |
|---|---|---|
| `redundant 'as T': an X converts to T implicitly here` | Widening cast (`u8 as u16`, `i64 as f64`) where Goose already widens implicitly | Remove the redundant cast, or bind `var acc: u16 = a;` |
| `redundant &: elements of this type bind by reference without it` | Writing `for &n in xs` over a non-fixed element type (variable-mode enum, `u8[][]`, etc.) | Write `for n in xs`—non-fixed elements bind by reference automatically |
| `unused variable` / `unused binding` | A local or match payload binding is never read | Prefix or replace with `_` (e.g., `let _, ok = ...;`) |
| `reusable` / `reusable[]` deprecation warning | Legacy builtin pool modifier | Migrate to `import slot_pool;` (`slot_pool<T>`) or `import slice_pool;` (`slice_pool<T>`) |
