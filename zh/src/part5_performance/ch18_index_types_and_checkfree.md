# 第 18 章：索引类型、越界检查消除与 `checkfree`

Goose 中的每一次数组和切片索引访问都会进行越界检查，而且不存在 `unsafe { get_unchecked(...) }` 这样的逃生口。在紧凑的内层循环中——例如图像滤波器、物理求解器、图遍历——哪怕仅剩一次未被消除的越界检查，都可能阻止 C 编译器对循环进行自动向量化，或在每次内存跳转时引入分支开销。

在大多数安全语言中，越界检查消除（BCE）是一种静默的尽力而为优化：你反复调整循环写法，寄希望于优化器去掉了检查，但如果日后的某次修改悄悄重新引入了越界检查，你却浑然不觉。

Goose 通过两项协同工作的特性彻底扭转了这一局面：

1. **索引类型与上界类型（`uN in a`、`uN upto a`）：**将越界检查从高频的间接索引循环（`dist[edges[k]]`）中移出，集中到索引构造的那唯一一处。
2. **无检查函数（`checkfree fn`）：**将“零残留越界检查”设为**由编译器强制执行的契约**——只要函数内部（或其调用的任何辅助函数内部）哪怕残留了一次越界检查，编译就会失败并精确指出所在行号。

学完本章后，你将能够：

- 使用 `goose check --stats` 和 `goose check --bce-lines` 审计越界检查。
- 借助入口处的 `assert` 前置条件、欧几里得取模 `%` 和行切片来引导 BCE。
- 使用 **`uN in a`** 和 **`uN upto a`** 编码经过验证的图和表索引。
- 使用 **`checkfree fn`** 守护性能关键型核心函数，使性能退化在编译期即被捕获。

---

## 18.1 Goose 的越界检查消除原理

在调用 C 后端之前，Goose 会运行一趟专用的 BCE 遍（`bce.h`），利用以下来源的事实来证明数组索引和切片边界处于合法范围内：

- **固定维度：**对定长数组 `T[k]`（以及 `.len` 已知时的定容数组 `T[..k]`）的常量索引。
- **循环边界：**`for i in a.len`、`for i in 1..W - 1` 以及 `for i in lo..hi`。
- **前置条件：**在循环之前通过 `assert(...)` 或 `if` 守卫一次性确立的长度或索引范围。
- **欧几里得取模 `%`：**对于正数 `n`，`x % n` 始终位于 `[0, n)` 区间内（绝不会为负！）。
- **只增单调性：**在只增数组 `T[>..]` 上已经证明的长度下界在调用 `.push(...)` 之后依然成立，因为只增数组绝不会暗中缩容。
- **调用点传播：**调用方已证明的事实会流入特化的被调用函数中。

你可以在命令行中检查任意 Goose 程序的 BCE 情况：

```bash
goose check --stats kernel.goose       # prints e.g.: bce: elided 10/10 index checks
goose check --bce-lines kernel.goose   # lists exact source lines where checks survived
```

### 在图像核函数中引导 BCE

下面是一个 3×3 行切片均值模糊（box blur）核函数。开头的一条 `assert` 确立了全部四行的长度，使 BCE 能够消除像素循环中的**每一次索引检查**——而我们通过将函数标记为 **`checkfree fn`** 来永久强制这一保证：

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

如果没有这唯一的一条 `assert`，编译器就无法确定 `above`、`here`、`below` 和 `out` 的长度是否都等于 `W`，`checkfree fn` 也会立刻拒绝编译它。

---

## 18.2 索引类型（`uN in a`）与上界类型（`uN upto a`）

局部循环边界无法消除**间接索引**——而这正是每一种图、网格或关系表的核心访问模式：

```text
dist[edges[k]]     // Does the integer stored inside `edges[k]` lie in `0..dist.len`?
```

任何局部循环分析器都无法证明这一点，因为合法性取决于 `edges` 中存储的*内容*与 `dist` 的*长度*之间的关系！

Goose 通过**索引类型**与**上界类型**来解决这一问题：

- **`uN in a`**（`u8`、`u16`、`u32` 或 `u64`）：一个无符号整数，在任何可被观察到的地方都保证满足 **`0 <= v < a.len`**。
- **`uN upto a`**：一个无符号整数，保证满足 **`0 <= v <= a.len`**——非常适合表示半开区间的端点（`lo..hi`）。

这里的 `a` 指代一个全局的只增 `var` 数组（或全局 `var` 根结构体（`root struct`）中的可变只增数组字段，例如 `u32 in pool.items`）。数组名是类型标识的一部分：`u32 in nodes` 与 `u32 in edges` 是互不相同的类型（因此你绝不会不小心拿着节点 ID 去索引 `edges`！）。

### 索引类型的五条规则

1. **构造（`x as (uN in a)`）：**将越界检查前移至构造阶段——如果 `x` 超出范围，在*任何*构建模式下都会中止程序（`as!` 无法伪造索引类型，字面量也绝不会自动适配）。使用 `type` 别名（`type NodeId = u32 in nodes;`）可以让每处使用都清晰易读。
2. **无检查索引：**对 `a[id]` 进行索引需要**零次**越界检查。只要你先执行 `assert(weights.len >= a.len)`，对*并行*数组 `weights[id]` 的索引同样是零检查的！
3. **区间携带索引类型：**当 `lo` 和 `hi` 的类型为 `u32 upto edges` 时，`for k in lo..hi` 会自动赋予循环计数器 `k` 以 **`u32 in edges`** 类型！
4. **永不缩容：**被索引类型引用的任何数组在程序的任何地方都不可缩容，从而保证每一个先前已验证的索引永远保持有效。
5. **读取时视为普通整数：**索引类型在算术运算中会隐式转换为其底层 `uN` 类型（运算结果为普通整数），且没有默认值；`from_bytes` 会在加载镜像时自动验证每一个索引值！

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

仔细观察 `neighbor_weight_sum`：**`edges[k]`、`nodes[dst]` 以及 `weights[dst]` 全部在零越界检查的情况下完成编译**，并由 `checkfree fn` 永久验证！

---

## 18.3 使用无检查函数（`checkfree fn`）强制执行契约

将函数标记为 `checkfree fn` 并不是禁用越界检查——而是要求编译器**证明**：在 BCE 之后，该函数内部（以及它调用的每一个辅助函数或尾随代码块内部）没有任何残留的越界检查！

如果日后有人重构代码时不小心删掉了前置条件 `assert`，编译会立刻报错，而不是让性能在不知不觉中退化：

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

> **编译器提示：**在 `checkfree fn` 内部，标准库中的各项数学/向量/矩阵/四元数辅助函数（`std` 位运算、`vec`、除 `row` 之外的 `mat`、`quat`、切片上的 `min`/`max`）本身都已经做到零检查，因此你可以直接在 `checkfree` 核函数中调用它们，无需任何额外标注！

---

## 18.4 实战案例：可证明零检查的 CSR 图 BFS

压缩稀疏行（CSR，Compressed Sparse Row）是静态图在缓存友好性方面的黄金标准表示法：每个顶点在单个扁平 `edges` 数组中存储一个半开区间 `[lo..hi)`。

将 CSR 与 `NodeId = u32 in g_nodes` 和 `EdgeBound = u32 upto g_edges` 结合起来，看看 **`checkfree fn bfs`** 如何在**零越界检查**的前提下遍历整张图——全程使用 4 字节顶点 ID，且没有任何堆分配：

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

`bfs` 内部的任何地方都没有残留哪怕一次越界检查——而且与 C 或 `unsafe` Rust 不同，编译器**保证**绝不可能发生越界内存访问，即便有人向 `build_graph` 传入了畸形的边索引（它们会在 `e[1] as NodeId` 处被干脆利落地捕获）。

---

## 练习

1. **测试 `checkfree` 强制检查：**在 18.4 节中，注释掉 `bfs` 内部的 `assert(dist.len >= g_nodes.len);` 并点击 **▶ Run**。查看 Goose 精确标出的、重新出现越界检查的代码行！
2. **测试“永不缩容”不变量：**在 18.2 节中，尝试在 `main()` 末尾调用 `nodes.pop();`。观察 Goose 如何拒绝这次缩容，因为 `NodeId`（`u32 in nodes`）依赖于 `nodes` 永不缩容。
3. **编写无检查的一维移动平均函数：**编写 `checkfree fn smooth3(src: const f32[:], dst: f32[:])`，断言 `src.len >= 3 && dst.len == src.len`，设置端点 `dst[0]` 和 `dst[src.len - 1]`，并在 `i in 1..src.len - 1` 上对 `src[i - 1]`、`src[i]`、`src[i + 1]` 求平均值，确保不残留任何越界检查。

---

## 本章要点

- Goose 会为安全起见对每一次索引进行检查，并运行一趟静态 **BCE** 遍；可使用 `goose check --stats` 和 `goose check --bce-lines` 查看结果。
- **索引类型（`uN in a`）**与**上界类型（`uN upto a`）**在构造时*一次性*验证间接索引，使后续所有的查找操作（`a[id]`、在 `assert(b.len >= a.len)` 之后的 `b[id]`，以及 `for k in lo..hi`）完全免于越界检查。
- **无检查函数（`checkfree fn`）**将“零残留越界检查”作为编译期契约强制贯彻于函数本身、其辅助函数以及代码块之中——让你在零 `unsafe` 的前提下获得 C 级别的内层循环速度。

在**第 19 章：编写高性能 Goose 代码**中，我们将纵览完整的性能图景：数据布局选择、`inline fn`、`simd fn`（AVX2/AVX-512 多版本编译）、阅读生成的 C 代码，以及来自 Goose 16 项基准测试套件的真实经验教训！
