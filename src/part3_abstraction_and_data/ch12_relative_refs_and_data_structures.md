# Chapter 12: Relative References and Pointer-Free Data Structures

A plain machine reference `T&` takes 8 bytes and stores a raw virtual address. That works great for local variables and parameters, yet inside a large tree, graph, or linked list, 8-byte pointers bring two real costs:

1. **Cache bloat:** A binary tree node with an `i32` key and two 8-byte pointers takes 20–24 bytes—over 75% of every cache line is spent on pointers instead of data.
2. **Process-bound addresses:** Raw virtual addresses mean nothing outside the current process, so saving a pointer graph to disk requires writing a custom serializer and pointer-fixup loader.

Goose solves both with **relative references**—typed, bounds-checked, transparent references stored as compact **1-, 2-, or 4-byte offsets** (`T&<u8>`, `T&<u16>`, `T&<u32>`, `T&<u64>`, or `T&<varint>`). Reading a relative reference yields an ordinary `T&`; storing one encodes the offset automatically.

By the end of this chapter, you will be able to:

- Choose between **self-relative** (`T&<u32>`) and **pool-relative** (`T&<u32 in pool>`) references.
- Build compact, pointer-free binary search trees inside a single grow-only array and work cleanly with self-relative non-copyability rules.
- Construct null-free circular doubly-linked lists using `slot_pool<T>`, `alloc_with`, and **`self`** initializers.
- Combine `slot_pool`, `slice_pool`, and 4-byte `in pool.items` links to build a mutable n-ary directory tree with zero heap allocations.

---

## 12.1 Self-Relative vs. Pool-Relative References

Relative references come in two forms, differing only in where the offset is measured from:

```text
1. Self-relative:  T&<u32>  /  T&<u32>?
   Offset = target_address - field_address   (0 means null)
   [ node 0 ] ... [ node 1: left = +16 ] ---> [ node 2 ]
                               ^                   ^
                          field_addr          target_addr
   -> Position-independent! The entire array means the same thing anywhere in RAM or on disk.

2. Pool-relative:  T&<u32 in pool>  /  T&<u32 in pool.items>?
   Stored value = (target_address - pool_base) + 1   (0 means null)
   pool_base + 0        + 16          + 32
   [ node 0 ]           [ node 1 ]    [ node 2 ]
   Stored in ANY array (even another array!): offset = 32 + 1 = 33 -> points to node 2!
```

Both forms support the optional `?` suffix (`T&<u32>?`) using `0` for `null`, so a nullable 4-byte link costs **4 bytes total** (zero extra tag bytes).

| Property | Self-Relative (`T&<u32>`) | Pool-Relative (`T&<u32 in pool>`) |
|---|---|---|
| Works in local / parameter arrays? | **Yes** (no global name needed) | **No** (`pool` must be a global grow-only `var` array or `root.items` field) |
| Links from *other* arrays into the pool? | **No** (target must live in the same array) | **Yes** (e.g., an external index or `slice_pool` of child links) |
| Position-independent / `to_bytes` serializable? | **Yes** (Chapter 13!) | **No** (depends on external `pool` base) |
| Copyable value? | **No** (offset depends on field's own address) | **Yes** (offset depends only on `pool`'s base) |
| Root precision when read from a container | Exact same enclosing array | **Exact** (`pool` is part of the type's identity!) |

Note that a function's parameter or return type is never written as a relative reference—write plain `T&` or `T?`, and the relative field decodes on read and re-encodes on `.=` store automatically.

---

## 12.2 Self-Relative Trees and Non-Copyability

Here is a complete binary search tree living inside **one grow-only array**, where every node is just **16 bytes** (two `i32`s and two nullable 4-byte self-relative links—or 12 bytes without `count`):

```goose
struct Node {
    key: i32,
    count: i32 = 1,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

fn insert(pool: Node[>..]&, key: i32) {
    if pool.len == 0 {
        pool.push(Node { key: key });
        return;
    }
    var cur .= pool[0];              // `.=` binds a reference (`=` would try to copy!)
    loop {
        if key == cur.key {
            cur.count += 1;
            return;
        }
        let next = if key < cur.key { cur.left } else { cur.right };
        if next {
            cur .= next;             // walk down via narrowed reference
            continue;
        }
        // `pool.push(...)` builds the new node in place and never invalidates `cur`!
        if key < cur.key { cur.left  .= pool.push(Node { key: key }); }
        else             { cur.right .= pool.push(Node { key: key }); }
        return;
    }
}

recursive fn in_order(n: Node&, out: i32[>..]&) {
    let l = n.left;
    if l { in_order(l, out); }
    out.push(n.key);
    let r = n.right;
    if r { in_order(r, out); }
}

fn main() {
    var pool: Node[>..] = [];
    for k in [50, 30, 70, 20, 40, 60, 80, 30] {
        insert(pool, k as i32);
    }
    var keys: i32[>..] = [];
    in_order(pool[0], keys);
    print("sorted=", keys);
    print(pool.len, " nodes in ", bytes_of(pool).len, " bytes; key 30 count=", pool[1].count);
}
```

```
sorted=[20, 30, 40, 50, 60, 70, 80]
7 nodes in 112 bytes; key 30 count=2
```

### Why Self-Relative Structs Cannot Be Copied

Because a self-relative offset is measured from the field's *own address*, copying a `Node` to a different address (a local variable or another array) would silently make its offset point to the wrong place! Therefore, the compiler forbids copying any value that contains a self-relative link:

<!-- compile-fail -->
```goose
struct Node { key: i32, left: Node&<u32>? }

fn main() {
    var pool: Node[>..] = [Node { key: 1 }];
    let bad = pool[0];               // error: cannot copy value containing self-relative link
    print(bad.key);
}
```

Work with self-relative structs in three idiomatic ways:
- **Construct in place:** `pool.push(Node { ... })` (a function or block returning a fresh `Node` straight into `pool.push(...)` is also built in place at `pool`'s destination!).
- **Bind by reference:** `let n .= pool[0];` instead of `let n = pool[0];`.
- **Pass and match by reference:** `fn walk(n: Node&)` and `match e { Add &a => ... }`.

---

## 12.3 Pool-Relative Links, `self`, and Null-Free Doubly-Linked Lists

A circular doubly-linked list with a sentinel node is famously clean: there are **no `null` links anywhere**, so every insert and remove executes the exact same four link updates with zero edge cases.

But how do you initialize the sentinel node's non-optional `prev` and `next` links to point to *itself* before the sentinel exists?

With **`self`**! Inside a struct or variant literal constructed directly in its destination (such as `slot_pool.alloc_with()`), writing `prev: self, next: self` initializes those non-optional relative links to point straight at the node being constructed!

```goose
import slot_pool;

struct ListNode {
    val: i64,
    prev: ListNode&<u32 in list_pool.items>,
    next: ListNode&<u32 in list_pool.items>,
}

var list_pool = slot_pool<ListNode> {};
// Sentinel allocated once before `main`: `self` points directly to slot 0!
let sentinel .= list_pool.alloc_with() {
    ListNode { val: 0, prev: self, next: self }
};

fn insert_after(p: ListNode&, v: i64) -> ListNode& {
    let n .= list_pool.alloc_with() { ListNode { val: v, prev: p, next: p.next } };
    n.next.prev .= n;
    p.next .= n;
    return n;
}

fn unlink_and_free(n: ListNode&) {
    n.prev.next .= n.next;
    n.next.prev .= n.prev;
    list_pool.free_ref(n);
}

fn push_back(v: i64) -> ListNode& { insert_after(sentinel.prev, v) }
fn is_end(n: ListNode&) -> bool   { n .== sentinel }

fn each_node<F>() {
    var cur .= sentinel.next;
    while !is_end(cur) {
        let next .= cur.next;        // read `next` before `F` in case `F` removes `cur`!
        F(cur);
        cur .= next;
    }
}

fn main() {
    for v in [10, 20, 30, 40, 50] { push_back(v); }

    // Remove multiples of 20 during iteration, returning their slots to `list_pool`:
    each_node() { if it.val % 20 == 0 { unlink_and_free(it); } };

    // Push two new values — they reuse the two freed slots!
    push_back(77);
    push_back(88);

    var forward: i64[>..] = [];
    each_node() { forward.push(it.val); };

    var backward: i64[>..] = [];
    var cur .= sentinel.prev;
    while !is_end(cur) {
        backward.push(cur.val);
        cur .= cur.prev;
    }

    print("forward:  ", forward);
    print("backward: ", backward);
    print("slots used: ", list_pool.items.len, " (16 bytes/node)");
}
```

```
forward:  [10, 30, 50, 77, 88]
backward: [88, 77, 50, 30, 10]
slots used: 6 (16 bytes/node)
```

Notice three things working together seamlessly:
- Every `ListNode` is just **16 bytes** (`i64` + two 4-byte `in list_pool.items` links), with zero `null` checks anywhere in the entire program.
- `is_end(n)` uses **`.==`** (reference identity) rather than `==` (which would compare node contents).
- `list_pool.items.len` stayed at `6` (1 sentinel + 5 nodes) even after removing 2 nodes and inserting 2 more!

### Indexing Variable-Mode Sequential Arrays with 4-Byte Links

Pool-relative links also give you a compact 4-byte random-access index into any global variable-mode sequential array (`Mark..[>..]`):

```goose
enum Mark { Circle { r: f64 }, Label { text: u8[varint] }, Dot }

var marks:   Mark..[>..]                  = [];
var mark_at: (Mark..&<u32 in marks>)[>..] = [];

fn main() {
    mark_at.push(marks.push(Mark.Label { text: "hello, goose" }));
    mark_at.push(marks.push(Mark.Circle { r: 1.5 }));
    mark_at.push(marks.push(Mark.Dot));

    for idx in [2, 0, 1] {
        match mark_at[idx] {
            Label l  => print(idx, ": label \"", l.text, "\""),
            Circle c => print(idx, ": circle r=", c.r),
            Dot      => print(idx, ": dot"),
        }
    }
    print(bytes_of(marks).len, " bytes of marks, ", mark_at.len, " links of 4 bytes");
}
```

```
2: dot
0: label "hello, goose"
1: circle r=1.5
24 bytes of marks, 3 links of 4 bytes
```

---

## 12.4 Worked Example: An In-Memory Directory Tree (`slot_pool` + `slice_pool`)

Now let us combine every tool from Chapters 8, 11, and 12 into a mutable n-ary **directory tree** supporting `mkdir`/`touch`, `mv`, and recursive `rm -r`—with **zero heap allocations**:

- Nodes live in `var dir_nodes = slot_pool<DirNode> {};`.
- Each directory's children are stored as one contiguous run of 4-byte `DirNode&<u32 in dir_nodes.items>?` links inside `var dir_runs = slice_pool<DirLink> {};`.
- Removing a subtree (`rm_tree`) frees every node slot *and* every child-link run; creating new files immediately reuses both!

```goose
import slot_pool;
import slice_pool;

type DirLink = DirNode&<u32 in dir_nodes.items>?;

struct DirNode {
    name: u8[..15],                  // fixed inline name
    children: DirLink[:],            // contiguous run of 4-byte links in `dir_runs`
}

var dir_nodes = slot_pool<DirNode> {};
var dir_runs  = slice_pool<DirLink> {};

fn make_node(name: const u8[:]) -> DirNode& {
    return dir_nodes.alloc_ref(DirNode { name: name, children: dir_runs.alloc_slice(0) });
}

let fs_root .= make_node("/");

fn attach(dir: DirNode&, child: DirNode&) {
    let k = dir.children.len;
    dir.children = dir_runs.realloc_slice(dir.children, k + 1);
    dir.children[k] .= child;
}

fn add_entry(dir: DirNode&, name: const u8[:]) -> DirNode& {
    let n .= make_node(name);
    attach(dir, n);
    return n;
}

fn find_child_index(dir: DirNode&, target: DirNode&) -> i64 {
    for i in dir.children.len {
        let e = dir.children[i];
        assert(e);
        if e .== target { return i; }
    }
    abort("child not found");
}

fn detach(dir: DirNode&, idx: i64) -> DirNode& {
    let n = dir.children[idx];
    assert(n);
    for j in idx..dir.children.len - 1 {
        dir.children[j] .= dir.children[j + 1];
    }
    dir.children = dir_runs.realloc_slice(dir.children, dir.children.len - 1);
    return n;
}

recursive fn release_subtree(n: DirNode&) {
    for e in n.children {
        assert(e);
        release_subtree(e);
    }
    dir_runs.free_slice(n.children);
    dir_nodes.free_ref(n);
}

fn rm_entry(dir: DirNode&, child: DirNode&) {
    detach(dir, find_child_index(dir, child));
    release_subtree(child);
}

fn mv_entry(from_dir: DirNode&, child: DirNode&, to_dir: DirNode&) {
    attach(to_dir, detach(from_dir, find_child_index(from_dir, child)));
}

recursive fn dump_tree(n: DirNode&, depth: i64) -> i64 {
    {
        var line: u8[>..] = [];
        for i in depth { line.append("  "); }
        format(line, n.name);
        print(line);
    }
    var count = 1;
    for e in n.children {
        assert(e);
        count += dump_tree(e, depth + 1);
    }
    return count;
}

fn show_state(label: const u8[:]) {
    print("=== ", label, " ===");
    let live = dump_tree(fs_root, 0);
    print("live=", live, " | node slots=", dir_nodes.items.len,
          " | link cells=", dir_runs.items.len);
}

fn main() {
    let home .= add_entry(fs_root, "home/");
    let ann  .= add_entry(home, "ann/");
    let todo .= add_entry(ann, "todo.txt");
    let bob  .= add_entry(home, "bob/");
    add_entry(bob, "save.dat");
    let etc  .= add_entry(fs_root, "etc/");
    add_entry(etc, "hosts");
    show_state("initial");

    // Delete /home/bob/ and its child — 2 node slots + runs freed:
    rm_entry(home, bob);

    // Move /home/ann/todo.txt -> /etc/todo.txt:
    mv_entry(ann, todo, etc);

    // Create two new entries — reuses both freed node slots without growing!
    let carl .= add_entry(home, "carl/");
    add_entry(carl, "notes.md");
    show_state("after rm, mv, and reuse");
}
```

```
=== initial ===
/
  home/
    ann/
      todo.txt
    bob/
      save.dat
  etc/
    hosts
live=8 | node slots=8 | link cells=7
=== after rm, mv, and reuse ===
/
  home/
    ann/
    carl/
      notes.md
  etc/
    hosts
    todo.txt
live=8 | node slots=8 | link cells=7
```

Look at that result: deleting `/home/bob` + `save.dat`, moving `todo.txt`, and adding `/home/carl` + `notes.md` kept **both** pools completely flat at **8 node slots and 7 link cells**—with typed references throughout, 4-byte child links, and zero heap allocations!

---

## Exercises

1. **Shrink the BST to 2-byte links:** In Section 12.2, change `left` and `right` from `Node&<u32>?` to `Node&<u16>?` (and remove `count`). Click **▶ Run** and verify each node is now just **8 bytes** (`4 + 2 + 2`).
2. **Add `move_to_front` to the doubly-linked list:** In Section 12.3, write `fn move_to_front(n: ListNode&)` that unlinks `n` and splices it right after `sentinel` without touching `list_pool` (0 allocations, 6 pointer stores), and move `77` to the front of the list.
3. **Add `find_path` to the directory tree:** Write a helper `fn find_by_name(dir: DirNode&, name: const u8[:]) -> DirNode?` that returns a narrowed child reference matching `name` (or `null`), and test looking up `"etc/"` and `"hosts"` starting from `fs_root`.

---

## Key Takeaways

- **Relative references** (`T&<u8>`, `T&<u16>`, `T&<u32>`, optional `T&<u32>?` with `0` = `null`) pack typed, bounds-checked links into 1–4 bytes and decode transparently into `T&`.
- **Self-relative (`T&<u32>`)** links are measured from the field itself—ideal for local arrays and position-independent serialized blobs (Chapter 13), and values holding them are constructed in place and never copied.
- **Pool-relative (`T&<u32 in pool>`)** links are measured from a global grow-only array (or `root.items`)—ideal for cross-array indices, relink-heavy `slot_pool` structures, and exact roots.
- **`self`** inside a struct literal initializes non-optional circular links (`prev: self, next: self`) cleanly via `slot_pool.alloc_with()`, eliminating `null` edge cases in sentinels.

Now that you can build pointer-free, position-independent trees and graphs using self-relative links, **Chapter 13: Zero-Copy Persistence and Verified Serialization** shows how saving an entire tree to disk is a single write of its bytes—and loading untrusted files back safely takes one verified call to `from_bytes`!
