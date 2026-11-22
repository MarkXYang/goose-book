# Chapter 20: Capstone 1 — A Compact JSON Parser, Query Engine, and Binary Cache

In a traditional systems language, building a JSON AST means deciding between two unpleasant extremes: heap-allocating every node (`std::unique_ptr` / `Box` + `std::string` + `std::vector`) and paying for hundreds of tiny allocations and an $O(n)$ teardown walk, or writing a sprawling custom byte-arena with raw offsets and unsafe casts.

In Goose, the most idiomatic representation *is* the compact, zero-allocation, directly serializable representation. In this first capstone project, we will build a complete **JSON parser, dot-path query engine, pretty/compact formatter, and verified binary cache** in a single cohesive program.

### Concepts Synthesized in This Capstone

| Feature | Chapter | Role in This Project |
|---|---|---|
| Variable-mode ADTs (`Json..`) | Ch. 7 | Each JSON node occupies only the exact bytes of its active variant—including inline `u8[varint]` keys and strings. |
| 4-byte self-relative links (`Node&<u32>?`) | Ch. 12 | Chains object members and array elements inside one flat grow-only pool with zero pointer-size bloat and zero relocation cost. |
| Nested recursive-descent driver | Ch. 11 | The non-recursive `parse_json` owns the cursor and scratch key buffer; nested `recursive fn` parsers borrow them with zero data-stack locals across recursion. |
| Long-distance `return … from` | Ch. 10 | Any syntax error at any nesting depth computes `(line, col)` lazily and returns straight out of `parse_json` with zero `Result` plumbing. |
| Specialization-time `const for` | Ch. 9 | Counts AST nodes by variant over `variants<Json..>` automatically—add a new variant and the profiler updates itself. |
| Verified persistence (`to_bytes` / `from_bytes`) | Ch. 13 | Saves the parsed AST verbatim behind a magic + version + FNV-1a header and reloads it into a live queryable tree without re-parsing JSON. |

---

## 1. Designing the Flat, Self-Relative AST

Every JSON value in a document lives inside a single grow-only array: `Node[>..]`.

```goose
struct Node {
    next: Node&<u32>?,   // 4-byte self-relative link to next sibling (or null)
    key:  u8[varint],    // member name (empty for array elements / root)
    val:  Json..,        // variable-mode payload: exact size of active variant
}

enum Json {
    Null,
    Bool { v: bool },
    Num  { v: f64 },
    Str  { s: u8[varint] },
    Arr  { first: Node&<u32>?, count: varint },
    Obj  { first: Node&<u32>?, count: varint },
}
```

Look closely at what this layout achieves:

1. **Zero per-node allocations:** A `Null` element inside an array takes only 6 bytes total (4-byte `next` + 1-byte zero-length `key` + 1-byte `Null` tag). A `Num` takes 14 bytes. A string stores its bytes inline right after its varint length.
2. **Post-order construction with sibling patching:** Because `Node` is variable-sized, children are parsed and pushed *before* their enclosing `Arr` or `Obj`. As each sibling finishes, we patch `last.next .= child`—legal because `next` is a fixed-width relative reference field!
3. **The $O(1)$ sentinel root trick:** Because a variable-mode array (`Node[>..]`) cannot be randomly indexed (`pool[pool.len - 1]`), how do we find the root node instantaneously after reloading from disk with `from_bytes<Node[>..]>`?
   Before parsing begins, we push **one sentinel `Node` at position 0** (`Node { next: null, key: "", val: Json.Null }`). After `parse_value` finishes and returns the document's `root`, we patch `sentinel.next .= root`. When we reload the binary image later, a single step of `for n in pool { return n.next; }` recovers the live root node in **$O(1)$ time**—without scanning a single node!

```text
Node[>..] contiguous memory image:
┌─────────────────────────┬──────────────┬──────────────┬─────────────────────────┐
│ [0] Sentinel            │ child 0      │ child 1      │ Root Obj                │
│ next ───────────────┐   │ next ──►     │ next: null   │ first ──► child 0       │
│ key:""  val:Null    │   │ key:"a" 1.0  │ key:"b" "hi" │ count:2                 │
└─────────────────────┼───┴──────────────┴──────────────┴▲────────────────────────┘
                      └──────────────────────────────────┘ (4-byte relative offset)
```

---

## 2. Nested Recursive Descent & Lazy `(line, col)` Diagnostics

Remember Chapter 11's golden rule for recursion in Goose: **a function inside a recursive cycle cannot hold a data-stack local across a recursive call**. Placing the recursive parsers *inside* a non-recursive driver `parse_json(pool, text)` solves this effortlessly:

- `parse_json` owns `var pos = 0;` and `var keys: u8[>..] = [];` (a grow-only scratch buffer for unescaped object keys) on its own stack frame, which outlives every recursive activation.
- `parse_string()` is *not* part of the recursive cycle (strings never contain nested JSON values), so it can safely own a local `var s: u8[>..] = [];` and return it directly into `Json.Str { s: parse_string() }` via construct-in-destination!
- `parse_child` and `parse_value` form the recursive cycle (`recursive fn`), holding only fixed-size locals (`Node?`, `i64`, `bool`, `u8`) while sharing `pos`, `keys`, and `pool`.
- On any syntax error, `fail(what)` scans `text[0..pos]` *only on the error path* to compute 1-based `line` and `col`, then executes `return null, fail(...) from parse_json;`—instantly unwinding any nesting depth with zero runtime overhead on valid JSON.

---

## 3. Dot-Path Queries, `const for` Profiling, and Formatting

Once parsed (or reloaded from a binary cache), traversing the AST uses ordinary optional references (`const Node?`):

- **`member(n, name)`** walks an `Obj`'s `first` → `next` chain looking for `cur.key == name`.
- **`element(n, index)`** walks an `Arr`'s `first` → `next` chain to position `index`.
- **`query_path(root, path)`** splits a dot-separated query like `"cluster.nodes.1.ports.0"`—matching object keys on `Obj` nodes and decimal indices on `Arr` nodes—returning `const Node?`.
- **`print_variant_stats(pool)`** uses `const for <N, V> in variants<Json..>` inside a `match` to tally every variant name and count automatically (skipping the sentinel node at index 0).
- **`render_json(n, out, indent)`** walks any subtree and writes compact (`indent < 0`) or indented (`indent >= 0`) JSON straight into a `u8[>..]&` builder.

---

## 4. Verified Binary Caching with `to_bytes` and `from_bytes`

Because every link inside `Node[>..]` is self-relative (`Node&<u32>?`) and every string is inline (`u8[varint]`), **`Node[>..]` is directly serializable with zero conversion code**.

To make the binary cache production-grade, we wrap the `to_bytes(pool)` payload in a 16-byte container using `std`'s `binary` module:

```text
Byte offset:   0..4          4..8            8..16               16..end
             ┌─────────────┬───────────────┬───────────────────┬────────────────────┐
             │ Magic "GJSN"│ Version = 1   │ FNV-1a u64 hash   │ to_bytes(pool)     │
             └─────────────┴───────────────┴───────────────────┴────────────────────┘
```

Recall Chapter 13's two-layer defense:
1. **Integrity layer (`unwrap_cache`):** checks magic bytes, version, and FNV-1a checksum so accidental truncation, version mismatch, or bit-flips are caught immediately with a descriptive error.
2. **Memory-safety layer (`from_bytes<Node[>..]>`):** even if an attacker crafts a valid checksum over a hostile payload, Goose's built-in verifier checks every varint, enum tag, boolean byte, array bound, and self-relative `Node&<u32>?` offset (confirming every non-null link lands on an actual element start inside the pool) before a single byte becomes a live `Node[>..]`.

Notice one subtle Goose detail from Chapter 13: because `Node` contains self-relative references, `Node[>..]` cannot be copied across branches of a `return` statement. Keeping `unwrap_cache(blob)` as a pure header-stripping function returning `(const u8[:], u8[>..])` lets the caller invoke `let loaded, ok = from_bytes<Node[>..]>(payload);` directly into its own destination!

---

## 5. Complete Runnable Implementation

Here is the full Capstone 1 program—parser, query engine, `const for` variant profiler, formatter, and verified binary cache—ready to run and experiment with:

```goose
import std;
import binary;

// --- 1. Compact, self-relative AST ------------------------------------

struct Node {
    next: Node&<u32>?,
    key:  u8[varint],
    val:  Json..,
}

enum Json {
    Null,
    Bool { v: bool },
    Num  { v: f64 },
    Str  { s: u8[varint] },
    Arr  { first: Node&<u32>?, count: varint },
    Obj  { first: Node&<u32>?, count: varint },
}

// Recover the document root from sentinel node 0 in O(1) time.
fn doc_root(pool: const Node[>..]& ) -> const Node? {
    for n in pool {
        return n.next;
    }
    return null;
}

// --- 2. Nested recursive-descent parser -------------------------------

fn parse_json(pool: Node[>..]&, text: const u8[:]) -> Node?, u8[>..] {
    var pos = 0;
    var keys: u8[>..] = [];

    fn skip_space() {
        while pos < text.len
              && (text[pos] == ' ' || text[pos] == '\n'
                  || text[pos] == '\r' || text[pos] == '\t') {
            pos++;
        }
    }

    // Compute 1-based line and column lazily on the error path only.
    fn fail(what: const u8[:]) -> u8[>..] {
        var line = 1;
        var col = 1;
        for i in pos {
            if text[i] == '\n' {
                line += 1;
                col = 1;
            } else {
                col += 1;
            }
        }
        return str(what, " at line ", line, ", col ", col);
    }

    fn expect(c: u8) {
        skip_space();
        if pos < text.len && text[pos] == c {
            pos++;
            return;
        }
        var msg: u8[>..] = [];
        format(msg, "expected '");
        msg.push(c);
        msg.push('\'');
        return null, fail(msg) from parse_json;
    }

    // Non-recursive: may own a local builder constructed directly into the AST.
    fn parse_string() -> u8[>..] {
        expect('"');
        var s: u8[>..] = [];
        loop {
            guard pos < text.len else {
                return null, fail("unterminated string") from parse_json;
            }
            let c = text[pos];
            pos++;
            if c == '"' { break; }
            if c != '\\' {
                s.push(c);
                continue;
            }
            guard pos < text.len else {
                return null, fail("unterminated escape") from parse_json;
            }
            let e = text[pos];
            pos++;
            if e == 'n' { s.push('\n'); }
            else if e == 't' { s.push('\t'); }
            else if e == 'r' { s.push('\r'); }
            else if e == '"' || e == '\\' || e == '/' { s.push(e); }
            else { return null, fail("unsupported escape") from parse_json; }
        }
        return s;
    }

    fn parse_number() -> f64 {
        let start = pos;
        while pos < text.len
              && (ascii::is_digit(text[pos]) || text[pos] == '-'
                  || text[pos] == '+' || text[pos] == '.'
                  || text[pos] == 'e' || text[pos] == 'E') {
            pos++;
        }
        let v, ok = parse_flt(text[start..pos]);
        guard ok else {
            return null, fail("invalid number") from parse_json;
        }
        return v;
    }

    fn parse_word(w: const u8[:]) {
        guard pos + w.len <= text.len && text[pos..pos + w.len] == w else {
            return null, fail("unexpected token") from parse_json;
        }
        pos += w.len;
    }

    recursive fn parse_child(is_obj: bool) -> Node& {
        if is_obj {
            let start = keys.len;
            keys.append(parse_string());
            expect(':');
            return parse_value(keys[start..]);
        }
        return parse_value("");
    }

    recursive fn parse_value(key: const u8[:]) -> Node& {
        skip_space();
        guard pos < text.len else {
            return null, fail("unexpected end of input") from parse_json;
        }
        let c = text[pos];
        if c == '{' || c == '[' {
            let is_obj = c == '{';
            let close = if is_obj { '}' } else { ']' };
            pos++;
            skip_space();
            guard pos < text.len else {
                return null, fail("unclosed container") from parse_json;
            }
            var first: Node? = null;
            var count = 0;
            if text[pos] != close {
                let head .= parse_child(is_obj);
                first .= head;
                var last .= head;
                count = 1;
                loop {
                    skip_space();
                    guard pos < text.len else {
                        return null, fail("unclosed container") from parse_json;
                    }
                    if text[pos] == close { break; }
                    expect(',');
                    let child .= parse_child(is_obj);
                    last.next .= child;
                    last .= child;
                    count++;
                }
            }
            pos++;
            if is_obj {
                return pool.push(Node {
                    next: null,
                    key: key,
                    val: Json.Obj { first: first, count: count },
                });
            }
            return pool.push(Node {
                next: null,
                key: key,
                val: Json.Arr { first: first, count: count },
            });
        }
        if c == '"' {
            return pool.push(Node { next: null, key: key, val: Json.Str { s: parse_string() } });
        }
        if c == 't' {
            parse_word("true");
            return pool.push(Node { next: null, key: key, val: Json.Bool { v: true } });
        }
        if c == 'f' {
            parse_word("false");
            return pool.push(Node { next: null, key: key, val: Json.Bool { v: false } });
        }
        if c == 'n' {
            parse_word("null");
            return pool.push(Node { next: null, key: key, val: Json.Null });
        }
        return pool.push(Node { next: null, key: key, val: Json.Num { v: parse_number() } });
    }

    // Push sentinel node at position 0 so doc_root() is O(1) after reload.
    let sentinel .= pool.push(Node { next: null, key: "", val: Json.Null });
    let root .= parse_value("");
    skip_space();
    guard pos == text.len else {
        return null, fail("trailing input") from parse_json;
    }
    sentinel.next .= root;
    return root, str("");
}

// --- 3. Query engine & compact/pretty formatter -----------------------

fn member(n: const Node&, name: const u8[:]) -> const Node? {
    match n.val {
        Obj &o => {
            var cur = o.first;
            while cur {
                if cur.key == name { return cur; }
                cur .= cur.next;
            }
        },
        _ => {},
    }
    return null;
}

fn element(n: const Node&, target: i64) -> const Node? {
    if target < 0 { return null; }
    match n.val {
        Arr &a => {
            var cur = a.first;
            var idx = 0;
            while cur {
                if idx == target { return cur; }
                cur .= cur.next;
                idx++;
            }
        },
        _ => {},
    }
    return null;
}

fn query_path(root: const Node&, path: const u8[:]) -> const Node? {
    var cur: const Node? = root;
    var start = 0;
    while start <= path.len {
        var end = start;
        while end < path.len && path[end] != '.' { end++; }
        let seg = path[start..end];
        guard cur else { return null; }
        match cur.val {
            Obj &o => {
                // Suppress unused binding warning by delegating to member().
                let _ = o.count;
                cur .= member(cur, seg);
            },
            Arr &a => {
                let _ = a.count;
                let idx, ok = parse_int(seg);
                if !ok { return null; }
                cur .= element(cur, idx);
            },
            _ => { return null; },
        }
        if end == path.len { break; }
        start = end + 1;
    }
    return cur;
}

fn write_quoted(out: u8[>..]&, s: const u8[:]) {
    out.push('"');
    for c in s {
        if c == '"' || c == '\\' {
            out.push('\\');
            out.push(c);
        } else if c == '\n' {
            out.append("\\n");
        } else if c == '\t' {
            out.append("\\t");
        } else {
            out.push(c);
        }
    }
    out.push('"');
}

recursive fn render_json(n: const Node&, out: u8[>..]&) {
    match n.val {
        Null => out.append("null"),
        Bool &b => out.append(if b.v { "true" } else { "false" }),
        Num &x => {
            let iv = x.v as! i64;
            if iv == x.v { format(out, iv); } else { format(out, x.v); }
        },
        Str &s => write_quoted(out, s.s),
        Arr &a => {
            out.push('[');
            var cur = a.first;
            var first = true;
            while cur {
                if !first { out.push(','); }
                first = false;
                render_json(cur, out);
                cur .= cur.next;
            }
            out.push(']');
        },
        Obj &o => {
            out.push('{');
            var cur = o.first;
            var first = true;
            while cur {
                if !first { out.push(','); }
                first = false;
                write_quoted(out, cur.key);
                out.push(':');
                render_json(cur, out);
                cur .= cur.next;
            }
            out.push('}');
        },
    }
}

fn node_to_str(n: const Node&) -> u8[>..] {
    var out: u8[>..] = [];
    render_json(n, out);
    return out;
}

// Compile-time variant profiler using `const for` over `variants<Json..>`.
fn print_variant_stats(pool: const Node[>..]&) {
    var counts: i64[6] = [0; 6];
    var skip_sentinel = true;
    for n in pool {
        if skip_sentinel {
            skip_sentinel = false;
            continue;
        }
        match n.val {
            const for <N, V> in variants<Json..> {
                V => {
                    var slot = 0;
                    const for <M, W> in variants<Json..> {
                        const if same<V, W> {
                            counts[slot] += 1;
                        }
                        slot += 1;
                    }
                },
            }
        }
    }
    var idx = 0;
    var summary: u8[>..] = [];
    const for <N, V> in variants<Json..> {
        if idx > 0 { summary.append(", "); }
        format(summary, N, "=", counts[idx]);
        idx += 1;
    }
    print("variants: ", summary);
}

// --- 4. Verified binary cache container -------------------------------

const CACHE_MAGIC: u32 = 0x4E534A47; // "GJSN" little-endian
const CACHE_VER:   u32 = 1;
const HEADER_LEN = 16;

fn fnv1a(data: const u8[:]) -> u64 {
    var h: u64 = 14695981039346656037;
    for b in data {
        h ^= b;
        h *= 1099511628211;
    }
    return h;
}

fn pack_cache(pool: const Node[>..]&) -> u8[>..] {
    var payload: u8[>..] = [];
    to_bytes(pool, payload);
    let checksum = fnv1a(payload);

    var out: u8[>..] = [];
    binary::write_u32le(out, CACHE_MAGIC);
    binary::write_u32le(out, CACHE_VER);
    binary::write_u64le(out, checksum);
    out.append(payload);
    return out;
}

fn unwrap_cache(blob: const u8[:]) -> const u8[:], u8[>..] {
    if blob.len < HEADER_LEN {
        return blob[..0], str("truncated cache header");
    }
    let magic, ok1 = binary::read_u32le(blob, 0);
    let ver, ok2 = binary::read_u32le(blob, 4);
    let want_hash, ok3 = binary::read_u64le(blob, 8);
    if !ok1 || !ok2 || !ok3 || magic != CACHE_MAGIC {
        return blob[..0], str("bad cache magic");
    }
    if ver != CACHE_VER {
        return blob[..0], str("unsupported cache version ", ver);
    }
    let payload = blob[HEADER_LEN..];
    if fnv1a(payload) != want_hash {
        return blob[..0], str("checksum mismatch");
    }
    return payload, str("");
}

// --- 5. Demo driver ---------------------------------------------------

fn main() {
    let sample = """
{
  "cluster": {
    "name": "north-eu",
    "replicas": 3,
    "tls": true,
    "nodes": [
      { "host": "10.0.1.10", "ports": [80, 443], "note": "primary\tready" },
      { "host": "10.0.1.11", "ports": [8080, 8443], "note": null }
    ]
  }
}
""";

    var pool: Node[>..] = [];
    let root, err = parse_json(pool, sample);
    guard root else {
        print("parse error: ", err);
        return;
    }

    print("input bytes: ", sample.len, ", AST bytes: ", bytes_of(pool).len);
    print_variant_stats(pool);

    // Run dot-path queries on the freshly parsed AST.
    let q1 = query_path(root, "cluster.name");
    let q2 = query_path(root, "cluster.nodes.1.host");
    let q3 = query_path(root, "cluster.nodes.1.ports.0");
    if q1 && q2 && q3 {
        print("query cluster.name           = ", node_to_str(q1));
        print("query cluster.nodes.1.host   = ", node_to_str(q2));
        print("query cluster.nodes.1.ports.0 = ", node_to_str(q3));
    }

    // Demonstrate deep syntax error with line/col via `return ... from`.
    var bad_pool: Node[>..] = [];
    let bad_src = "{\n  \"ok\": [1, 2, ]\n}";
    let bad_root, bad_err = parse_json(bad_pool, bad_src);
    if !bad_root {
        print("error report: ", bad_err);
    }

    // Pack AST to verified binary cache, reload with ZERO JSON parsing.
    let cache_blob = pack_cache(pool);
    print("cache image bytes: ", cache_blob.len);

    let payload, uerr = unwrap_cache(cache_blob);
    guard uerr.len == 0 else {
        print("cache error: ", uerr);
        return;
    }
    let loaded, ok = from_bytes<Node[>..]>(payload);
    assert(ok);

    let loaded_root = doc_root(loaded);
    if loaded_root {
        let same = node_to_str(loaded_root) == node_to_str(root);
        print("reloaded round-trip exact: ", same);
        let rq = query_path(loaded_root, "cluster.nodes.0.note");
        if rq {
            print("reloaded query note: ", node_to_str(rq));
        }
    }

    // Tamper test 1: flip a payload byte -> caught by FNV-1a checksum.
    var tampered = copy(cache_blob);
    tampered[HEADER_LEN + 2] ^= 0xFF;
    let _, terr = unwrap_cache(tampered);
    print("tamper checksum test: ", terr);

    // Tamper test 2: bypass checksum with corrupted raw payload -> caught by from_bytes!
    var raw_bad: u8[>..] = [];
    raw_bad.append(payload);
    raw_bad[raw_bad.len - 1] = 250; // invalid tag / truncated structure
    let _, bad_ok = from_bytes<Node[>..]>(raw_bad);
    print("structural verify rejected hostile payload: ", !bad_ok);
}
```

```text
input bytes: 247, AST bytes: 286
variants: Null=1, Bool=1, Num=5, Str=4, Arr=3, Obj=4
query cluster.name           = "north-eu"
query cluster.nodes.1.host   = "10.0.1.11"
query cluster.nodes.1.ports.0 = 8080
error report: invalid number at line 2, col 16
cache image bytes: 304
reloaded round-trip exact: true
reloaded query note: "primary\tready"
tamper checksum test: checksum mismatch
structural verify rejected hostile payload: true
```

Pause and admire that first line of output: **the entire parsed, typed, linked, queryable AST—18 nodes plus the sentinel, with sibling links, keys, strings, IEEE-754 doubles, and containers—occupies just 286 bytes in RAM (1.16× the 247-byte raw JSON text)**, compared to 5×–10× overhead for heap-allocated DOM trees in typical languages. And reloading it from the 304-byte binary cache verifies every structural invariant and restores $O(1)$ root lookup without parsing a single character of JSON.

---

## Exercises

1. **Add pretty-printing with indentation:** Extend `render_json` with an `indent: i64` parameter (where `indent < 0` emits compact JSON and `indent >= 0` emits 2-space indented JSON with newlines around non-empty arrays and objects).
2. **Add `\uXXXX` Unicode escape decoding:** Implement `push_utf8(out: u8[>..]&, cp: i64)` and handle `\u` escapes in `parse_string()` using `ascii::hex_value(c)`. Test it on `"caf\u00e9"`.
3. **Add CLI disk caching via `os`:** Write a wrapper `main()` that accepts a `.json` file path via `os::args()`, checks whether `<path>.gjsn` already exists using `os::read_file`, loads the binary cache if valid, and otherwise parses the JSON and writes `<path>.gjsn` with `os::write_file`.
4. **Add a wildcard array query:** Extend `query_path` (or add `collect_field(node, key, out)`) so you can collect every `"host"` string across all elements of `"cluster.nodes"` into a `u8[>..]` report.

---

## Key Takeaways

- **One grow-only array + `Json..` + `Node&<u32>?`** turns a pointer-heavy AST into a single flat block smaller than the source text, with zero heap allocations and $O(1)$ teardown.
- **Sentinel node 0** gives $O(1)$ root recovery on a sequential variable-mode array after `from_bytes<Node[>..]>`.
- **Nested recursive-descent functions** inside a non-recursive driver keep recursion-stack rules effortless while `return … from` delivers rich `(line, col)` diagnostics without threading error types through every parser function.
- **Two-layer binary caching** combines application-level framing and checksums (`binary` + FNV-1a) with Goose's built-in `from_bytes` structural verifier so persisted ASTs are both tamper-evident and memory-safe.
