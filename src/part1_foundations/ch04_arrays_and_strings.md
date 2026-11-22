# Chapter 4: The Array Family and Strings

In most languages, you get one dynamic array (`std::vector<T>`, `Vec<T>`, `[]T`) and one string type (`std::string`, `String`, `string`), and every one of them stores a pointer to a separate heap allocation. If you put a string inside a struct, and put that struct inside an array, you end up with a tree of pointers scattered across the heap.

Goose takes a cleaner approach: **there is no single array type, and there is no dedicated string type.** Instead, Goose gives you a unified **array family** whose members differ only in how their size behaves—and every string is simply an array (or slice) of `u8` bytes. Picking the array type *is* picking the exact memory layout.

By the end of this chapter, you will be able to:

- Choose the right array type from Goose's array family (`T[k]`, `T[]`, `T[varint]`, `T[..k]`, `T[..]`, `T[>..]`, `T[>..<]`) for any data layout.
- Use built-in array operations (`.push`, `.append`, `.pop`, `.resize`, `.clear`, `.move_from`, structural `==`) and understand placement rules.
- Pack variable-size strings and nested arrays inline with zero pointers, and work comfortably with **sequential** arrays.
- Build, slice, parse, search, UTF-8-iterate, and custom-`format` text using `u8` arrays and `std`.

---

## 4.1 The Array Family at a Glance

Every non-resizable array in Goose is stored inline as `[metadata][elements...]` right inside whatever holds it—a local variable, a struct field, or an element of another array—with **no pointer** to an external buffer. The two resizable kinds keep their elements on top of a data stack (Chapter 3) and store only their length in the owning frame.

| Type | Size Class | Behavior | Stored Metadata |
|---|---|---|---|
| `T[k]` | fixed | Exactly `k` elements (`k` compile-time constant) | None |
| `T[]` | variable | Sized at construction, frozen forever after | `u32` length |
| `T[u8]`, `T[u16]`, `T[u32]`, `T[u64]` | variable | Frozen after construction, with chosen length width | Length of that exact integer type |
| `T[varint]` | variable | Frozen after construction, compact variable-width length | 1 byte up to 127 elements, 2 up to 16,383 |
| `T[..k]` | fixed | Inline capacity `k`; grows and shrinks within `0..=k` | Length in smallest unsigned type holding `k` (`u8` up to 255, `u16` up to 65,535, ...) |
| `T[..]` | variable | Runtime capacity chosen at construction; grows/shrinks within it | `u32` capacity + `u32` length |
| `T[>..]` | resizable | **Grow-only** (the default workhorse); references survive growth | Length (in owning frame) |
| `T[>..<]` | resizable | **Grow-shrink** (stacks, queues, heaps); shrinks from anywhere | Length (in owning frame) |

Here are all of them working side by side:

```goose
fn main() {
    var fixed: i32[4] = [1, 2, 3, 4];
    let zeros: f64[8] = [0.0; 8];           // fill literal: 8 copies of 0.0
    let frozen: i64[] = [7, 8, 9];          // variable class, frozen length
    let tiny: u8[varint] = "hi";            // 1-byte varint length + 2 bytes

    var small: u8[..8] = [];                // fixed-size! capacity 8 lives inline
    small.push('h');
    small.push('i');

    var bounded: i64[..] = [..16];          // runtime capacity 16, length 0
    bounded.push(42);

    var grow: i64[>..] = [];                // grow-only workhorse
    for i in 5 { grow.push(i * i); }

    var stack: i64[>..<] = [];              // grow-shrink stack
    stack.push(10);
    stack.push(20);
    let top = stack.pop();

    print(fixed, " zeros=", zeros.len, " frozen=", frozen, " tiny=", tiny);
    print("small=", small, " (", small.len, "/", small.cap, ") bounded.cap=", bounded.cap);
    print("grow=", grow, " stack=", stack, " top=", top);
}
```

```
[1, 2, 3, 4] zeros=8 frozen=[7, 8, 9] tiny=hi
small=hi (2/8) bounded.cap=16
grow=[0, 1, 4, 9, 16] stack=[10] top=20
```

### Which One Should You Pick?

When in doubt, use this quick rule of thumb:

- **`T[>..]`** — Your **default** dynamic array: builders, collections, node pools, and scratch buffers. Despite the name "grow-only," you *can* still `.clear()`, `.pop()`, or `.resize()` it whenever nothing live points into it (Chapter 6).
- **`T[>..<]`** — Whenever you push and pop freely as a stack, queue, or binary heap. Its trade-off is that references into it may live in local variables only, never stored inside structs or other arrays.
- **`T[..k]`** — Whenever a small growable list or short string belongs **inline inside a struct** (like a 16-byte username or up to 8 neighbor IDs) while keeping the struct fixed-size and indexable.
- **`T[]` or `T[varint]`** — For **finished, read-only-size data** embedded inside records or pools (e.g., an array of strings `u8[][>..]`, or `u8[varint]` inside a compact record).
- **`T[k]`** — For fixed geometry or lookup tables (a `f32[16]` matrix, a `bool[256]` byte table).

> **Memory Model:** Only `T[>..]` and `T[>..<]` are **resizable** and subject to the one-resizable-per-data-stack rule. `T[..k]` is **fixed-size**—so you can put twenty `u8[..16]` fields inside one struct or an array of a million of them on a single stack!

---

## 4.2 Literals, Operations, and Placement Rules

### Array Literals and Type Inference

- `[1, 2, 3]` with no destination type defaults to a fixed array `i64[3]` (or `T[]` if its elements are variable-size).
- `[v; n]` repeats `v` (evaluated once) `n` times, where `n` is a compile-time constant (literals, named constants, or `.len` of a fixed-length global).
- `[..cap]` constructs an empty runtime-capacity `T[..]` with capacity `cap`.
- `var out = [];` with no type annotation creates a **grow-only `T[>..]`** whose element type `T` is inferred from your very first `push`, `append`, `format`, or whole assignment!

### Built-In Operations

All array operations work with method syntax via UFCS (`xs.push(v)` is `push(xs, v)`):

| Operation | Works On | Meaning |
|---|---|---|
| `.len` | All arrays & slices | Current element count (`i64`) |
| `.cap` | `T[..k]`, `T[..]` | Fixed inline capacity (`i64`); pushing past `.cap` aborts |
| `.push(v) -> T&` | Limited, `[>..]`, `[>..<]` | Constructs `v` in place at the end; returns a reference to it |
| `.append(src)` | Limited, `[>..]`, `[>..<]` | Appends every element of an array, slice, or literal |
| `.pop() -> T` | Limited, `[>..<]`, and `[>..]` (when unreferenced) | Removes and returns the last element (aborts if empty) |
| `.pop_n(n)` | Same as `.pop()` | Drops the last `n` elements |
| `.resize(n, v)` / `.resize(n)` | Same as `.pop()` | Grows with copies of `v` (or default `T`), or shrinks to `n` |
| `.clear()` | Same as `.pop()` | Sets length to `0` |
| `.index_of(r) -> i64` | Arrays of fixed-size elements | Turns a reference `r` into its element index |
| `.move_from(src)` | Slices of fixed-size elements | Overlap-safe copy between equal-length slices (`memmove` semantics) |

Growth in Goose **never exposes uninitialized memory**: `.push`, `.append`, and `.resize` always write valid element values.

Equality (`==` and `!=`) compares arrays structurally—length first, then element by element—even across **different array kinds** with the same element type!

```goose
fn main() {
    var xs = [];                         // inferred as i64[>..] from the first push
    xs.push(10);
    xs.append([20, 30, 40]);
    let last .= xs.push(50);
    print("index=", xs.index_of(last), " eq_fixed=", xs == [10, 20, 30, 40, 50]);

    // Overlap-safe copy within the same array via `.move_from`:
    xs[1..4].move_from(xs[0..3]);        // shift elements [10, 20, 30] right by 1
    print("shifted=", xs);

    var st: i64[>..<] = [1, 2, 3, 4];
    st.pop_n(2);
    st.resize(5, 9);
    print("stack=", st);
}
```

```
index=4 eq_fixed=true
shifted=[10, 10, 20, 30, 50]
stack=[1, 2, 9, 9, 9]
```

### Placement Rules: What Can Contain What?

The size classes from Chapter 3 determine exactly what can be nested inside what:

1. **Fixed elements** (`i32`, `Point`, `u8[..16]`, `T&`) can go inside **every** array kind and struct field.
2. **Variable elements** (`u8[]`, `u8[varint]`, `Order`, `Shape..`) can go inside `T[]` and `T[>..]` (making them **sequential**), and at any field position in a struct—but **never** inside `T[>..<]`, `T[..k]`, or `T[..]` (because shrinking or reusing fixed slots must never change the byte layout of an element).
3. **Resizable values** (`T[>..]`, `T[>..<]`) can be locals, globals, parameters, return values, the **last field** of an ordinary struct (making that struct resizable), or any field of a `root struct`—**never** an element of an array.

---

## 4.3 Nested Inline Data and Sequential Arrays

Now let us see why Goose's `varint` and `T[varint]` types produce such dramatically compact layouts.

`varint` is a variable-length integer **storage type** usable in struct fields and array elements (taking 1 to 10 bytes depending on magnitude; reading one yields `i64`, and it is set only at construction). Pair it with `T[varint]` and nested records pack down to raw bytes with zero pointers:

```text
struct Item  { sku: u8[varint], qty: varint, cents: varint }
struct Order { id: varint, customer: u8[varint], items: Item[varint] }

Order { id: 1001, customer: "alice", items: [
    Item { sku: "SKU-441", qty: 2, cents: 1999 },
    Item { sku: "SKU-7",   qty: 1, cents: 500  },
]}

Inline byte layout inside `Order[>..]` (29 bytes total, 0 pointers):
+------+----+-------+----+----+---------+---+------+----+-------+---+------+
| 1001 | 05 | alice | 02 | 07 | SKU-441 | 2 | 1999 | 05 | SKU-7 | 1 | 500  |
+------+----+-------+----+----+---------+---+------+----+-------+---+------+
  2 B   1 B    5 B   1 B  1 B     7 B    1 B  2 B   1 B    5 B   1 B  2 B
```

Every single field—including the customer name and the list of line items and each item's SKU string—sits back-to-back in one cache line.

### The Trade-Off: Sequential Arrays

Because each `Order` (or each `u8[]` string in `u8[][>..]`) can have a **different byte length**, the compiler cannot compute the address of element `i` by multiplying `i * element_size`. Such an array is **sequential**:

- You **can** iterate it in order with `for x in arr` (which binds each element by reference without copying).
- You **cannot** index it with `arr[i]` or call `.pop()`.

Trying to index a sequential array is caught immediately at compile time:

<!-- compile-fail -->
```goose
fn main() {
    var words: u8[][>..] = [];
    words.push("alpha");
    words.push("beta");
    print(words[0]);                 // error: variable-size elements cannot be indexed
}
```

### Best of Both Worlds: Keeping a Side Index of Views

What if you want compact inline storage *and* random access or sorting? Keep the strings packed in one flat `u8[][>..]` and build a lightweight index of slices (`u8[:][>..]`) pointing into it. Sorting the slices reorders the views **without moving a single byte of text**:

```goose
import std;

fn main() {
    var words: u8[][>..] = [];
    words.push("delta");
    words.push("alpha");
    words.push("charlie");
    words.push("bravo");

    var index: u8[:][>..] = [];
    for w in words {
        index.push(w[..]);           // zero-copy view into `words`
    }
    sort(index) { a, b => compare(a, b) < 0 };

    print("sorted views: ", index);
    print("original:     ", words);
    print("2nd sorted:   ", index[1]);
}
```

```
sorted views: ["alpha", "bravo", "charlie", "delta"]
original:     ["delta", "alpha", "charlie", "bravo"]
2nd sorted:   bravo
```

Because `words` never moves on its data stack, every slice inside `index` stays valid automatically—and the compiler proves it with zero lifetime annotations. (In Chapter 12, you will see how 4-byte relative links make indexes even smaller!)

---

## 4.4 Strings Are Just `u8` Arrays

There is no special `String` class in Goose. Every text role maps directly to an array of `u8`:

| Role | Goose Type | Notes |
|---|---|---|
| String literal / borrowed text view | `const u8[:]` (or `u8[:]`) | Zero-copy slice; literals live in read-only static data |
| String builder | `u8[>..]` | Pointer-bump `.append`, `.push`, `format(s, ...)` |
| Finished inline string | `u8[]` or `u8[varint]` | Immutable length, packed inline inside arrays/structs |
| Fixed-capacity small string | `u8[..16]` (any `k`) | Fixed-size! Lives inline in structs without making them sequential |

### Raw Multi-Line Strings

Standard `"..."` literals support escapes (`\n`, `\t`, `\r`, `\0`, `\\`, `\"`, `\'`, `\xNN`). For SQL, GLSL shaders, usage text, or Windows paths, Goose provides **raw strings** delimited by `"""..."""`—no escapes are interpreted, and for multi-line raw strings, the closing `"""`'s indentation is automatically stripped from every content line:

```goose
fn main() {
    let usage = """
        usage: tool [-v] file
          -v   verbose output
        """;
    let path = """C:\data\new "file" \n""";
    print(usage);
    print(path);
}
```

```
usage: tool [-v] file
  -v   verbose output
C:\data\new "file" \n
```

### Rendering Text and Custom `format` Overloads

Three builtins turn any value into text, taking any number of arguments with nothing inserted between them:

- `print(a, b, ...)` — renders to stdout + newline in one atomic write.
- `format(out, a, b, ...)` — appends rendered text into any growable `u8` array (`u8[>..]` or `u8[..k]`).
- `str(a, b, ...)` — returns a fresh `u8[>..]`, built directly in place at its destination.

> **Watch Out:** Character literals like `'a'` are **integer byte constants** (`97`), not 1-character strings! So `str('a')` produces `"97"`, whereas `buf.push('a')` appends the actual byte `'a'`.

To customize how your own struct or enum renders everywhere (`print`, `str`, and `format`, even when nested inside arrays!), define a `format(out: u8[>..]&, v: YourType)` overload:

```goose
struct Person { name: u8[..16], age: i32 }

fn format(out: u8[>..]&, p: Person) {
    format(out, p.name, " (", p.age, ")");
}

fn main() {
    var team: Person[>..] = [];
    team.push(Person { name: "ada", age: 36 });
    var second = Person { name: "", age: 45 };
    format(second.name, "grace");    // `u8[..16]` works as a bounded builder too!
    team.push(second);

    print(team);
    print("first as str: ", str(team[0]), " (char 'a' is ", str('a'), ")");
}
```

```
[ada (36), grace (45)]
first as str: ada (36) (char 'a' is 97)
```

### Everyday Text Operations and UTF-8 (`import std;`)

Importing `std` brings a rich set of zero-copy slice helpers—`trim`, `find`, `starts_with`, `ends_with`, `split`, `each_split`, `words`, `join`, `parse_int`, `parse_flt`, `format_int`, `format_flt`, `format_replaced`, `to_upper`, `to_lower`, and UTF-8 code-point iteration (`each_utf8` / `push_utf8`):

```goose
import std;

fn main() {
    // Zero-copy slicing and parsing:
    let line = "  host = localhost:8080  ";
    let body = trim(line);
    let eq = find(body, "=");
    let key = trim(body[..eq]);
    let val = trim(body[eq + 1..]);
    print(key, " => ", val);

    // Zero-copy split & join:
    let parts = split("10,25,bad,42", ',');
    var sum = 0;
    for p in parts {
        let n, ok = parse_int(p);
        if ok { sum += n; }
    }
    var joined: u8[>..] = [];
    join(joined, parts, " | ");
    print("sum=", sum, " joined=", joined);

    // UTF-8 bytes vs. Unicode code points:
    let greek = "αβγ";
    var codepoints = 0;
    var shifted: u8[>..] = [];
    each_utf8(greek) {
        codepoints++;
        push_utf8(shifted, it + 1);
    };
    print(greek, ": ", greek.len, " bytes, ", codepoints, " code points -> ", shifted);
}
```

```
host => localhost:8080
sum=77 joined=10 | 25 | bad | 42
αβγ: 6 bytes, 3 code points -> βγδ
```

Notice that `split` returns `u8[:][>..]`—an array of views into the original text, copying zero characters—and `each_split` goes one step further by invoking a block per piece without even allocating a slice array.

---

## 4.5 Worked Example: A Zero-Allocation Inline Order Book

Let us put the array family, `varint`, raw strings, custom `format`, sequential iteration, and zero-copy views together into a complete inline order book:

```goose
import std;
import dictionary;

struct Item  { sku: u8[varint], qty: varint, cents: varint }
struct Order { id: varint, customer: u8[varint], items: Item[varint] }
struct Row   { name: u8[:], total: i64 }

struct Money { cents: i64 }
fn format(out: u8[>..]&, m: Money) {
    format(out, "$", m.cents / 100, ".");
    format_int(out, m.cents % 100, 10, 2, '0');
}

let FEED = """
    1001,alice,SKU-441:2:1999;SKU-7:1:500
    1002,bob,SKU-12:10:250
    1003,alice,SKU-7:3:500;SKU-900:1:12999
    1004,carol,SKU-12:2:250;SKU-13:1:275
    """;

fn parse_items(field: u8[:]) -> Item[>..] {
    var items: Item[>..] = [];       // constructed directly inside each Order's `items` field
    each_split(field, ';') {
        let p = split(it, ':');
        assert(p.len == 3);
        let qty, ok1 = parse_int(p[1]);
        let cents, ok2 = parse_int(p[2]);
        assert(ok1 && ok2);
        items.push(Item { sku: p[0], qty: qty, cents: cents });
    };
    return items;
}

fn order_total(o: Order&) -> i64 {
    var total = 0;
    for item in o.items {
        total += item.qty * item.cents;
    }
    return total;
}

fn main() {
    var book: Order[>..] = [];
    each_split(FEED, '\n') {
        guard it.len > 0;
        let f = split(it, ',');
        assert(f.len == 3);
        let id, ok = parse_int(f[0]);
        assert(ok);
        book.push(Order { id: id, customer: f[1], items: parse_items(f[2]) });
    };

    var grand = 0;
    var item_count = 0;
    for o in book {
        grand += order_total(o);
        item_count += o.items.len;
    }
    print(book.len, " orders, ", item_count, " line items, ",
          bytes_of(book).len, " packed bytes, total ", Money { grand });

    // Group totals per customer using zero-copy slices directly into `book`:
    var by_cust = dictionary<u8[:], i64> {};
    for o in book {
        by_cust.update(o.customer[..], 0) { it += order_total(o); };
    }

    var rows: Row[>..] = [];
    by_cust.each() { name, total => rows.push(Row { name: name, total: total }); };
    sort(rows) { a, b => a.total > b.total };

    for r in rows {
        print("  ", r.name, ": ", Money { r.total });
    }
}
```

```
4 orders, 7 line items, 105 packed bytes, total $222.72
  alice: $189.97
  bob: $25.00
  carol: $7.75
```

Stop and appreciate that number: **4 complete orders, 7 line items, 4 customer strings, and 7 SKU strings packed into 105 contiguous bytes with zero heap allocations and zero pointers**, while `by_cust` aggregates per-customer totals using zero-copy slices directly into `book`!

---

## Exercises

1. **Compare `u8[]` vs. `u8[varint]`:** In the Section 4.5 worked example, change `u8[varint]` to `u8[]`, `Item[varint]` to `Item[]`, and `varint` fields to `i64`. Click **▶ Run** and compare the new `bytes_of(book).len` against `105` bytes.
2. **Bounded small-string overflow:** Create a `var tag: u8[..4] = [];` and call `format(tag, "goose");`. Run it in the playground to see the exact runtime abort message when a limited array exceeds its capacity, then resize the capacity to `u8[..8]`.
3. **Custom hex byte dump:** Write a small struct `struct ByteDump { data: const u8[:] }` with a `format(out: u8[>..]&, d: ByteDump)` overload using `format_int(out, b, 16, 2, '0')` to print any slice as space-separated hex bytes (e.g., `"hi"` → `68 69`).

---

## Key Takeaways

- Goose's array family (`T[k]`, `T[]`/`T[varint]`, `T[..k]`/`T[..]`, `T[>..]`, `T[>..<]`) lets you choose exact inline layout and mutability rules without a heap allocator.
- Arrays of variable-size elements (`u8[][>..]`, `Order[>..]`) pack nested data with zero pointers; they are **sequential** (iterated, not indexed), and can be indexed or sorted via a side array of zero-copy slices.
- Strings are simply `u8` arrays (`const u8[:]` literals/views, `u8[>..]` builders, `u8[]`/`u8[varint]` finished strings, `u8[..k]` small inline strings).
- `print`, `str`, and `format` render any value structurally and automatically pick up custom `fn format(out: u8[>..]&, v: T)` overloads.

That concludes **Part I: Foundations**. In **Part II: References, Types, and Safe Memory**, we open with **Chapter 5: Slices, References, and Optionals**, uncovering how Goose makes references transparent, nullable-safe, and impossible to dangle.
