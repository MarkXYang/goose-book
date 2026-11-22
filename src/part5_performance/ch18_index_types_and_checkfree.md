# Chapter 18: Index Types, Bounds-Check Elimination, and `checkfree`

Every array and slice index in Goose is bounds-checked, and there is no `unsafe { get_unchecked(...) }` escape hatch. In tight inner loops—image filters, physics solvers, graph traversals—even a single surviving bounds check can block the C compiler from auto-vectorizing a loop or add branch overhead on every memory hop.

In most safe languages, bounds-check elimination (BCE) is a silent best-effort optimization: you tweak a loop, hope the optimizer dropped the checks, and never notice if a future edit quietly reintroduces them.

Goose turns that around with two cooperating features:

1. **Index and bound types (`uN in a`, `uN upto a`):** Move bounds checks out of hot indirect-indexing loops (`dist[edges[k]]`) to the single point where an index is constructed.
2. **`checkfree fn`:** Make "zero remaining bounds checks" a **compiler-enforced contract**—if any check survives anywhere inside the function (or any helper it calls), compilation fails and names the exact line.

By the end of this chapter, you will be able to:

- Audit bounds checks with `goose check --stats` and `goose check --bce-lines`.
- Guide BCE with entry `assert` preconditions, Euclidean `%`, and row slicing.
- Encode validated graph and table indices with **`uN in a`** and **`uN upto a`**.
- Guard performance-critical kernels with **`checkfree fn`** so regressions are caught at compile time.

---

## 18.1 How Goose's Bounds-Check Elimination Works

Before invoking the C backend, Goose runs a dedicated BCE pass (`bce.h`) that proves array indices and slice bounds in range using facts from:

- **Fixed dimensions:** Constant indices into fixed arrays `T[k]` (and `T[..k]` when `.len` is known).
- **Loop bounds:** `for i in a.len`, `for i in 1..W - 1`, and `for i in lo..hi`.
- **Preconditions:** `assert(...)` or `if` guards establishing lengths or index ranges once before a loop.
- **Euclidean `%`:** `x % n` is always in `[0, n)` for positive `n` (never negative!).
- **Grow-only monotonicity:** A length bound proven on a `T[>..]` still holds after `.push(...)`, because a grow-only array never shrinks silently.
- **Call-site propagation:** Facts proven by a caller flow into specialized callees.

Inspect BCE on any Goose program from the command line:

```bash
goose check --stats kernel.goose       # prints e.g.: bce: elided 10/10 index checks
goose check --bce-lines kernel.goose   # lists exact source lines where checks survived
```

### Guiding BCE in an Image Kernel

Here is a 3×3 row-slice box blur kernel. One `assert` at the top establishes all four row lengths, allowing BCE to eliminate **every single index check** inside the pixel loop—which we enforce permanently by marking the function **`checkfree fn`**:

```goose
let W = 8;

checkfree fn blur_row(above: const u8[:], here: const u8[:],
                      below: const u8[:], out: u8[:]) {
    assert(above.len == W && here.len == W && below.len == W && out.len == W);
    out[0] = here[0];
    out[W - 1] = here[W - 1];
    for x in 1..W - 1 {
        // Every index is `x - 1`, `x`, or `x + 1` against proven length W -> 0 checks!
        let sum = above[x - 1] as u16 + above[x] + above[x + 1]
                + here[x - 1]         + here[x]  + here[x + 1]
                + below[x - 1]        + below[x] + below[x + 1];
        out[x] = (sum / 9) as! u8;
    }
}

fn main() {
    let r0: u8[W] = [0, 9, 18, 27, 36, 45, 54, 63];
    let r1: u8[W] = [9, 18, 27, 36, 45, 54, 63, 72];
    let r2: u8[W] = [18, 27, 36, 45, 54, 63, 72, 81];
    var out: u8[W] = [0; W];
    blur_row(r0, r1, r2, out);
    // Print byte values numerically (a u8 array prints as text bytes directly):
    var nums: i64[W] = [0; W];
    for i in W { nums[i] = out[i]; }
    print("blurred: ", nums);
}
```

```
blurred: [9, 18, 27, 36, 45, 54, 63, 72]
```

Without that single `assert`, the compiler could not know whether `above`, `here`, `below`, and `out` all have length `W`, and `checkfree fn` would immediately refuse to compile it.

---

## 18.2 Index Types (`uN in a`) and Bound Types (`uN upto a`)

Local loop bounds cannot eliminate **indirect indexing**—the pattern at the heart of every graph, mesh, or relational table:

```text
dist[edges[k]]     // Does the integer stored inside `edges[k]` lie in `0..dist.len`?
```

No local loop analyzer can prove that, because validity depends on the *contents* of `edges` compared with the *length* of `dist`!

Goose solves this with **index types** and **bound types**:

- **`uN in a`** (`u8`, `u16`, `u32`, or `u64`): an unsigned integer guaranteed to satisfy **`0 <= v < a.len`** everywhere it can be observed.
- **`uN upto a`**: an unsigned integer guaranteed to satisfy **`0 <= v <= a.len`**—perfect for half-open range endpoints (`lo..hi`).

Here `a` names a global grow-only `var` array (or a mutable grow-only array field of a global `var` `root struct`, such as `u32 in pool.items`). The array name is part of the type's identity: `u32 in nodes` and `u32 in edges` are distinct types (so you can never accidentally index `edges` with a node ID!).

### The Five Rules of Index Types

1. **Construction (`x as (uN in a)`):** Moves the bounds check to construction time—aborts in *every* build if `x` is out of range (`as!` cannot forge an index type, and literals never adapt). Use a `type` alias (`type NodeId = u32 in nodes;`) so every use reads cleanly.
2. **Check-free indexing:** Indexing `a[id]` needs **zero** bounds checks. Indexing a *parallel* array `weights[id]` is also check-free once you `assert(weights.len >= a.len)`!
3. **Ranges carry index types:** `for k in lo..hi` where `lo` and `hi` have type `u32 upto edges` automatically gives the loop counter `k` type **`u32 in edges`**!
4. **Never shrunk:** Any array named by an index type cannot be shrunk anywhere in the program, guaranteeing every previously validated index remains valid forever.
5. **Plain integer on read:** An index type widens/converts implicitly to its underlying `uN` for arithmetic (returning a plain integer) and has no default value; `from_bytes` automatically verifies every index value in a loaded image!

```goose
struct Node {
    lo: u32 upto edges,              // start offset into `edges` (<= edges.len)
    hi: u32 upto edges,              // exclusive end offset into `edges` (<= edges.len)
    weight: i32,
}

var nodes: Node[>..] = [];
type NodeId = u32 in nodes;          // validated index into `nodes` (< nodes.len)
var edges: NodeId[>..] = [];

fn add_node(w: i32) -> NodeId {
    let empty = 0 as (u32 upto edges);
    nodes.push(Node { lo: empty, hi: empty, weight: w });
    return (nodes.len - 1) as NodeId;
}

checkfree fn neighbor_weight_sum(weights: const i32[:]) -> i64 {
    assert(weights.len >= nodes.len);
    var total = 0;
    for v in nodes.len {
        let n = nodes[v];
        // `k` automatically has type `u32 in edges`, and `edges[k]` has type `NodeId`!
        for k in n.lo..n.hi {
            let dst = edges[k];
            total += nodes[dst].weight + weights[dst];
        }
    }
    return total;
}

fn main() {
    for w in [5, 3, 8, 1] { add_node(w as i32); }
    for v in nodes.len {
        nodes[v].lo = edges.len as (u32 upto edges);
        for d in 1..3 {
            if v + d < nodes.len { edges.push((v + d) as NodeId); }
        }
        nodes[v].hi = edges.len as (u32 upto edges);
    }

    let extra: i32[4] = [10, 20, 30, 40];
    let sum = neighbor_weight_sum(extra);
    let plain: u32 = edges[0];       // converts implicitly to plain u32 for math
    print("sum=", sum, " first_edge=", plain, " next=", plain + 1);
}
```

```
sum=181 first_edge=1 next=2
```

Look closely at `neighbor_weight_sum`: **both `edges[k]` and `nodes[dst]` and `weights[dst]` compile with zero bounds checks**, verified permanently by `checkfree fn`!

---

## 18.3 Enforcing Contracts with `checkfree fn`

Marking a function `checkfree fn` does not disable bounds checks—it asks the compiler to **prove** that zero bounds checks remain inside it (and inside every helper function or trailing block it calls) after BCE!

If someone later refactors the code and accidentally drops a precondition `assert`, compilation fails immediately instead of silently regressing performance:

<!-- compile-fail -->
```goose
checkfree fn sum_pairs(a: const i64[:], b: const i64[:]) -> i64 {
    // Missing `assert(b.len >= a.len);` — `b[i]` retains a bounds check!
    var total = 0;
    for i in a.len {
        total += a[i] + b[i];        // error: bounds check remains in checkfree fn
    }
    return total;
}

fn main() {
    print(sum_pairs([1, 2, 3], [4, 5, 6]));
}
```

> **Compiler Tip:** Inside a `checkfree fn`, every standard library math/vector/matrix/quaternion helper (`std` bit ops, `vec`, `mat` except `row`, `quat`, `min`/`max` over slices) is already check-free, so you can call them directly inside `checkfree` kernels without any annotation!

---

## 18.4 Worked Example: A Provably Check-Free CSR Graph BFS

Compressed Sparse Row (CSR) is the gold-standard cache-friendly representation for static graphs: each vertex stores a half-open range `[lo..hi)` into one flat `edges` array.

Combine CSR with `NodeId = u32 in g_nodes` and `EdgeBound = u32 upto g_edges`, and watch **`checkfree fn bfs`** traverse an entire graph with **zero bounds checks**—using 4-byte vertex IDs and zero heap allocations:

```goose
struct Vertex {
    lo: u32 upto g_edges,
    hi: u32 upto g_edges,
}

var g_nodes: Vertex[>..] = [];
type NodeId = u32 in g_nodes;
var g_edges: NodeId[>..] = [];

// Build a graph from an adjacency list of raw integers; validation happens ONCE here.
fn build_graph(num_nodes: i64, raw_edges: const i32[2][:]) {
    let zero_bound = 0 as (u32 upto g_edges);
    for _ in num_nodes {
        g_nodes.push(Vertex { lo: zero_bound, hi: zero_bound });
    }
    for v in g_nodes.len {
        g_nodes[v].lo = g_edges.len as (u32 upto g_edges);
        for e in raw_edges {
            if e[0] == v {
                g_edges.push(e[1] as NodeId);    // bounds-checked ONCE at graph build time
            }
        }
        g_nodes[v].hi = g_edges.len as (u32 upto g_edges);
    }
}

// 100% bounds-check free BFS, enforced by the compiler!
checkfree fn bfs(start: NodeId, dist: i32[:], queue: NodeId[>..]&) -> i64 {
    assert(dist.len >= g_nodes.len);
    for i in dist.len { dist[i] = -1; }

    dist[start] = 0;
    queue.push(start);
    var head = 0;
    var visited = 0;

    while head < queue.len {
        let u = queue[head];                     // proven < queue.len by loop guard
        head++;
        visited++;
        let next_d = dist[u] + 1;                // `u` is NodeId -> proven < g_nodes.len <= dist.len
        let vtx = g_nodes[u];                    // `u` is NodeId -> 0 checks
        for k in vtx.lo..vtx.hi {                // `k` is `u32 in g_edges` -> 0 checks
            let nb = g_edges[k];                 // `nb` is `NodeId` -> 0 checks
            if dist[nb] < 0 {
                dist[nb] = next_d;               // `nb` is `NodeId` -> 0 checks
                queue.push(nb);
            }
        }
    }
    return visited;
}

fn main() {
    // 6-node directed graph (0 -> 1, 2; 1 -> 3; 2 -> 3, 4; 4 -> 5):
    let raw: i32[2][6] = [
        [0, 1], [0, 2], [1, 3], [2, 3], [2, 4], [4, 5],
    ];
    build_graph(6, raw);

    var dist: i32[>..] = [];
    dist.resize(g_nodes.len, -1);
    var queue: NodeId[>..] = [];

    let reached = bfs(0 as NodeId, dist, queue);
    print("reached=", reached, " distances=", dist);
}
```

```
reached=6 distances=[0, 1, 1, 2, 2, 3]
```

Not a single bounds check survives anywhere inside `bfs`—and unlike C or `unsafe` Rust, the compiler **guarantees** that no out-of-bounds memory access is possible even if someone feeds malformed edge indices into `build_graph` (which catches them cleanly at `e[1] as NodeId`).

---

## Exercises

1. **Test `checkfree` enforcement:** In Section 18.4, comment out `assert(dist.len >= g_nodes.len);` inside `bfs` and click **▶ Run**. Read the exact lines Goose flags where bounds checks reappeared!
2. **Test the "never shrunk" invariant:** In Section 18.2, try calling `nodes.pop();` at the end of `main()`. Observe Goose rejecting the shrink because `NodeId` (`u32 in nodes`) depends on `nodes` never shrinking.
3. **Write a check-free 1D moving average:** Write `checkfree fn smooth3(src: const f32[:], dst: f32[:])` that asserts `src.len >= 3 && dst.len == src.len`, sets endpoints `dst[0]` and `dst[src.len - 1]`, and averages `src[i - 1]`, `src[i]`, `src[i + 1]` for `i in 1..src.len - 1` with zero surviving bounds checks.

---

## Key Takeaways

- Goose checks every index for safety and runs a static **BCE** pass; inspect results with `goose check --stats` and `goose check --bce-lines`.
- **Index types (`uN in a`)** and **bound types (`uN upto a`)** validate indirect indices *once* at construction time and keep every downstream lookup (`a[id]`, `b[id]` after `assert(b.len >= a.len)`, and `for k in lo..hi`) completely check-free.
- **`checkfree fn`** enforces zero surviving bounds checks as a compile-time contract across the function, its helpers, and its blocks—giving you C-speed inner loops with zero `unsafe`.

In **Chapter 19: Writing Fast Goose**, we look at the full performance picture: data layout choices, `inline fn`, `simd fn` (AVX2/AVX-512 multi-versioning), reading generated C, and honest lessons from Goose's 16-benchmark suite!
