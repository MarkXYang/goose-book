# Chapter 8: Structs, Root Structs, and Non-Nested Lifetimes

Up to this point, every dynamic container we created obeyed a clean scope hierarchy: one variable owned one array, and its lifetime nested neatly inside a block or function. Real programs quickly push beyond that simple pattern in two ways:

1. **Bundled state:** An application subsystem (a parser context, a simulation engine, a workspace) often needs *several* growable arrays that live and die together.
2. **Non-nested lifetimes:** Cache entries, open editor documents, game entities, and edited tree nodes are created and deleted in arbitrary order—not strictly LIFO.

Goose solves the first with **`root struct`** and the second with two standard-library pool types built directly on top of `root struct`: **`slot_pool<T>`** (reusable individual slots) and **`slice_pool<T>`** (reusable contiguous runs).

By the end of this chapter, you will be able to:

- Design packed structs with field defaults, `let`/`const` fields, explicit `pad`, generics, and resizable tails.
- Group multiple independently growing containers under one owning lifetime using `root struct`.
- Manage arbitrary object lifetimes safely with `slot_pool<T>` and `slice_pool<T>`.
- Explain Goose's **type-safe reuse** guarantee and layer generation counters on top when you want strict stale-handle detection.

---

## 8.1 Ordinary Structs: Layout, Defaults, Qualifiers, and Tails

An ordinary `struct` lays out its fields in declaration order, **packed**—Goose inserts no hidden alignment padding between fields (an `i8[3]` followed by an `i32` is 7 bytes). When you need explicit spacing or C/GPU alignment, insert `pad n` (`n` unused bytes) or bare `pad` (aligns the *next* field to its scalar width). Pad bytes are ignored by `==`.

Key features of ordinary structs:

- **Defaults and `..`:** A field default (`id: i64 = next_id()`) is evaluated each time a literal omits that field. Ending a literal in `..` fills any remaining fields with their declared defaults or type zeroes. Generic code can build a default value with `default<T>()`.
- **Field qualifiers:** `let f: T` forbids reassigning that field after construction; `const f: T` locks both the field and its contents.
- **Generics:** `struct Pair<T> { a: T, b: T }` monomorphizes per concrete type argument (inferred from field values when unambiguous).
- **Resizable tail:** An ordinary struct's **last** field may be a resizable array (`T[>..]` or `T[>..<]`), making the struct itself resizable—still stored as one contiguous layout, but limited to *one* resizable tail.

```goose
var next_seq = 0;
fn gen_id() -> i64 {
    next_seq += 1;
    return next_seq;
}

struct Header {
    let id: i64 = gen_id(),          // immutable after construction
    const tag: u8[..8] = "item",     // read-only contents too
    pad,                             // aligns next field (if needed for foreign layout)
    score: f64 = 1.0,
}

struct Pair<T> { a: T, b: T }

struct NamedStack {
    name: u8[..12],
    items: i64[>..<],                // resizable tail (must be the last field)
}

fn push_pair(s: NamedStack&, x: i64, y: i64) {
    s.items.push(x);
    s.items.push(y);
}

fn main() {
    let h1 = Header { .. };
    var h2 = Header { score: 9.5 };
    h2.score += 0.5;
    let h3 = default<Header>();

    let p = Pair { a: 10, b: 20 };   // Pair<i64> inferred
    var st = NamedStack { name: "ops", items: [] };
    st.push_pair(3, 4);
    let top = st.items.pop();

    print(h1, " | ", h2, " | h3.id=", h3.id);
    print(p, " | ", st.name, "=", st.items, " popped=", top);
}
```

```
Header { 1, "item", 1.0 } | Header { 2, "item", 10.0 } | h3.id=3
Pair<i64> { 10, 20 } | ops=[3] popped=4
```

Remember that `==` compares ordinary structs memberwise (skipping `pad` bytes), comparing reference or slice fields by identity (address and length) so cyclic graphs never loop infinitely.

---

## 8.2 `root struct`: Bundling Multiple Independent Containers

An ordinary struct packs into a single contiguous block, which is why it can have at most one resizable tail. What if a workspace, parser, or data structure needs **two or three** independently growing arrays owned together?

Declare a **`root struct`**:

- A `root struct` lifts the single-contiguous-block requirement: **every field gets its own independent storage (and its own data stack if resizable)**, in any field order.
- All fields share the lifetime of the owning variable—when the owner's scope exits, every component stack rewinds automatically in O(1) time.
- Functions receive a root struct **by reference** (`Workspace<T>&`), and shrinking one field never invalidates references into another field!

```goose
root struct Workspace<T> {
    items: T[>..] = [],
    scratch: i64[>..<] = [],
    ops: i64 = 0,
}

fn record<T>(ws: Workspace<T>&, value: T) -> T& {
    ws.ops += 1;
    let slot .= ws.items.push(value);
    // `ws.scratch` lives on its own independent stack: clearing it cannot affect `slot`!
    ws.scratch.push(ws.ops);
    ws.scratch.clear();
    return slot;
}

fn main() {
    var ws = Workspace<i64> { .. };
    let first .= ws.record(100);
    ws.record(200);
    first += 5;
    print("first=", first, " items=", ws.items, " ops=", ws.ops);
}
```

```
first=105 items=[105, 200] ops=2
```

### Placement Trade-Offs of `root struct`

Because a `root struct` owns multiple separate data stacks rather than one flat byte sequence, it belongs to the **root** size class (Chapter 3) with clear placement rules:

- **Owner declarations only:** An owning `root struct` must be a **local or global variable** (`var ws = Workspace<i64> { .. };`).
- **Never copied, moved by value, or nested:** You cannot copy a root struct, pass/return it by value, put it inside an array, or embed it inside another struct (nested owning roots are not currently supported—declare sibling owners side by side and pass references to them).
- **References are ordinary fixed values:** `Workspace<T>&` and `Workspace<T>?` can be passed to functions, returned, stored in ordinary structs or arrays, and used with UFCS (`ws.record(...)`).
- **No packed byte image:** Serialize eligible fields individually (for example, `to_bytes(ws.items)`).

Trying to copy or store an owning `root struct` inside an array is caught at compile time:

<!-- compile-fail -->
```goose
root struct Bag { xs: i64[>..] = [] }

fn main() {
    var bags: Bag[>..] = [];         // error: root structs cannot be array elements
    print(bags.len);
}
```

> **Memory Model:** Click **Stacks** on the `Workspace<T>` example above! You will see the compiler assign separate data stacks to `ws.items` and `ws.scratch` automatically, freeing both at the end of `main`.

---

## 8.3 `slot_pool<T>`: Reusable Individual Slots

Whenever objects are created and destroyed in arbitrary, non-nested order—entities in a game world, nodes in a mutable graph, cached records—import **`slot_pool`** from the standard library.

`slot_pool<T>` is a `root struct` that pairs a grow-only backing array `items: T[>..]` (for any fixed-size `T`) with an internal freelist of recycled slot indices:

| Operation | What It Does |
|---|---|
| `pool.alloc_index(v) -> i64` | Stores `v` in a recycled free slot (or pushes a new one) and returns its index |
| `pool.alloc_ref(v) -> T&` | Same, returning a direct reference to the slot |
| `pool.alloc_with() { expr } -> T&` | Constructs a value **in place** in the chosen slot (needed for `self` / self-relative links) |
| `pool.free(i)` | Marks slot `i` reusable (bounds-checked; never shrinks `items`) |
| `pool.free_ref(r)` | Converts `r` via `pool.items.index_of(r)` and frees that slot |
| `pool.recycle_all()` | Marks every slot reusable while keeping backing storage initialized |

```goose
import slot_pool;

struct Item { id: i32, weight: f32 }

fn main() {
    var pool = slot_pool<Item> {};
    let s0 = pool.alloc_index(Item { 10, 1.0 });
    let s1 = pool.alloc_index(Item { 11, 2.0 });
    pool.free(s0);

    // Reuses slot 0 instead of growing `pool.items`:
    let s2 = pool.alloc_index(Item { 12, 3.0 });
    let r .= pool.alloc_ref(Item { 13, 4.0 });
    let r_idx = pool.items.index_of(r);
    pool.free_ref(r);

    // In-place construction via `alloc_with` reuses `r_idx`:
    let fresh .= pool.alloc_with() { Item { 14, 5.0 } };

    print("slots: s0=", s0, " s1=", s1, " s2=", s2, " r_idx=", r_idx);
    print("reused? ", fresh .== pool.items[r_idx], " items.len=", pool.items.len);
}
```

```
slots: s0=0 s1=1 s2=0 r_idx=2
reused? true items.len=3
```

Notice `pool.items.len` is `3`—the **high-water mark** of slots ever appended, *including* freed slots. Because `items` never shrinks, references into `pool.items` stay valid for the entire lifetime of `pool`.

---

## 8.4 `slice_pool<T>`: Reusable Contiguous Runs

Sometimes individual fixed slots are not enough: each directory in a filesystem needs a variable-length list of child links, or each open tab in a text editor needs a growable byte buffer that can be closed independently of other tabs.

Import **`slice_pool`**. A `slice_pool<T>` is a `root struct` managing a grow-only `items: T[>..]` plus a coalescing free-span tree that hands out **contiguous slices `T[:]`**:

| Operation | What It Does |
|---|---|
| `pool.alloc_slice(n) -> T[:]` | First-fit free run of length `n` (or extends `items`), every element reset to its default |
| `pool.alloc_copy(src) -> T[:]` | Allocates a run initialized directly from `src` (great for strings in `slice_pool<u8>`) |
| `pool.realloc_slice(s, n) -> T[:]` | Resizes `s` to `n`: shrinks or grows **in place** when possible, otherwise relocates safely |
| `pool.free_slice(s)` | Returns `s` to the free-span tree and merges adjacent free spans |
| `pool.recycle_all()` | Resets the entire high-water range into one free span |
| `pool.trim() -> bool` | *(On `slice_pool<u8>` only)* Discards full unused OS pages inside free runs |

```goose
import slice_pool;

fn main() {
    var runs = slice_pool<i64> {};
    let a = runs.alloc_slice(3);
    a[0] = 10; a[1] = 20; a[2] = 30;
    var b = runs.alloc_slice(2);
    b[0] = 7; b[1] = 8;

    runs.free_slice(a);                      // frees [0..3)
    let c = runs.alloc_slice(2);             // reuses [0..2), reset to [0, 0]
    b = runs.realloc_slice(b, 4);            // `b` sits at the end: grows in place!

    var text = slice_pool<u8> {};
    let msg = text.alloc_copy("hello, pool");

    print("c=", c, " b=", b, " high_water=", runs.items.len);
    print("msg=", msg);
}
```

```
c=[0, 0] b=[7, 8, 0, 0] high_water=7
msg=hello, pool
```

---

## 8.5 Type-Safe Reuse vs. Memory Safety (and Legacy `reusable`)

Pause and think carefully about what happens if your code holds a reference `r` to a `slot_pool` slot, frees that slot, allocates a new value into it, and mistakenly reads `r` afterward.

- **It is never memory corruption:** Backing storage is never unmapped or retyped; every slot in `pool.items` remains a live, properly initialized `T` for the life of the pool. Reading `r` reads a *different valid `T`*, never garbage bytes, never a wild pointer, and never an out-of-bounds crash.
- **It is still a logic bug**—the exact same class of bug as holding a stale integer index into a Rust `Vec` or database table.

Whenever you want **strict runtime detection of stale handles** (for example, entity references in a game or document handles in an editor), pair each slot index with a monotonically increasing **generation counter** (`u32`):

1. Store `gen: u32` inside each pooled record.
2. Hand callers a lightweight value handle `struct Handle { idx: i64, gen: u32 }`.
3. On free, increment `slot.gen` (and clear `slot.alive = false`) before returning the slot to `pool.free(idx)`.
4. On lookup, verify `slot.alive && slot.gen == handle.gen`—returning `null` if the handle is stale!

> **Migration Note (Legacy `reusable`):** Older Goose code and docs may show language-level `reusable var pool: T[>..] = [];` and `reusable[] var runs: T[>..] = [];`. Those language keywords are **deprecated** in favor of the standard library's `slot_pool<T>` and `slice_pool<T>` (which are written in ordinary Goose on top of `root struct`). Always prefer `slot_pool` and `slice_pool` in new code.

---

## 8.6 Worked Example: A Multi-Document Buffer Manager

Let us build a mini editor buffer manager where documents are opened, edited, resized, and closed in arbitrary order—combining `slot_pool<Doc>`, `slice_pool<u8>`, and **generational handles** so stale handles to closed documents are caught cleanly:

```goose
import slot_pool;
import slice_pool;

struct Doc {
    name: u8[..16],
    content: u8[:],
    gen: u32,
    alive: bool,
}

struct DocHandle {
    idx: i64,
    gen: u32,
}

fn open_doc(docs: slot_pool<Doc>&, gens: u32[>..]&, text: slice_pool<u8>&,
            name: const u8[:], initial: const u8[:]) -> DocHandle {
    let buf = text.alloc_copy(initial);
    let idx = docs.alloc_index(Doc { name: name, content: buf, gen: 0, alive: true });
    while gens.len <= idx { gens.push(1); }
    let g = gens[idx];
    docs.items[idx].gen = g;
    return DocHandle { idx: idx, gen: g };
}

fn get_doc(docs: slot_pool<Doc>&, h: DocHandle) -> Doc? {
    guard h.idx >= 0 && h.idx < docs.items.len else { return null; }
    let d .= docs.items[h.idx];
    guard d.alive && d.gen == h.gen else { return null; }
    return d;
}

fn append_text(docs: slot_pool<Doc>&, text: slice_pool<u8>&,
               h: DocHandle, extra: const u8[:]) -> bool {
    let d = get_doc(docs, h);
    guard d else { return false; }
    let old_len = d.content.len;
    d.content = text.realloc_slice(d.content, old_len + extra.len);
    d.content[old_len..].move_from(extra);
    return true;
}

fn close_doc(docs: slot_pool<Doc>&, gens: u32[>..]&, text: slice_pool<u8>&,
             h: DocHandle) -> bool {
    let d = get_doc(docs, h);
    guard d else { return false; }
    text.free_slice(d.content);
    d.content = text.alloc_slice(0);
    d.alive = false;
    gens[h.idx] += 1;                // invalidates every existing handle to this slot!
    docs.free(h.idx);
    return true;
}

fn main() {
    var docs = slot_pool<Doc> {};
    var gens: u32[>..] = [];
    var text = slice_pool<u8> {};

    let h1 = open_doc(docs, gens, text, "notes.md", "hello");
    let h2 = open_doc(docs, gens, text, "todo.md", "buy milk");
    let h3 = open_doc(docs, gens, text, "main.goose", "fn main() {}");

    append_text(docs, text, h2, " + eggs");

    // Close `todo.md` (slot 1) — both its Doc slot and byte span return to the pools:
    close_doc(docs, gens, text, h2);

    // Open a new document — reuses slot 1 with generation 2!
    let h4 = open_doc(docs, gens, text, "plan.md", "phase 1");

    print("h2 slot=", h2.idx, " gen=", h2.gen, " | h4 slot=", h4.idx, " gen=", h4.gen);
    print("stale h2 lookup valid? ", get_doc(docs, h2) != null);
    print("append to stale h2?    ", append_text(docs, text, h2, "!"));
    print("pool high-water: ", docs.items.len, " doc slots, ", text.items.len, " bytes");

    for h in [h1, h3, h4] {
        let d = get_doc(docs, h);
        if d { print("  ", d.name, ": \"", d.content, "\""); }
    }
}
```

```
h2 slot=1 gen=1 | h4 slot=1 gen=2
stale h2 lookup valid? false
append to stale h2?    false
pool high-water: 3 doc slots, 40 bytes
  notes.md: "hello"
  main.goose: "fn main() {}"
  plan.md: "phase 1"
```

Look closely at `h2` vs. `h4`: opening `"plan.md"` reused slot `1` (so `docs.items.len` stayed at `3`) and reused the freed bytes inside `text`, yet the bumped generation (`gen=2`) made the stale handle `h2` (`gen=1`) fail safely on both lookup and mutation.

---

## Exercises

1. **Inspect `root struct` stacks:** Click **Stacks** on the `Workspace<T>` example in Section 8.2, then add a third resizable field `log: u8[>..] = []` to `Workspace<T>` and click **Stacks** again to confirm each field gets its own data stack.
2. **Test `slice_pool<u8>.trim()`:** In Section 8.6, open a large 100,000-byte document, close it, and call `text.trim()`. Check its boolean return value and confirm live documents (`h1`, `h3`, `h4`) remain completely intact.
3. **Add `rename_doc`:** Extend the Section 8.6 buffer manager with `fn rename_doc(docs: slot_pool<Doc>&, h: DocHandle, new_name: const u8[:]) -> bool` using `d.name.clear(); format(d.name, new_name);` and verify that renaming a closed handle returns `false`.

---

## Key Takeaways

- Ordinary structs pack fields tightly without hidden padding; use `pad` for foreign layouts, `let`/`const` for immutable fields, field defaults + `default<T>()`, and a single resizable tail when one growable field suffices.
- **`root struct`** groups multiple independently growing containers under one local or global owner whose fields share the owner's scope-exit lifetime.
- For **non-nested lifetimes**, use the standard library's **`slot_pool<T>`** (reusable fixed slots) and **`slice_pool<T>`** (reusable coalescing contiguous runs) instead of deprecated `reusable` / `reusable[]`.
- Pool reuse is **type-safe** (never memory corruption); add a generation counter whenever you want runtime rejection of stale handles.

That wraps up **Part II: References, Types, and Safe Memory**! In **Part III: Abstraction, Control, and Data Structures**, we start with **Chapter 9: Functions, Generics, and Compile-Time Expansion**, exploring Goose's zero-overhead monomorphized generics, trailing function blocks, non-local `return`, and `const for` / `const if` reflection.
