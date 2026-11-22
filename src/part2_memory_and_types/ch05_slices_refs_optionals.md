# Chapter 5: Slices, References, and Optionals

In Part I, you saw how Goose stores values inline on data stacks so memory never moves during growth. Now in **Part II**, we explore how your code *points* into that memory safely—without raw pointer arithmetic, without `*` and `->` clutter, without null-pointer dereferences, and without lifetime annotations.

Goose provides three core indirection types, each a fixed-size, zero-overhead value:

- **`T[:]`** — a **slice** (a view of a contiguous run of elements inside any array).
- **`T&`** — a **reference** (a single non-null machine address, completely transparent in expressions).
- **`T?`** — an **optional** (a nullable reference that must be narrowed before use, at zero extra space cost).

By the end of this chapter, you will be able to:

- Write generic array-processing functions over `T[:]` slices using half-open and end-relative (`^k`) ranges.
- Bind, rebind, and compare transparent `T&` references using `.=` and `.==` without accidental copies.
- Return and narrow nullable `T?` references cleanly with `if`, `while`, `guard`, and `assert`.
- Control read-only vs. writable access with `const T&` and `const T[:]`.

---

## 5.1 Slices (`T[:]`): Universal Zero-Copy Views

A slice `T[:]` is a fixed-size pair: a pointer to the first element plus an `i64` element count. Because it copies zero elements, `T[:]` is Goose's universal parameter type for any function that reads or mutates elements in place.

```text
grow: i64[>..]  on a data stack:
  index:   0    1    2    3    4
         +----+----+----+----+----+
         | 10 | 20 | 30 | 40 | 50 |
         +----+----+----+----+----+
               ^              ^
               |              |
  s = grow[1..4]:  { ptr -> elem 1, len = 3 }
  tail = grow[^2..]:              { ptr -> elem 3, len = 2 }
```

### Slicing Syntax and Coercion

- `a[x..y]` views elements `x` up to `y` (exclusive).
- Omit either bound to start at `0` (`a[..y]`) or run to `a.len` (`a[x..]`), or omit both (`a[..]`) for the whole array.
- `^k` counts from the end (`len - k`): `a[^2..]` is the last two elements, and `a[..^1]` drops the last element.
- **Automatic coercion:** Whenever *any* array kind (`T[k]`, `T[]`, `T[..k]`, `T[..]`, `T[>..]`, `T[>..<]`) reaches a destination of type `T[:]` (a function parameter, annotated variable, struct field, or return value), it coerces to a full slice automatically—no `&` or `[..]` needed.
- **Growth stability:** Taking a slice of a `T[>..]` before calling `.push(...)` leaves the slice completely valid; its `.len` simply keeps the length it was created with.

```goose
fn sum(xs: const i64[:]) -> i64 {
    var total = 0;
    for x in xs { total += x; }
    return total;
}

fn main() {
    let fixed: i64[4] = [1, 2, 3, 4];
    var grow: i64[>..] = [10, 20, 30, 40, 50];

    let mid = grow[1..4];            // [20, 30, 40]
    let last_two = grow[^2..];       // [40, 50]
    let trim_ends = grow[1..^1];     // [20, 30, 40]

    // `sum` accepts every array kind and any slice without copying:
    print("fixed=", sum(fixed), " grow=", sum(grow), " mid=", sum(mid),
          " last_two=", sum(last_two), " trim=", trim_ends);

    // Growing `grow` does not invalidate `mid` or change its length:
    grow.push(60);
    print("mid after push=", mid, " grow.len=", grow.len);
}
```

```
fixed=10 grow=150 mid=90 last_two=90 trim=[20, 30, 40]
mid after push=[20, 30, 40] grow.len=6
```

Just like arrays, slices of fixed-size elements support both indexing (`s[i]`, bounds-checked against `s.len`) and iteration, whereas slices of variable-size elements (`u8[][:]`) support iteration only.

---

## 5.2 Transparent References (`T&`), `.=`, and `.==`

A reference `T&` is a single 8-byte machine address with three guarantees: **it is never null**, **it supports no pointer arithmetic or unsafe casts**, and **it cannot dangle**.

### Transparency

In Goose, references are **transparent**: wherever you use a reference in an ordinary expression, it behaves directly as the value it points to. There is no `*` dereference operator and no `->` arrow operator:

- `r + 1` reads the target and adds `1`.
- `r.field` accesses a field of the target.
- `r = 99` and `r += 5` **write through** to the target.
- `r1 == r2` compares the **target values**.
- `print(r)` prints the target value.

Because `=` and `==` always operate on the *target*, Goose provides two clean, unmistakable operators when you want to operate on the **reference itself**:

| Operator | Meaning | Example |
|---|---|---|
| `=` | Copy or assign the **target value** | `r = 11;` (modifies the element `r` points to) |
| `.=` | **Bind or rebind the reference** to a target | `var r .= xs[0];` then `r .= xs[2];` |
| `==` / `!=` | Compare **target values** for equality | `r == xs[2]` (`true` if values match) |
| `.==` / `.!=` | Compare **reference identity** (same address?) | `r .== xs[2]` (`true` only if same element) |

```goose
fn main() {
    var xs: i64[>..] = [10, 20, 10];
    var r .= xs[0];                  // `r` refers to xs[0]
    var c = xs[0];                   // `c` is a fixed-size copy of 10

    r = 11;                          // writes xs[0]
    c = 99;                          // writes only the local copy `c`
    print("after write: xs=", xs, " c=", c);

    // Value equality vs. identity equality:
    xs[2] = 11;
    print("r == xs[2]: ", r == xs[2], "  r .== xs[2]: ", r .== xs[2]);

    // Rebind `r` to point at xs[2], then mutate through it:
    r .= xs[2];
    r += 100;
    print("after rebind: xs=", xs, " r .== xs[2]: ", r .== xs[2]);
}
```

```
after write: xs=[11, 20, 10] c=99
r == xs[2]: true  r .== xs[2]: false
after rebind: xs=[11, 20, 111] r .== xs[2]: true
```

### Binding, Implicit `&`, and Decay Rules

Two small rules keep reference code crisp and free of noise:

1. **No redundant `&` when the destination is already `T&`:** When calling `fn inc(n: i64&)`, assigning an annotated `let r: i64& = count;`, or filling a reference field in a struct literal, pass the lvalue directly without `&` (`inc(count)`). Writing `inc(&count)` works, but Goose warns that the `&` is redundant.
2. **Inferred variables decay fixed-size references to copies:** Because references are transparent, writing `let d = r;` on a reference `r: i64&` creates a *copy* of the `i64`. To bind a new reference with type inference, use `.=` (`let a .= r;`) or explicit `&` (`let a = &n;`). Non-fixed lvalues (`u8[>..]`, `Order`) never copy implicitly, so `let alias = xs;` always binds by reference.

```goose
fn inc(n: i64&) { n += 1; }

struct CounterRef { target: i64& }

fn main() {
    var count = 0;
    inc(count);                      // binds by reference without `&`
    let ref_a: i64& = count;         // annotated reference
    let ref_b .= count;              // inferred reference via `.=`
    let ref_c = &count;              // inferred reference via `&`
    let snapshot = ref_a;            // fixed-size reference decays to a copy!

    let holder = CounterRef { target: count };
    ref_a += 10;
    ref_b += 100;
    ref_c += 1000;
    holder.target += 10_000;

    print("count=", count, " snapshot=", snapshot);
}
```

```
count=11111 snapshot=1
```

> **Watch Out:** Taking `&` of a temporary expression (like `&(a + b)`) is a compile error, and there are no references-to-references (`&r` simply yields the stored reference). Also remember: `let r .= xs[0];` prevents *rebinding* `r` with `.=`, but `r = 5;` still writes to `xs[0]` unless `r` is `const`.

---

## 5.3 Optionals (`T?`) and Flow-Sensitive Narrowing

Because `T&` is never null, how do you represent "maybe an element" (such as a search result, a dictionary lookup, or a tree node's left child)?

With **`T?`**—an **optional reference**. Under the hood, `T?` is simply an 8-byte reference where address `0` represents `null`, so optionality adds **zero extra bytes**. Writing `T&?` means the exact same type as `T?`, and any optional struct field omitted from a literal defaults automatically to `null`.

### Narrowing Before Use

Unlike `T&`, an optional `T?` is **not transparent**. You cannot read fields, perform arithmetic, or assign through a `T?` directly—doing so is always a compile error:

<!-- compile-fail -->
```goose
fn main() {
    var x = 10;
    let opt: i64? = x;
    print(opt + 1);                  // error: optional must be narrowed before use
}
```

To use an optional, you **narrow** it in one of four built-in ways. Inside the narrowed region, the compiler treats the variable as a plain non-null `T&`:

1. **`if opt { ... }`** — narrowed inside the `then` branch (or in `else` for `if opt == null { ... } else { ... }`).
2. **`guard opt else { return ...; }`** — narrowed for the entire rest of the enclosing block!
3. **`while cur { ... cur .= cur.next; }`** — narrowed inside the loop body, re-tested before every iteration.
4. **`assert(opt);`** — aborts if null, narrowed for the rest of the block.

```goose
fn find_over(xs: i64[:], threshold: i64) -> i64? {
    for &x in xs {
        if x > threshold { return x; }
    }
    return null;
}

fn boost_first_over(xs: i64[:], threshold: i64, bonus: i64) -> bool {
    let hit = find_over(xs, threshold);
    guard hit else { return false; } // narrowed to `i64&` from here down!
    hit += bonus;                    // mutates the found element directly in `xs`
    return true;
}

struct Node { val: i64, next: Node? }

fn main() {
    var nums: i64[>..] = [3, 12, 7, 25];
    let ok1 = boost_first_over(nums, 10, 100);
    let ok2 = boost_first_over(nums, 500, 100);
    print("ok1=", ok1, " ok2=", ok2, " nums=", nums);

    // Walking a linked chain with `while cur`:
    var n3 = Node { val: 30 };       // `next` defaults to `null`
    var n2 = Node { val: 20, next: n3 };
    var n1 = Node { val: 10, next: n2 };

    var cur: Node? = n1;
    var sum = 0;
    while cur {
        sum += cur.val;              // narrowed to `Node&` inside the loop body
        cur .= cur.next;
    }
    print("chain sum=", sum);
}
```

```
ok1=true ok2=false nums=[3, 112, 7, 25]
chain sum=60
```

Notice how `boost_first_over` mutates `nums[1]` in place through `hit` without ever copying an element or computing an index—and because `find_over` returned a nullable reference (`i64?`), there is no sentinel `-1` magic value to confuse with real data.

---

## 5.4 Writability and `const`

In Chapter 2, you saw that `let` locks a binding while `const` locks contents. For references and slices, `const` controls **writability through the indirection**:

- `const T&` — a reference through which you may read `T`, but never mutate it.
- `const T[:]` — a slice through which you may read elements, but never overwrite them.

### What Is Read-Only?

The compiler tracks writability statically for every reference and slice:

- **Read-only:** String literals (`"hello"` is `const u8[:]`), `&x` or `x[..]` of a `const` variable, by-value `for` or `match` bindings, views of temporaries, `bytes_of(...)`, and anything read out of a `const` slot.
- **Writable:** Everything else—including `&x` and `x[..]` of both `var` and ordinary `let` variables.

Adding `const` is always implicit (you can pass a writable `u8[:]` to a `const u8[:]` parameter). Dropping `const` is **never** allowed.

### Parameters Are Generic Over Constness; Slots Are Explicit

One subtle and remarkably convenient Goose feature is how functions handle `T[:]` and `T&` parameters:

- A parameter written **`u8[:]`** (without `const`) accepts *both* writable and read-only arguments! The compiler checks the function body against each call's actual writability: if the function only reads, passing a string literal succeeds; if the function writes, passing a string literal fails at that call site.
- A parameter written **`const u8[:]`** is read-only inside the function body no matter what the caller passed—documenting and enforcing that the function never mutates its input.
- **Stored slots** (struct fields, array elements, globals, and annotated variables) must explicitly declare `const` if you want them to hold read-only references or string literals.

```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 && s[0] >= 'a' && s[0] <= 'z' {
        s[0] -= 32;
    }
}

struct Tag { label: const u8[:] }    // `const` slot: can hold string literals OR mutable views

fn main() {
    var word: u8[>..] = [];
    word.append("goose");
    capitalize_first(word);          // writable argument: succeeds

    let t1 = Tag { label: "static-tag" };
    let t2 = Tag { label: word };    // writable coerces to `const u8[:]` implicitly
    let chosen = if word.len > 3 { t1.label } else { word[..] }; // read-only (one branch is const)

    print("word=", word, " t1=", t1.label, " t2=", t2.label, " chosen=", chosen);
}
```

```
word=Goose t1=static-tag t2=Goose chosen=static-tag
```

If you try to pass a read-only string literal into `capitalize_first`, Goose catches the mutation attempt at compile time:

<!-- compile-fail -->
```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 && s[0] >= 'a' && s[0] <= 'z' {
        s[0] -= 32;
    }
}

fn main() {
    capitalize_first("immutable");   // error: cannot write through read-only slice
}
```

> **Syntax Note:** `const` is a prefix that qualifies the *first* reference or slice on the base type (`const u8[:]` is a read-only slice of bytes). To apply `const` to an *outer* reference around a slice variable, add parentheses: `const (u8[:])&` means a read-only reference to a writable slice variable. Note also that `const` is **shallow**: reading a non-const reference field out of a `const Box&` still yields that field's own writability.

---

## 5.5 Worked Example: An In-Place Log Index with Chained Back-Links

Let us combine slices, transparent references (`.=`, `.==`), optional narrowing (`guard`, `if`, `while`), and `const` views in a practical example: indexing a multi-line log buffer **without copying a single byte of text**.

Each `Entry` stores `const u8[:]` views straight into the raw log text, a mutable `priority: i32`, and an optional `prev: Entry?` linking back to the previous log entry for the same service. Because `entries` is a grow-only `Entry[>..]`, those `Entry?` links stay valid across every `push`!

```goose
import std;

struct Entry {
    id: i32,
    service: const u8[:],
    msg: const u8[:],
    priority: i32,
    prev: Entry?,
}

fn find_latest(entries: Entry[:], svc: const u8[:]) -> Entry? {
    // Scan backward to find the most recent entry for `svc`.
    var i = entries.len - 1;
    while i >= 0 {
        if entries[i].service == svc { return entries[i]; }
        i--;
    }
    return null;
}

fn escalate(entries: Entry[:], svc: const u8[:], bump: i32) -> bool {
    let latest = find_latest(entries, svc);
    guard latest else { return false; }

    // Walk all entries for this service through the `prev` chain and boost priority:
    var cur: Entry? = latest;
    while cur {
        cur.priority += bump;
        cur .= cur.prev;
    }
    return true;
}

let RAW_LOG = """
    auth:login ok
    db:pool opened
    auth:token refreshed
    http:200 /health
    auth:login failed
    """;

fn main() {
    var entries: Entry[>..] = [];

    each_split(RAW_LOG, '\n') {
        guard it.len > 0;
        let colon = find(it, ":");
        guard colon > 0;
        let svc = it[..colon];
        let msg = it[colon + 1..];
        let prev = find_latest(entries, svc);
        entries.push(Entry {
            id: entries.len as i32,
            service: svc,
            msg: msg,
            priority: 1,
            prev: prev,
        });
    };

    let changed = escalate(entries, "auth", 5);
    let missing = escalate(entries, "cache", 5);
    print("escalated auth=", changed, " cache=", missing);

    // Inspect the linked chain for "auth" from newest to oldest:
    let head = find_latest(entries, "auth");
    assert(head);                    // narrowed to `Entry&` for the rest of `main`
    print("head index=", entries.index_of(head),
          " is last element? ", head .== entries[^1..][0]);

    var cur: Entry? = head;
    while cur {
        print("  #", cur.id, " [p=", cur.priority, "] ", cur.service, ": ", cur.msg);
        cur .= cur.prev;
    }
}
```

```
escalated auth=true cache=false
head index=4 is last element? true
  #4 [p=6] auth: login failed
  #2 [p=6] auth: token refreshed
  #0 [p=6] auth: login ok
```

Look at what happened here with **zero heap allocations, zero string copies, and zero lifetime annotations**:
- Every `service` and `msg` field is a `const u8[:]` slicing directly into `RAW_LOG`.
- Every `prev` field is a typed `Entry?` pointing straight at an earlier element of `entries`, surviving every subsequent `entries.push(...)`.
- `escalate` mutates all three `"auth"` entries in place through narrowed optional references, and `entries.index_of(head)` recovers `head`'s exact index (`4`) directly from its reference!

---

## Exercises

1. **Test `const` vs. writable slices:** In the Section 5.5 worked example, change `struct Entry`'s `msg` field from `const u8[:]` to `u8[:]`. Click **▶ Run** and read the exact compiler error explaining why `RAW_LOG` cannot be stored in a non-const slot.
2. **Add a search-and-count helper:** Write `fn count_chain(start: Entry?) -> i64` that walks `cur .= cur.prev` using `while cur` and returns the length of the chain (returning `0` for `null`). Call it for `"auth"`, `"db"`, and `"cache"`.
3. **Explore `.=` vs. `=`:** Write a short program with two `var a = 1; var b = 2;` locals and `var r .= a;`. Show in one program the difference between `r = b;` and `r .= b;`, printing `a`, `b`, `r`, and `r .== b` after each step.

---

## Key Takeaways

- Slices `T[:]` are universal zero-copy views across every array kind, supporting half-open `a[x..y]` and end-relative `a[^k..]` bounds.
- References `T&` are non-null, transparent 8-byte addresses: use `=` / `==` for target values and `.=` / `.==` for reference binding and identity.
- Optionals `T?` are zero-overhead nullable references (`null` is address `0`) that must be narrowed via `if`, `while`, `guard`, or `assert` before use.
- Writability is controlled by `const`: functions taking `T[:]` adapt to read-only or writable arguments per call site, while `const T[:]` documents read-only access and stored slots require `const` to hold read-only data.

So far, references and slices have "just worked"—without a single lifetime annotation. In **Chapter 6: Lifetimes Without Annotations**, we lift the hood on **roots, candidates, shrink rules, and balanced calls** so you understand exactly what the compiler checks and how to fix any lifetime or shrink diagnostic in seconds.
