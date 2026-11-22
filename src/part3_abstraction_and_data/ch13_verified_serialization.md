# Chapter 13: Zero-Copy Persistence and Verified Serialization

In most languages, saving a rich in-memory data structure to disk means writing a serializer or introducing an external schema compiler (Protobuf, Cap'n Proto, FlatBuffers, Serde) to convert pointer graphs into a wire format and parse them back again.

In Goose, **a structure built from flat data and self-relative links is already its own file format.** Because every string is inline and every link is a self-relative offset (Chapter 12), the bytes sitting in memory mean the exact same thing wherever they are copied—no schema, no code generator, no pointer fixups, and no per-node allocation on reload.

There is only one catch: bytes arriving from a disk file or network socket cannot be trusted the way compiler-constructed values are. Goose solves that at the language level: **`from_bytes` runs a compiler-generated verifier before untrusted bytes ever become a Goose value.**

By the end of this chapter, you will be able to:

- Use `bytes_of`, `to_bytes`, and `from_bytes<T[>..]>` to persist and reload flat and self-relative arrays.
- Explain every safety check `from_bytes` performs—and the critical distinction between **memory safety** and **data integrity**.
- Serialize and verify compact **variable-mode** trees using `varint` strings and `varint` self-relative links.
- Wrap `to_bytes` images inside a versioned, checksummed container format using `binary` and `std`'s `hash`.

---

## 13.1 The Three Serialization Builtins

Goose provides three builtins for turning eligible arrays into bytes and back:

| Builtin | Signature | Meaning |
|---|---|---|
| `bytes_of(a)` | `-> const u8[:]` | Zero-copy, read-only byte view of `a`'s element region (no framing prefix) |
| `to_bytes(a)` / `to_bytes(a, out)` | `-> u8[>..]` / appends to `out` | Framed image: a ULEB128 byte count followed by the element region |
| `from_bytes<A>(b)` | `-> A, bool` | Verifies framed image `b`, then returns a live ordinary array (`A` = `T[>..]`, `T[]`, or link-free `T[>..<]`) and `true`, or an empty array and `false` |

```text
Framed image produced by to_bytes(arr) and expected by from_bytes<T[>..]>(bytes):

  +------------------------+------------------------------------------------+
  | ULEB128(payload_bytes) |  exact contiguous element region (little-endian)|
  +------------------------+------------------------------------------------+
       1..10 bytes                      exact payload_bytes bytes
```

`bytes_of(a)` is always read-only (`const u8[:]`) so code can never overwrite raw link offsets behind the checker's back. Notice also that `to_bytes(a, out)` appends the framed image straight into an existing `u8[>..]` buffer—letting you prepend your own header tag with a single copy!

```goose
import std;

struct Node {
    word: u8[..12],
    count: i32,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

fn insert(pool: Node[>..]&, word: const u8[:]) {
    if pool.len == 0 {
        pool.push(Node { word: word, count: 1 });
        return;
    }
    var cur .= pool[0];
    loop {
        let c = compare(word, cur.word[..]);
        if c == 0 { cur.count += 1; return; }
        let next = if c < 0 { cur.left } else { cur.right };
        if next { cur .= next; continue; }
        if c < 0 { cur.left  .= pool.push(Node { word: word, count: 1 }); }
        else     { cur.right .= pool.push(Node { word: word, count: 1 }); }
        return;
    }
}

fn lookup(pool: Node[>..]&, word: const u8[:]) -> i32 {
    guard pool.len > 0 else { return 0; }
    var cur .= pool[0];
    loop {
        let c = compare(word, cur.word[..]);
        if c == 0 { return cur.count; }
        let next = if c < 0 { cur.left } else { cur.right };
        guard next else { return 0; }
        cur .= next;
    }
}

fn main() {
    var index: Node[>..] = [];
    each_word("the quick fox and the lazy dog and the fox") {
        insert(index, it);
    };

    // Build a framed image and reload it into a brand-new independent tree:
    let image = index.to_bytes();
    var loaded, ok = from_bytes<Node[>..]>(image);

    // `loaded` is an ordinary grow-only array: we can query AND keep growing it!
    insert(loaded, "goose");
    print("ok=", ok, " original=", index.len, " loaded=", loaded.len);
    print("raw payload=", bytes_of(index).len, " framed=", image.len, " bytes");
    print("counts: the=", lookup(loaded, "the"), " fox=", lookup(loaded, "fox"),
          " goose=", lookup(loaded, "goose"));
}
```

```
ok=true original=6 loaded=7
raw payload=150 framed=152 bytes
counts: the=3 fox=2 goose=1
```

Look how natural that is: once `from_bytes` returns `true`, `loaded` is indistinguishable from a tree you constructed in memory—rooted at `loaded`, indexable, growable, and obeying every ordinary lifetime rule.

---

## 13.2 What `from_bytes` Verifies (Safety vs. Integrity)

External bytes are the *only* place in Goose where a hostile or corrupted offset could try to forge a reference. Therefore, `from_bytes` validates four properties before handing back a value:

1. **Exact framing:** The ULEB128 byte count must be canonical and account for **every remaining byte** of the slice (catching truncation, trailing garbage, and empty slices). For fixed `T`, the payload size must be an exact multiple of `sizeof(T)`; for variable `T`, a sequential walk must land exactly at the end.
2. **Tags, lengths, and indices:** Every enum tag must be in range, every nested inline array length must fit within the remaining bytes, and every index type (`uN in a`) must be in range of its array.
3. **Self-relative links:** Offset `0` is accepted only on optional (`?`) links; every non-zero offset `field_offset + offset` must land **precisely on an element start** inside the same image (and match the variant's tag for variant-typed links).
4. **Compile-time eligibility:** Types containing raw addresses (`T&`, `T[:]`) or external pool links (`T&<u32 in pool>`) are rejected at compile time—as is loading self-relative links into a grow-shrink `T[>..<]` (where a later `.pop()` would dangle internal links).

Attempting to serialize a struct with plain references is caught at compile time:

<!-- compile-fail -->
```goose
struct Bad { v: i32, next: Bad? }

fn main() {
    var xs: Bad[>..] = [Bad { v: 1 }];
    let img = xs.to_bytes();         // error: plain references are addresses, not serializable
    print(img.len);
}
```

### Safety Is Not Integrity

Understanding what `from_bytes` does—and does **not**—promise is essential:

- **Tampering with a link offset, length, tag, or file size** threatens memory safety → **`from_bytes` rejects it (`false`, empty array).**
- **Flipping a bit inside a plain integer scalar (like `count`)** still produces a 100% memory-safe, structurally valid tree with a different number → **`from_bytes` accepts it (`true`).**

```goose
struct Node {
    val: i32,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

fn main() {
    var tree: Node[>..] = [];
    let root .= tree.push(Node { val: 10 });
    root.left .= tree.push(Node { val: 5 });
    root.right .= tree.push(Node { val: 20 });

    let img = tree.to_bytes();
    let pfx = img.len - bytes_of(tree).len;

    // 1. Corrupt a byte inside `root.left` (offset 4..8 of node 0) -> REJECTED!
    var bad_link = copy(img);
    bad_link[pfx + 5] = bad_link[pfx + 5] ^ 0x40;
    let t1, ok1 = from_bytes<Node[>..]>(bad_link);

    // 2. Truncate by 1 byte -> REJECTED!
    let t2, ok2 = from_bytes<Node[>..]>(img[..^1]);

    // 3. Append 1 trailing junk byte -> REJECTED!
    var extended = copy(img);
    extended.push(0);
    let t3, ok3 = from_bytes<Node[>..]>(extended);

    // 4. Flip a bit inside `root.val` (offset 0 of node 0) -> ACCEPTED (memory-safe, wrong value)!
    var edited_val = copy(img);
    edited_val[pfx] = edited_val[pfx] ^ 0x01;
    let t4, ok4 = from_bytes<Node[>..]>(edited_val);

    // 5. Empty framed image vs. unframed empty slice:
    var empty: Node[>..] = [];
    let t5, ok5 = from_bytes<Node[>..]>(empty.to_bytes());
    let t6, ok6 = from_bytes<Node[>..]>("");

    print("bad_link=", ok1, " (len=", t1.len, ") truncated=", ok2, " extended=", ok3);
    print("edited_val=", ok4, " (new val=", t4[0].val, ")");
    print("valid_empty=", ok5, " (len=", t5.len, ") unframed_empty=", ok6);
}
```

```
bad_link=false (len=0) truncated=false extended=false
edited_val=true (new val=11)
valid_empty=true (len=0) unframed_empty=false
```

That is why production container formats pair `from_bytes` (for guaranteed memory safety) with a header + checksum or cryptographic hash (for data integrity)—which we will build in Section 13.4!

---

## 13.3 Serializing Variable-Mode Self-Relative Trees

`from_bytes` is not limited to fixed-size nodes! It also verifies **variable-mode enums (`Tiny..`)** with inline `u8[varint]` strings and `varint` self-relative links:

- During verification, pass 1 walks the sequential stream to verify every tag, varint, and string length while recording every valid element start offset.
- Pass 2 verifies that every non-zero `varint` link lands on one of those exact element starts.

Watch how much smaller a word tree becomes when packed bottom-up into variable-mode `Tiny..[>..]` (pushing children before parents so backward offsets are tiny 1–2 byte varints!):

```goose
import std;

enum Tiny {
    Leaf { word: u8[varint], count: varint },
    Fork {
        word: u8[varint],
        count: varint,
        left: Tiny..&<varint>?,
        right: Tiny..&<varint>?,
    },
}

fn total_count(pool: Tiny..[>..]&) -> i64 {
    var sum = 0;
    for node in pool {
        match node {
            Leaf &l => { sum += l.count; },
            Fork &f => { sum += f.count; },
        }
    }
    return sum;
}

fn main() {
    var tiny: Tiny..[>..] = [];
    let a = tiny.push(Tiny.Leaf { word: "and", count: 2 });
    let d = tiny.push(Tiny.Leaf { word: "dog", count: 1 });
    let f = tiny.push(Tiny.Fork { word: "fox", count: 2, left: a, right: d });
    let q = tiny.push(Tiny.Leaf { word: "quick", count: 1 });
    let root = tiny.push(Tiny.Fork { word: "the", count: 3, left: f, right: q });

    let img = tiny.to_bytes();
    var reloaded, ok = from_bytes<Tiny..[>..]>(img);

    print("nodes=", tiny.len, " framed=", img.len, " bytes");
    print("reloaded ok=", ok, " total_count=", total_count(reloaded),
          " matches=", total_count(reloaded) == total_count(tiny));
    match root {
        Fork &fk => print("root=", fk.word, " x", fk.count),
        Leaf &lf => print("root=", lf.word, " x", lf.count),
    }
}
```

```
nodes=5 framed=37 bytes
reloaded ok=true total_count=9 matches=true
root=the x3
```

Five tree nodes—with five inline strings, counts, enum tags, and four tree links—serialize, verify, and reload in **37 bytes total**!

---

## 13.4 Worked Example: A Checksummed Binary Container (`"GSX1"`)

Let us combine `to_bytes`, `from_bytes`, `std`'s `hash`, and the `binary` module into a production-ready binary container format:

```text
+------------+--------------+------------------+-----------------------------+
| "GSX1" (4B)| version (4B) | hash64 (8B LE)   | to_bytes(tree) framed blob  |
+------------+--------------+------------------+-----------------------------+
```

- **Magic + Version:** Rejects wrong file types or incompatible schemas immediately.
- **64-bit Checksum (`hash(payload)`):** Catches *any* accidental corruption, including 1-bit flips inside scalar counts!
- **Verified `from_bytes<Node[>..]>`: ** Guarantees structural and pointer safety even against crafted malicious files whose checksum matches!

*(In a real desktop or server CLI app, save the resulting `u8[>..]` with `os::write_file_atomic("index.gsx", blob)` and load it back with `os::read_file("index.gsx", blob)`—the exact pattern used by Goose's `30_mini_minecraft.goose` save system!)*

```goose
import std;
import binary;

let MAGIC = "GSX1";
let FORMAT_VERSION: u32 = 1;

struct WordNode {
    word: u8[..16],
    count: i32,
    left: WordNode&<u32>?,
    right: WordNode&<u32>?,
}

fn insert_word(pool: WordNode[>..]&, word: const u8[:]) {
    if pool.len == 0 { pool.push(WordNode { word: word, count: 1 }); return; }
    var cur .= pool[0];
    loop {
        let c = compare(word, cur.word[..]);
        if c == 0 { cur.count += 1; return; }
        let next = if c < 0 { cur.left } else { cur.right };
        if next { cur .= next; continue; }
        if c < 0 { cur.left  .= pool.push(WordNode { word: word, count: 1 }); }
        else     { cur.right .= pool.push(WordNode { word: word, count: 1 }); }
        return;
    }
}

fn save_container(pool: WordNode[>..]&) -> u8[>..] {
    let payload = pool.to_bytes();
    let checksum = hash(payload);

    var out: u8[>..] = [];
    out.append(MAGIC);
    binary::write_u32le(out, FORMAT_VERSION);
    binary::write_u64le(out, checksum);
    out.append(payload);
    return out;
}

// Validates the container header and integrity checksum for ANY serialized type,
// returning a zero-copy slice of the framed payload ready for `from_bytes<T>`.
fn unwrap_container(blob: const u8[:]) -> const u8[:], const u8[:] {
    var r = binary::reader(blob);
    let magic, ok_m = binary::read_bytes(r, 4);
    guard ok_m && magic == MAGIC else { return "", "bad magic"; }

    let ver, ok_v = binary::read_u32le(r);
    guard ok_v && ver == FORMAT_VERSION else { return "", "unsupported version"; }

    let expected_hash, ok_h = binary::read_u64le(r);
    guard ok_h else { return "", "truncated header"; }

    let payload = blob[r.pos..];
    guard hash(payload) == expected_hash else { return "", "checksum mismatch"; }
    return payload, "";
}

fn try_load(label: const u8[:], blob: const u8[:]) {
    let payload, err = unwrap_container(blob);
    if err.len > 0 {
        print(label, ": err='", err, "'");
        return;
    }
    let loaded, ok = from_bytes<WordNode[>..]>(payload);
    if !ok {
        print(label, ": err='corrupt tree structure'");
        return;
    }
    print(label, ": ok! nodes=", loaded.len, " root=", loaded[0].word, " x", loaded[0].count);
}

fn main() {
    var tree: WordNode[>..] = [];
    each_word("goose runs fast and goose stays safe") { insert_word(tree, it); };

    let blob = save_container(tree);
    print("container size=", blob.len, " bytes (header=16 + payload=", blob.len - 16, ")");

    // 1. Clean reload:
    try_load("clean reload", blob);

    // 2. Wrong magic:
    var bad_magic = copy(blob);
    bad_magic[0] = 'X';
    try_load("bad magic   ", bad_magic);

    // 3. 1-bit flip inside a scalar count — NOW caught by checksum!
    var flipped_scalar = copy(blob);
    flipped_scalar[flipped_scalar.len - 1] = flipped_scalar[flipped_scalar.len - 1] ^ 0x01;
    try_load("bit flip    ", flipped_scalar);

    // 4. Forged checksum + corrupted link — caught by `from_bytes` verifier!
    var forged_payload: u8[>..] = [];
    forged_payload.append(blob[16..]);
    let pfx = forged_payload.len - bytes_of(tree).len;
    forged_payload[pfx + 21] = forged_payload[pfx + 21] ^ 0x40; // inside root.left
    var forged: u8[>..] = [];
    forged.append(MAGIC);
    binary::write_u32le(forged, FORMAT_VERSION);
    binary::write_u64le(forged, hash(forged_payload));          // attacker recomputes hash!
    forged.append(forged_payload);
    try_load("forged link ", forged);
}
```

```
container size=192 bytes (header=16 + payload=176)
clean reload: ok! nodes=6 root=goose x2
bad magic   : err='bad magic'
bit flip    : err='checksum mismatch'
forged link : err='corrupt tree structure'
```

Notice test #4 (`forged link`): even when an attacker corrupts a link *and* recomputes a valid checksum for the tampered payload, `from_bytes` still catches the bad offset and safely rejects the image!

---

## Exercises

1. **Test zero-copy disk save (`bytes_of` + `format_uleb`):** Instead of calling `pool.to_bytes()`, build the framed header manually with `var head: u8[>..] = []; format_uleb(head, bytes_of(pool).len);`, concatenate `head` and `bytes_of(pool)`, and verify `from_bytes<WordNode[>..]>` loads it identically.
2. **Test `from_bytes` rejecting `T[>..<]` with relative links:** Try calling `from_bytes<WordNode[>..<]>(payload)` in Section 13.4 and inspect Goose's exact compile-time explanation of why grow-shrink arrays cannot load self-relative links.
3. **Add a lookup walk to `Tiny`:** Write a recursive `fn count_word(n: Tiny..&, target: const u8[:]) -> i64` for Section 13.3's variable-mode tree, and verify that querying `"fox"` and `"the"` on `reloaded` returns `2` and `3`.

---

## Key Takeaways

- Structures whose links are **self-relative** (`T&<uN>`, `T&<varint>`) are completely position-independent: **`bytes_of`** views their raw bytes without copying, **`to_bytes`** produces a ULEB128-framed image, and **`from_bytes<T[>..]>`** verifies and loads them back into ordinary arrays.
- `from_bytes` guarantees **memory safety** (framing, canonical varints, valid enum tags, inline lengths, and exact element-start targets for every link), while a container header + checksum guarantees **data integrity**.
- Both fixed-size structs and variable-mode enums (`Tiny..`) with `varint` strings and `varint` links serialize and verify seamlessly.

That completes **Part III: Abstraction, Control, and Data Structures**! In **Part IV: Concurrency, Interop, and Ecosystem**, we begin with **Chapter 14: Share-Nothing Concurrency**, exploring how Goose eliminates data races, locks, and atomics completely with `thread_fn`, typed queues, and contained workers.
