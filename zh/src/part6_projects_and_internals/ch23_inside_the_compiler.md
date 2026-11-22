# 第 23 章：深入底层——Goose 编译器的工作原理

贯穿全书，你已经熟悉了 Goose 一系列独具特色的规则：无需生命周期标注即可在 `push` 后保持有效的引用、不能跨越调用保留可增长局部变量的递归函数、无检查函数（`checkfree fn`）契约、跨帧返回（`return … from`）栈展开，以及可直接充当网络传输格式的扁平紧凑布局。这些规则无一是凭空设定的——它们全都直接源自一套紧凑、透明的全程序编译器架构。

在这最后一章中，我们将揭开 Goose 编译器（`src/` 与 `docs/implementation.md`）的底层面纱。读完本章后，你将建立起清晰的心智模型，全面掌握以下内容：

- 全程序编译流水线的全部七个阶段，从词法语法解析直至 TinyCC JIT 与原生 C 构建。
- 单态化、根溯源与静态数据栈分配如何在没有借用检查器或垃圾回收器的前提下，于编译期证明内存安全。
- 越界检查消除（BCE）、视图提升以及 `checkfree` 验证如何与 C 代码生成协同工作。
- 编译器为紧凑结构体、数组、代数数据类型（ADT）、相对引用以及跨帧返回（`return … from`）生成的精确 C99 内存表示。
- 当前编译器有意保持保守的边界所在，以及 Goose 路线图上正在推进的设计提案。

---

## 1. 全程序编译流水线

Goose 以一个根 `.goose` 文件及其传递性的 `import` 依赖图为起点，通过单个快速的 C++ 可执行文件完成整个程序的编译。每个编译阶段都会在内存中共享的同一份 `Ast` 上依次执行：

```text
  .goose sources (+ imports)
            │
            ▼
  ┌───────────────────┐
  │ 1. Parse          │  lexer.h, parser.h -> Ast (nodes, namespaces, globals)
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 2. Resolve        │  resolve.h -> resolves type names, expands type aliases
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 3. Typecheck      │  typecheck*.h -> monomorphizes FnSpecs, infers roots &
  │    & Specialize   │  lifetimes, checks shrinks/recursion, compiles embed_shader
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 4. Optimize       │  optimize*.h -> inlining, base-case inlining, tail-rec
  │                   │  elimination (TRE), loop shaping (stripk/sumred/countdown)
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 5. BCE            │  bce.h -> proves index/slice/pop bounds (nobc), hoists
  │                   │  loop views (hoistrefs/hoistfields), verifies checkfree
  └─────────┬─────────┘
            ▼
  ┌───────────────────┐
  │ 6. C99 Codegen    │  codegen*.h -> static stack counts, packed C types,
  │                   │  SIMD versions, cached stack tops + src/runtime/
  └────┬─────────┬────┘
       │         │
       ▼         ▼
  ┌─────────┐  ┌────────────────────────────────────────────┐
  │ 7a. JIT │  │ 7b. Native Build (goose build / -o out.c)  │
  │ libtcc  │  │ C compiler (clang / gcc / MSVC) + cached   │
  │ in-proc │  │ runtime object (GS_RUNTIME_VERSION hashed) │
  └─────────┘  └────────────────────────────────────────────┘
```

Goose 之所以编译极快，源于四项关键架构决策：

1. **无析构函数的 Arena 风格 AST：** 正如 Goose 程序本身一样，编译器将每一个 AST 节点、类型表达式、`VarDef` 与特化结果都存放在归 `Ast` 所有的只增表中。函数体会按特化实例（`FnSpec`）克隆，每个克隆节点都回指其源节点（`Node::origin`），以支撑报错诊断和 LSP 查询。
2. **专用的 64 MB 编译线程：** 深度嵌套的程序和递归特化路径运行在一条拥有 64 MB 虚拟栈（仅按实际深度按需提交物理内存）的专用编译线程上，而 JIT 执行则会回到主线程，从而确保 macOS/SDL3 等 GUI 框架可以安全创建窗口。
3. **拆分运行时目标文件以加速原生构建：** `goose build` 会将运行时中偏重于平台交互的那一半（`runtime_impl.h`、`runtime_threads.h`、`runtime_os.h`——涵盖 `windows.h` / POSIX 头文件、打印输出、操作系统调用与内存区域管理）**一次性**编译为带有 `GS_RUNTIME_VERSION` 内容哈希标记的缓存目标文件。你的程序生成的 C 代码仅需包含轻量的内联头文件（`runtime.h` + `runtime_ext.h`），从而大幅缩短原生 C 编译耗时。而在 TinyCC JIT 或 `--standalone` 模式下，两部分则会被合并输出为一个自包含的 C 编译单元。
4. **编译器*本身就是*语言服务器（`goose lsp`）：** `goose lsp` 会针对编辑器中打开的缓冲区运行完全相同的“解析 → 名称消解 → 类型检查 → 优化 → BCE”流水线，并针对尚未被任何地方调用的泛型函数额外执行*样本检查（exemplar checking）*，使悬停提示、代码补全和跳转定义在库代码内部同样顺畅可用。

---

## 2. 根溯源、根与静态数据栈分配

Goose 如何在既不要求你编写 `'a` 生命周期标注、也不禁止别名（aliasing）的情况下，保证任何引用永远不会悬垂——即便跨越 `push`、辅助函数调用与嵌套作用域？

### 单态化 + 根溯源

当 Goose 对 `fn f(x: Node&, a: Node[>..]&)` 的调用进行类型检查时，它并不会拿着抽象的生命周期参数孤立地检查 `f` 一次；相反，它会同时依据**具体的实参类型**和每个引用/切片实参的**根溯源**对 `f` 进行特化（生成对应的 `FnSpec`）：

- **精确根：** 编译器确切知道引用指向的是*哪一块具体存储*（例如局部变量 `nodes`、全局变量 `pool` 或参数类别 `P0`）。精确根解锁了 `index_of`、自相对引用链接写入（`a.next .= b`）以及栈顶指针寄存器缓存等能力。
- **非精确根：** 当两条分支合并了来自不同数组的引用（`if cond { a[0] } else { b[0] }`），或者引用是从普通结构体字段中读取出来的，Goose 会退回到**存活期上界（outlives bound）**（即“该引用指向作用域深度 $\le d$ 的存储”）。它仍然可以安全读写，只是无法宣称自己归属于某一个特定数组。
- **存储记录：** 每当函数将引用或切片存入结构体字段或数组元素时，Goose 都会将该根溯源记录到目标容器上。之后当调用方尝试缩减数组（`pop`、`clear`、`resize`）或将引用返回到其所属作用域之外时，检查器就会查阅存储记录，拒绝任何可能令存活视图悬垂的操作。

> **为何不需要独占性（`&mut`）规则？** 因为只增数组 `T[>..]` 底层的虚拟内存绝不会在 `push` 时发生移动，所以指向同一个只增数组的两个别名引用互相之间永远不会令对方失效！因此，Goose 可以毫无顾忌地允许任意别名，只限制那些真正会使地址失效的操作（缩容、离开作用域以及尚未构造完成的值）。

### 静态数据栈分配（`BoundStacks`）

每一个只增数组（`T[>..]`）、增缩数组（`T[>..<]`）或变长局部数组都需要一个底层数据栈。Goose 并不在运行时动态分配这些栈，而是在编译期为每个函数静态分配数据栈：

1. 在单个函数内部，**按顺序执行且生命周期互不重叠的代码块会复用同一个栈索引** `gs_sp + k`。只有同时存活的可调整大小数组才需要彼此独立的栈索引。
2. 当函数 `A` 调用函数 `B` 时，`B` 的数据栈会从 `gs_sp + live_in_A` 开始，紧挨在 `A` 当前正持有的数据栈上方。
3. 由于在存在任何存活数据栈的情况下禁止调用进入**递归环**（`NoStackAcrossCycleCall`），因此数据栈深度的调用图必然是无环的！
4. 由此，`BoundStacks` 可以在编译期精确算出 `main` 以及每一个 `thread_fn` 工作线程在最坏情况下同时活跃的数据栈数量。一旦超过 `GS_MAX_STACKS`（默认为 1024），就会直接报出**编译期错误**——绝不会演变成运行时的意外故障。

在线程启动时，运行时会精确保留对应数量的虚拟地址区域（默认每个区域 `GS_STACK_RESERVE` = 2 GB，且每段之后紧跟一段未映射的 `GS_STACK_GAP` 保护页区域）。物理内存由操作系统在写入页面时按需惰性提交，同时 `hardware_threads()` 会自动限制自身上限，使主程序连同全部工作线程稳妥地控制在 `GS_STACK_BUDGET`（32 TB 虚拟地址空间）之内。调用 `trim_memory()`（来自 `os` 模块）则会请求操作系统（Linux 上为 `MADV_DONTNEED`，macOS 上为 `MAP_FIXED` 替换，Windows 上为 `MEM_DECOMMIT`）回收每个栈顶之上的未使用内存页，同时保持所有存活指针依然有效。

---

## 3. 深入越界检查消除（BCE）、视图提升与 `checkfree`

在完成优化（内联、常量折叠、尾递归消除、针对 `recursive fn` 的基例内联以及循环形态标注）之后，Goose 会在每一个存活的特化实例上运行**越界检查消除（BCE）**遍（`bce.h`）。

### 差分约束域

BCE 追踪整型变量、数组长度与常量之间的线性不等式关系（`0 <= i`、`i < a.len`、`a.len == b.len`、`w <= nodes.len - 1`），并将这些事实顺着分支、`assert`/`guard` 条件、`for`/`while` 循环以及非递归调用点向前传播：

- **只增数组 `T[>..]` 的单调性：** 向只增数组执行 `push` 只会*增加*其长度，因此所有已有的 `i < a.len` 证明都会在 `a.push(...)` 之后自动保持有效！只有缩容操作（`pop`、`clear`、`resize`）才会使长度上界失效。
- **按类型精确失效（Typed kills）：** 通过引用对 `Node[>..]` 进行扩容或缩容，不会破坏关于另一类型 `i64[>..]` 的长度事实。
- **索引类型与上界类型：** `u32 in a` 类型的值在每次读取时都会立刻注入公理 `0 <= v <= a.len - 1`（而 `u32 upto a` 会注入 `0 <= v <= a.len`）——即便它是从数组或结构体字段中加载出来的！
- **验证无检查函数（`checkfree fn`）：** 在 BCE 将每一个已证明安全的访问标记为 `nobc` 之后，编译器驱动会扫描每个 `checkfree` 函数内部（以及从它可达的所有内联代码块与传递调用的辅助函数）的每一行源码。只要有任何一处检查未能被标记消除，就会立刻产生编译错误，并*精确说明为何无法证明其安全性*。

### BCE 传递给代码生成的额外事实

除了移除 `GS_IDX` 边界检查之外，BCE 还会为循环附加五项标注，从而在 C 代码层面解锁显著的性能提升：
1. **`ForLoop::fixedlen`：** 证明循环体绝不会改变被迭代数组的长度，使代码生成器只需加载一次基址指针和长度，而不必在每次迭代时重新读取。
2. **`hoistrefs` / `hoistfields`：** 将引用参数和结构体字段数组的基址指针与长度提升到 C 局部标量变量中，避免字节写入操作导致指针被反复重载。
3. **`Binary::specidx`：** 证明在直线型循环中，`&&` 或 `||` 的右侧无需依赖左侧守卫即可安全求值，从而允许编译器使用无分支的 `&` / `|` 指令进行自动向量化。
4. **`Binary::nonneg`：** 证明有符号除法 `/` 或取模 `%` 的操作数非负且除数为正，使代码生成器可以发出无符号除法指令以及 libdivide 魔数乘法。
5. **`ForLoop::lenbound`：** 为小型定容数组（`T[..8]`）上的循环标注迭代次数上界，让 clang 能够将其完全展开。

---

## 4. Goose 值的精确 C99 内存表示

Goose 最大的优势之一在于其内存布局**可以精确预测到每一个字节**。生成的每一份 C 文件均面向 64 位小端序主机，并对聚合类型统一应用 `#pragma pack(1)`，因此除非你显式写明 `pad` 或 `pad n`，否则绝不会存在任何隐式对齐填充：

| Goose 语言构造 | 生成的 C99 表示 | 关键运行时细节 |
|---|---|---|
| 紧凑结构体 `struct S { a: u8, b: u32 }` | `#pragma pack(1)` C `struct { uint8_t a; uint32_t b; }` | 精确字节大小为 5 字节；无尾部填充。空结构体占 1 字节（`gs_empty`）。 |
| 显式 `pad` / `pad n` | 插入的填充字节（`uint8_t _pad[k]`） | 单独的 `pad` 会将下一个标量字段对齐到其自然宽度（引用/切片为 8 字节）；在变长布局中会被忽略。 |
| 定长数组 `T[k]` | `struct { T e[k]; }` | 支持按值拷贝；针对常量下标的越界检查在编译期或 BCE 阶段消除。 |
| 静态定容数组 `T[..k]` | `struct { uintN_t len; T e[k]; }` | `len` 为 `u8`（$k \le 255$）、`u16`（$k \le 65535$）、`u32` 或 `u64`。 |
| 运行时定容数组 `T[..]` | 数据栈上的 `[u32 cap][u32 len][cap × T]` | 容量在分配时固定；赋值操作会保留接收方的原有容量。 |
| 变长数组 `T[]` / `T[varint]` | 内联长度（`u32` 或 ULEB128）+ 连续元素 | 直接存放在外层变长结构体或栈缓冲区内部。 |
| 可调整大小数组 `T[>..]` / `T[>..<]` | 头部（`data*`、`len`）+ 专用数据栈 | 胖引用 `T[>..]&` 的表示为 `gs_rref { hdr*, stk* }`。 |
| 固定模式 ADT `E` | `struct { uint8_t tag; union { ... } u; }` | `u8` 标签（$\le 256$ 个变体）或 `u16`；大小为标签加最大变体载荷之和。 |
| 变长模式 ADT `E..` | `[u8/u16 tag][当前活跃变体载荷字节]` | 仅占当前活跃变体的精确大小；无 union 对齐浪费。 |
| 自相对引用 `T&<u32>` / `T&<u32>?` | 裸 `uint32_t`（或 `uint8_t` / `uint16_t`） | 存储 `(target_addr - field_addr)`；可选引用保留 `0` 表示 `null`，并拒绝零偏移的非空写入。 |
| 池相对引用 `T&<u32 in pool>` | 裸 `uint32_t` | 存储 `(target_addr - pool_base)`；可自由按值拷贝！ |
| 切片 `T[:]` | `struct { T* data; int64_t len; }` | 占两个机器字；空切片即便 `data == NULL` 也能安全使用 `gs_memcpy`/`gs_memeq`。 |
| 跨帧返回 `return E from f` | 隐藏的线程局部返回标志 + 目标写入地址 | 正常返回路径零开销；传播路径上的每个调用帧仅需一次加载外加一条预测不跳转的分支。 |

即便是细微的运行时细节，也都为跨平台确定性与极致速度进行了精心设计：
- **`gs_memeq` 与 `gs_memcpy`** 针对不超过 256 字节的缓冲区直接内联执行（采用重叠字加载），无需发起 libc 调用，从而消除了短字符串键比较时的 `memcmp` 分发开销。
- **浮点数格式化（`gs_fmt_f64` / `gs_fmt_f32`）** 使用纯整数算术实现了 Burger–Dybvig 最短精确十进制数字算法，而不是调用 libc 的 `printf`，从而确保 `0.1f` 在 Linux、macOS、Windows 以及 TinyCC 上一律输出完全相同的 `"0.1"`。

### 使用 `bytes_of` 实测精确布局

你完全不必仅凭信任接受上表——下面让我们直接在 Goose 中使用 `bytes_of` 逐条验证每一项布局规则：

```goose
struct Empty {}

struct Packed {
    a: u8,
    b: u32,
}

struct Aligned {
    a: u8,
    pad,      // aligns b (u32) to a 4-byte offset (+3 bytes)
    b: u32,
}

enum Msg {
    Ping,
    Move { x: f64, y: f64, z: f64 },
}

fn main() {
    let empty_arr:   Empty[1]   = [Empty {}];
    let packed_arr:  Packed[1]  = [Packed { a: 1, b: 2 }];
    let aligned_arr: Aligned[1] = [Aligned { a: 1, b: 2 }];

    // Static limited arrays choose 1-byte len for cap <= 255, 2-byte len for cap <= 65535.
    let small_lim: u8[..10][1]  = ["hi"];
    let med_lim:   u8[..300][1] = ["hi"];

    // Fixed-mode vs variable-mode ADT size for a single Ping variant.
    let fixed_ping: Msg[1]     = [Msg.Ping];
    var var_ping:   Msg..[>..] = [];
    var_ping.push(Msg.Ping);

    print("Empty[1]:     ", bytes_of(empty_arr).len,   " byte  (C gs_empty)");
    print("Packed[1]:    ", bytes_of(packed_arr).len,  " bytes (1 + 4, no padding)");
    print("Aligned[1]:   ", bytes_of(aligned_arr).len, " bytes (1 + 3 pad + 4)");
    print("u8[..10][1]:  ", bytes_of(small_lim).len,   " bytes (1B len + 10B buf)");
    print("u8[..300][1]: ", bytes_of(med_lim).len,     " bytes (2B len + 300B buf)");
    print("Msg[1] Ping:  ", bytes_of(fixed_ping).len,  " bytes (1B tag + 24B max union)");
    print("Msg.. Ping:   ", bytes_of(var_ping).len,    " byte  (1B tag only!)");
}
```

```text
Empty[1]:     1 byte  (C gs_empty)
Packed[1]:    5 bytes (1 + 4, no padding)
Aligned[1]:   8 bytes (1 + 3 pad + 4)
u8[..10][1]:  11 bytes (1B len + 10B buf)
u8[..300][1]: 302 bytes (2B len + 300B buf)
Msg[1] Ping:  25 bytes (1B tag + 24B max union)
Msg.. Ping:   1 byte  (1B tag only!)
```

每一个字节都有据可查——没有隐藏的对象头，没有神秘的对齐填充，也没有运行时描述符。

---

## 5. 保守边界与前沿演进方向

由于 Goose 将健全性（soundness）、简洁性与高速编译置于首位，当前实现在若干有明确文档记载的地方有意保持了保守策略（详见 `docs/implementation.md` §10）：

- **越界检查消除（BCE）的作用范围：** BCE 不会追踪存储在数组内部的任意数值（请改用索引类型 `u32 in a`），不会追踪普通 `u64` 变量作为基准值（请使用 `i64` 或索引类型），也不支持在 `while` 退出条件中表达析取（逻辑或）约束。
- **跨帧返回（`return … from`）中的引用：** 以具名函数为目标的 `return … from` 目前仅允许返回指向全局或静态数据的引用（而从尾随代码块中进行的非局部返回则能够清晰地映射调用方的根）。
- **跨普通参数与队列的相对引用：** 在递归环之外，普通的 `T&` 参数会获得各自独立的根类别，除非通过 `in pool` 建立关联。此外，包含相对引用的值目前尚不能直接跨越 `qput`/`qget` 传递——需先通过 `to_bytes` / `from_bytes` 序列化。
- **根结构体（`root struct`）嵌套与池迁移：** `root struct` 的所有者目前必须是局部变量或全局声明（暂不支持嵌套拥有其他 `root struct` 字段），而旧版的 `reusable` / `reusable[]` 内建类型仍处于弃用状态，推荐统一改用 `slot_pool<T>`（槽位池）与 `slice_pool<T>`（切片池）。

### `docs/design/` 中的未来蓝图

Goose 正围绕其核心理念——**无需生命周期标注、无需垃圾回收器的高速、扁平、内存安全系统编程**——稳步演进。仓库的 `docs/design/` 目录中正在推进的设计提案包括：

- **结构化并行（`par for`）：** 轻量级子线程直接借用父线程数据栈的只读视图，无需通过队列拷贝整个数组。
- **协程与生成器（Coroutines / generators）：** 将可恢复状态机干净地融入 Goose 的静态数据栈模型。
- **无析构函数的移动语义：** 在保留 $O(1)$ 栈重置释放机制的同时，消除转移非固定大小值所有权时不必要的显式 `copy(...)` 操作。
- **源码级调试（`goose dap`）：** 与 `goose lsp` 并行的调试适配器协议（DAP）服务器，使编辑器能够原生单步调试并查看 Goose 变量、切片与紧凑结构体。
- **WebAssembly 目标平台：** 将 Goose 程序编译为紧凑、零 GC 的 Wasm 二进制文件，服务于浏览器与沙箱插件场景。

---

## 练习

1. **查看真实的特化表：** 对第 20 章的 JSON 解析器（或第 4 节的布局验证程序）运行 `goose check --specs --stacks`。观察 `main` 需要多少个静态数据栈，以及哪些泛型辅助函数被单态化了。
2. **阅读 `return … from` 生成的 C 代码：** 编写一个极简的三函数调用链（`a` → `b` → `c`），在 `c` 中执行 `return -1 from a`，用 `goose -o /tmp/retfrom.c chain.goose` 导出 C 代码，找出在 `b` 中传播跨帧返回的那个线程局部标志与单条分支语句。
3. **实测 `GS_STACK_STATS`：** 使用 `goose -o /tmp/prog.c prog.goose`（加上 `--standalone` 选项）将任意程序导出为独立 C 文件，再用 `cc -O2 -DGS_STACK_STATS=1 /tmp/prog.c -o /tmp/prog -lm` 进行原生编译，随后运行 `/tmp/prog`，观察运行时在退出时向 stderr 打印的精确数据栈与虚拟地址区域用量。
4. **在自己的代码上探索 `--bce-lines`：** 对你在本书前文编写的排序或图算法运行 `goose check --stats --bce-lines`，找出所有仍被保留的越界检查，并尝试看看能否用一条 `assert` 或一个索引类型将其彻底消除。

---

## 本章要点

- **全程序单态化**是整个 Goose 设计的引擎：类型检查、根溯源、生命周期验证、优化、越界检查消除（BCE）与静态数据栈计数全都在具体特化实例上完成，零运行时开销。
- **精确根与非精确根**结合**存储记录**，为 Goose 带来了无需任何标注的生命周期安全性，同时不必禁止别名。
- **静态数据栈计数（`BoundStacks`）** 将内存管理转化为编译期常量数量的惰性提交虚拟内存区域，并具备 $O(1)$ 的离开作用域重置开销。
- **基于 `#pragma pack(1)` 的紧凑 C99 布局**意味着 Goose 结构体、定容数组、变长模式枚举以及相对引用的每一个字节都透明、可预测，且可通过 `bytes_of` 与 `goose -o` 直接检视。
- 理解编译器的保守边界，并熟练运用 `--stats`、`--bce-lines`、`--stacks`、`--specs` 与 `-o out.c` 透视其工作过程，能将 Goose 从一个黑盒变成一把趁手的精密系统级工具。

---

至此，你已经从第一个 Goose 程序一路走来，完整穿越了内存栈、相对引用、受验证序列化、无共享并发、无检查 SIMD 性能工程、三大综合实战项目，直至编译器本身的内部实现。今后在构建你自己的 Goose 项目时，随时可以翻阅**附录 A–D**，速查语法卡片、标准库参考、C++/Rust/Go 迁移指南以及编译诊断信息速解手册。
