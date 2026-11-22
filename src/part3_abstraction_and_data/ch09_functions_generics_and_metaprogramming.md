# Chapter 9: Functions, Generics, and Compile-Time Expansion

In many languages, reaching for clean abstractions—generic utilities, higher-order iterators like `filter` and `map`, or structural struct inspection—comes with a quiet performance bill: heap-allocated closures, indirect function-pointer calls, trait-object vtables, or runtime reflection metadata.

In Goose, **every one of those abstractions disappears at compile time**. Because Goose compiles the whole program together and monomorphizes every generic and function value, `xs.filter() { it % 2 == 0 }` compiles to the exact loop you would write by hand, building its result directly in the caller's destination—and `const for` expands over struct fields or enum variants into plain, typed Goose code with zero runtime metadata.

By the end of this chapter, you will be able to:

- Use static overloading, nested functions with free variables, and function qualifiers cleanly.
- Write monomorphized generic functions (both untyped and explicit `<T>`) and understand literal parameters and generic `.=`.
- Write and call higher-order functions using compile-time **function values**, trailing `{ ... }` blocks, and non-local `return`.
- Generate zero-overhead structural code with **`const for`** and **`const if`** (`fields<T>`, `variants<E>`, `types<...>`, `same`, `has_default`).

---

## 9.1 Overloading, Nested Functions, and Qualifiers

### Static Overloading

Functions overload by parameter types, resolved entirely at compile time (never by expected return type). For each argument, Goose ranks candidates from best to worst:

1. Exact type match
2. Generic exact match
3. Coercion (array → slice, literal adaptation, implicit widening)
4. Integer → float conversion (lowest priority)

A candidate's overall rank is its *worst* argument rank; ties are compile errors.

### Nested Functions

You can declare helper functions right inside another function. A nested function reads and writes its enclosing function's locals as free variables under ordinary lifetime rules—making it ideal for stateful helpers or parsers that share local tables without threading a giant context struct through every call.

Sibling nested functions in the same block can call each other in either order and even overload each other (including nested case functions). Like function values, nested functions are compile-time entities and cannot escape their enclosing scope.

```goose
fn describe(x: i64) -> u8[>..]     { str("int(", x, ")") }
fn describe(x: f64) -> u8[>..]     { str("float(", x, ")") }
fn describe(xs: i64[:]) -> u8[>..] { str("slice[", xs.len, "]") }

struct Employee { name: u8[..12], dept: u8[..8], salary: i32 }

fn main() {
    let nums: i64[3] = [4, 8, 15];
    print(describe(7), " | ", 2.5.describe(), " | ", nums.describe());

    var staff: Employee[>..] = [];
    staff.push(Employee { name: "ada",   dept: "eng", salary: 120 });
    staff.push(Employee { name: "linus", dept: "ops", salary: 90 });
    staff.push(Employee { name: "grace", dept: "eng", salary: 135 });

    // Nested function directly updates `main`'s locals `budget` and `count`:
    var budget = 0;
    var count = 0;
    fn tally(e: Employee&) {
        budget += e.salary;
        count++;
    }

    for &e in staff {
        if e.dept == "eng" { tally(e); }
    }
    print("eng budget=", budget, " over ", count, " people");
}
```

```
int(7) | float(2.5) | slice[3]
eng budget=255 over 2 people
```

### Function Qualifiers at a Glance

As you progress through the book, you will meet seven function qualifiers. Whenever you combine them, they appear in a fixed order:

```text
export | extern  ->  checkfree  ->  simd  ->  inline  ->  recursive  ->  fn | thread_fn
```

We cover `inline` and `simd` in Chapter 19, `checkfree` in Chapter 18, `recursive` in Chapter 11, `thread_fn` in Chapter 14, and `extern` / `export` in Chapter 15.

---

## 9.2 Monomorphized Generics Without Trait Bounds

In Goose, **any untyped parameter is automatically generic**: `fn twice(x) { x + x }` works for integers, floats, numeric structs, and fixed numeric arrays alike.

Use an explicit type parameter list `<T>` when:
- Two parameters must share the **same** type (`fn span<T>(a: T, b: T) -> T`), or
- A type parameter does not appear in any argument (`fn zero<T>() -> T { default<T>() }`).

Goose monomorphizes every generic function per concrete call site and typechecks the instantiated body directly—so there are **no trait or interface bounds** to declare. If an instantiation attempts an operation that concrete type does not support, the compiler reports the error at that exact call chain.

Three subtle rules make Goose generics noticeably ergonomic:

1. **Literal parameters:** If only a literal binds `T`, it adapts inside the specialization—for instance, `push_n(bytes, 1, 4)` pushes `1` as a `u8` into `u8[>..]` with no cast, while `push_n(bytes, 300, 4)` fails cleanly at compile time.
2. **Floating context:** When a generic function returns a bare `T` from float literals/integers, an `f32` destination refines the whole expression to 32-bit float math (`let s: f32 = max(0.5, min(w / 960.0, h / 600.0));`).
3. **Generic `.=`:** Inside a generic function, `a .= b` performs a normal value assignment when `T` is a value type, and rebinds the *reference itself* when `T` is a reference type! That lets generic algorithms like `swap_ends` or `std`'s `sort` reorder arrays of values *and* arrays of references with one implementation.

```goose
fn twice(x) { x + x }

fn span<T>(a: T, b: T) -> T {
    if a > b { a - b } else { b - a }
}

fn zero<T>() -> T { default<T>() }

// Generic `.=` moves whole elements whether T is a value (i64) or a reference (i64&)!
fn swap_ends<T>(xs: T[:]) {
    guard xs.len >= 2;
    let last = xs.len - 1;
    let tmp: T = xs[0];              // holds a value for T=i64, or a reference for T=i64&
    xs[0] .= xs[last];
    xs[last] .= tmp;
}

fn main() {
    print(twice(21), " ", twice(1.5), " ", span(3, 8), " ", zero<f64>() + 0.5);

    var vals: i64[>..] = [10, 20, 30];
    swap_ends(vals);

    var refs: (i64&)[>..] = [vals[0], vals[1], vals[2]];
    swap_ends(refs);                 // swaps the references, NOT the underlying ints!

    print("vals=", vals, " refs=", refs);
}
```

```
42 3.0 5 0.5
vals=[30, 20, 10] refs=[10, 20, 30]
```

Look closely at that last line: `swap_ends(refs)` reversed the outer references (`refs` now points to `[10, 20, 30]`) while leaving `vals` untouched at `[30, 20, 10]`, using the exact same generic body!

---

## 9.3 Function Values, Trailing Blocks, and Non-Local `return`

Higher-order functions in Goose take a **function value** as a generic parameter `F` (you may optionally write a signature hint like `F: fn(i64)` as documentation):

- Every function value is a **compile-time entity**, never runtime data.
- You pass either a **trailing block** `{ ... }` right after the call's parentheses (with implicit parameter `it` or named parameters `a, b => ...`), or a **named function** (which carries its entire overload set and parameter defaults).
- Every call to `F(...)` is direct and inlined into the caller—there are **no closure objects, no heap allocations, and no indirect function pointers**.
- A block can read and write locals of its enclosing function freely.
- **Non-local `return`:** Executing `return expr;` inside a trailing block returns from the **lexically enclosing named function**, not from the higher-order function! That means any custom iterator you write has the exact same early-exit power as a built-in `for` loop.

```goose
import std;

fn each_pair<T, F>(xs: T[:]) {
    for i in 0..xs.len - 1 {
        F(xs[i], xs[i + 1]);
    }
}

fn each_step<F>(lo: i64, hi: i64, step: i64 = 1) {
    var i = lo;
    while i < hi {
        F(i);
        i += step;
    }
}

fn first_multiple_of_7(lo: i64, hi: i64) -> i64 {
    each_step(lo, hi, 3) {
        if it % 7 == 0 { return it; } // returns directly from `first_multiple_of_7`!
    };
    return -1;
}

fn desc(a: i64, b: i64) -> bool { a > b }

fn main() {
    let xs: i64[5] = [4, 8, 15, 16, 23];

    var gaps: i64[>..] = [];
    each_pair(xs) { a, b => gaps.push(b - a); };

    // Built directly in place with zero intermediate temporary arrays:
    let evens = xs.filter() { it % 2 == 0 };
    let total = fold(xs, 0) { acc, x => acc + x };
    sort(gaps, desc);                // pass a named function as F

    print("gaps(desc)=", gaps, " evens=", evens, " total=", total);
    print("first mult of 7: ", first_multiple_of_7(10, 40), " / ", first_multiple_of_7(10, 12));
}
```

```
gaps(desc)=[7, 7, 4, 1] evens=[4, 8, 16] total=66
first mult of 7: 28 / -1
```

Because function values are compile-time entities, **they cannot escape**—trying to store one in a local variable, put it in a struct, or return it from a function is rejected at compile time:

<!-- compile-fail -->
```goose
fn double(x: i64) -> i64 { x * 2 }

fn main() {
    let f = double;                  // error: function values cannot be stored as data
    print(f(3));
}
```

> **Design Trade-Off:** Disallowing escaping closures and function pointers is what lets Goose know the complete call graph statically—enabling static data-stack assignment, zero-annotation root inference, and guaranteed inlining. When you need runtime dynamic dispatch over a closed set of behaviors, use an `enum` + case functions (Chapter 7).

---

## 9.4 Specialization-Time Expansion: `const for` and `const if`

Hand-writing boilerplate that walks every field of a struct (for printing, diffing, resetting defaults, or serialization) is tedious and error-prone: add one field later and forget to update the helper, and you have a silent bug.

Goose solves this with **`const for`** and **`const if`**—compile-time expansion inside ordinary generic functions, with **no textual macros and no runtime reflection overhead**.

### The Four `const for` Sources

| Source | Bindings | Expands Over |
|---|---|---|
| `fields<T>` | `<N, A>` | Every direct field of struct or variant `T`: `N` is the field name (`const u8[:]`), `A` is a field accessor (`v.A` and `T { A: ... }`) |
| `variants<E>` | `<N, V>` | Every variant of enum `E`: `N` is the variant name, `V` is the variant (usable in `match` arms, `fields<V>`, and constructors) |
| `types<T1, T2, ...>` | `<U>` | An explicit sequence of types |
| `[c1, c2, ...]` | `<C>` | An explicit sequence of compile-time constants |

`const for` and `const if` can appear in three places: **statement lists**, **struct constructor initializer lists**, and **`match` arm lists**.

### `const if` and Queries (`same`, `has_default`)

`const if` selects one branch during specialization and **never typechecks the unchosen branches**—so a branch can perform operations valid only for `i64` or only for a specific struct without failing when specialized for other types! Chain branches with `else const if ... else { ... }`.

Two built-in compile-time queries are available in `const if` conditions:
- `same<T1, T2>` — `true` iff `T1` and `T2` are identical types after substitution (including `const`).
- `has_default<A>` — `true` iff field accessor `A` (from `fields<T>`) declared an explicit `= default` in the struct definition.

```goose
struct Config {
    host: u8[..16],
    port: i32 = 8080,
    retries: i32 = 3,
}

enum Shape {
    Circle { r: i64 },
    Rect { w: i64, h: i64 },
    Dot,
}

// Reset every field that declared a default back to its default, keeping others!
fn reset_defaults<T>(src: T&) -> T {
    T {
        const for <N, A> in fields<T> {
            const if !has_default<A> { A: src.A, }
        },
        ..
    }
}

// Generic enum + payload printer using nested `variants<E>` and `fields<V>`:
fn inspect_enum<E>(v: E) {
    match v {
        const for <N, V> in variants<E> {
            V payload => {
                print("variant ", N);
                const for <F, A> in fields<V> {
                    print("  ", F, " = ", payload.A);
                }
            },
        }
    }
}

fn type_tag<T>(x: T) -> const u8[:] {
    const if same<T, i64> { return "i64"; }
    else const if same<T, f64> { return "f64"; }
    else { return "other"; }
}

fn main() {
    var cfg = Config { host: "api.local", port: 9000, retries: 10 };
    let clean = reset_defaults(cfg);
    print("reset: ", clean);

    let s: Shape = Shape.Rect { w: 4, h: 9 };
    inspect_enum(s);

    var sizes: i64[>..] = [];
    const for <C> in [1, 2, 4] {
        let buf: u8[C] = [0; C];
        sizes.push(buf.len);
    }
    const for <U> in types<i64, f64, bool> {
        print("tag=", type_tag(default<U>()), " zero=", default<U>());
    }
    print("sizes=", sizes);
}
```

```
reset: Config { "api.local", 8080, 3 }
variant Rect
  w = 4
  h = 9
tag=i64 zero=0
tag=f64 zero=0.0
tag=other zero=false
sizes=[1, 2, 4]
```

Look how clean `reset_defaults<T>` and `inspect_enum<E>` are: zero runtime loops over reflection tables, zero heap allocations, and completely type-checked for every concrete `T` and `E`!

---

## 9.5 Worked Example: A Zero-Boilerplate Struct Diff & Validator

Let us combine Chapter 9's ideas—monomorphized generics, trailing blocks with non-local `return`, `const for` over `fields<T>`, and `const if` (`has_default<A>`, `same`)—into a tiny reusable diagnostics toolkit that works on **any** struct automatically:

- `diff_fields<T>(a, b)` prints every field whose value changed between `a` and `b` and returns the count of differences.
- `format_kv<T>(v)` renders any struct as `key=val; ...`, marking fields that have declared defaults with `*`.
- `find_first_invalid<T, F>(items)` scans a slice using a validation block and uses non-local `return` to bail out at the first failing record.

```goose
import std;

fn diff_fields<T>(a: T&, b: T&) -> i64 {
    var diffs = 0;
    const for <N, A> in fields<T> {
        if a.A != b.A {
            print("  changed ", N, ": ", a.A, " -> ", b.A);
            diffs++;
        }
    }
    return diffs;
}

fn format_kv<T>(v: T&) -> u8[>..] {
    var out: u8[>..] = [];
    var first = true;
    const for <N, A> in fields<T> {
        if !first { out.append("; "); }
        first = false;
        format(out, N);
        const if has_default<A> { out.push('*'); }
        format(out, "=", v.A);
    }
    return out;
}

fn each_with_index<T, F>(xs: T[:]) {
    for &x, i in xs { F(x, i); }
}

fn first_failing_index<T, F>(xs: T[:]) -> i64 {
    each_with_index(xs) { item, idx =>
        if !F(item) { return idx; }  // non-local return out of `first_failing_index`!
    };
    return -1;
}

struct Server {
    name: u8[..16],
    port: i32 = 443,
    workers: i32 = 4,
    tls: bool = true,
}

fn main() {
    var s1 = Server { name: "edge-1", .. };
    var s2 = Server { name: "edge-1", port: 8443, workers: 8, tls: true };

    print("s1: ", format_kv(s1));
    print("s2: ", format_kv(s2));
    print("diffing s1 vs s2:");
    let n = diff_fields(s1, s2);
    print("total diffs: ", n);

    var fleet: Server[>..] = [
        Server { name: "edge-1", .. },
        Server { name: "edge-2", port: 80, workers: 0, tls: false },
        Server { name: "edge-3", .. },
    ];
    let bad = first_failing_index(fleet) { it.workers > 0 && it.port > 0 };
    print("first invalid server index: ", bad, " (", fleet[bad].name, ")");
}
```

```
s1: name=edge-1; port*=443; workers*=4; tls*=true
s2: name=edge-1; port*=8443; workers*=8; tls*=true
diffing s1 vs s2:
  changed port: 443 -> 8443
  changed workers: 4 -> 8
total diffs: 2
first invalid server index: 1 (edge-2)
```

Add a fifth field `region: u8[..8] = "us-east"` to `struct Server` and both `format_kv` and `diff_fields` pick it up automatically at compile time—with zero runtime cost!

---

## Exercises

1. **Test automatic reflection:** In Section 9.5, add `region: u8[..8] = "us-east"` to `struct Server` and change `s2.region` to `"eu-west"`. Click **▶ Run** and confirm `format_kv` and `diff_fields` both reflect the new field without changing a single line of either helper.
2. **Write a generic `sum_ints<T>` with `const if`:** Using `const for <N, A> in fields<T>` and `const if same<...>` (or checking numeric fields), write a helper `fn count_fields<T>() -> i64` and `fn count_defaulted<T>() -> i64` and test them on `Config` and `Server`.
3. **Custom iterator with non-local `return`:** Write `fn each_window2<T, F>(xs: T[:])` that calls `F(xs[i..i + 2])` for each adjacent 2-element slice, then write `fn first_descending_pair(xs: i64[:]) -> i64` that uses `each_window2` and a non-local `return` to return the first element where `it[0] > it[1]`.

---

## Key Takeaways

- Functions overload statically by argument types, and **nested functions** share their enclosing function's locals cleanly without extra structs.
- Generics (`fn f(x)` or `fn f<T>(...)`) monomorphize per concrete call site with **no trait bounds**, adapting literal parameters and unifying value/reference moves via generic `.=`.
- **Function values** (`F` + trailing `{ it }` blocks) are compile-time entities: direct, inlinable, zero-allocation, non-escaping, and supporting **non-local `return`** out of the lexically enclosing function.
- **`const for`** (`fields<T>`, `variants<E>`, `types<...>`, `[c1, ...]`) and **`const if`** (`same`, `has_default`) give you zero-overhead compile-time reflection without macros or runtime metadata.

Next, in **Chapter 10: Error Handling and Long-Distance Returns**, we see how Goose uses the exact same zero-unwinder call-graph machinery behind non-local `return` to power **`return E from f`**—clean deep error handling without `Result`, `?` noise, or exceptions!
