# 第 8 章：结构体、根结构体与非嵌套生命周期

到目前为止，我们创建的每一个动态容器都遵循着清晰的作用域层级：一个变量拥有一个数组，而它的生命周期整齐地嵌套在某个代码块或函数之内。现实中的程序很快就会在两个方向上突破这种简单模式：

1. **成组状态：** 应用程序中的某个子系统（如解析器上下文、仿真引擎、工作区）往往需要*多个*一同生死的自动增长数组。
2. **非嵌套生命周期：** 缓存条目、已打开的编辑器文档、游戏实体以及被编辑的树节点，都是以任意顺序创建和删除的——并非严格遵循后进先出（LIFO）。

Goose 用 **`root struct`**（根结构体）解决第一个问题，并用直接构建在 `root struct` 之上的两个标准库池类型解决第二个问题：**`slot_pool<T>`**（槽位池，可复用的独立存储槽位）和 **`slice_pool<T>`**（切片池，可复用的连续区间）。

学完本章后，你将能够：

- 设计带有字段默认值、`let`/`const` 字段、显式 `pad`、泛型以及可调整大小尾部的紧凑（packed）结构体。
- 使用 `root struct` 将多个独立增长的容器归拢到同一个所有者生命周期之下。
- 使用 `slot_pool<T>` 和 `slice_pool<T>` 安全地管理任意对象的生命周期。
- 解释 Goose 的**类型安全复用**保证，并在需要严格检测失效句柄时在其上叠加代数计数器（generation counter）。

---

## 8.1 普通结构体：布局、默认值、限定符与尾部字段

普通 `struct` 按声明顺序对字段进行**紧凑（packed）**布局——Goose 不会在字段之间插入隐藏的对齐填充字节（一个 `i8[3]` 后跟一个 `i32` 总共占 7 字节）。当你需要显式间隔或 C/GPU 对齐时，可插入 `pad n`（`n` 个未使用的字节）或裸 `pad`（将*下一个*字段对齐到其标量宽度）。`==` 比较会自动忽略填充字节。

普通结构体的核心特性：

- **默认值与 `..`：** 字段默认值（`id: i64 = next_id()`）会在字面量省略该字段时每次重新求值。以 `..` 结尾的字面量会用声明的默认值或类型零值填充其余字段。泛型代码可以通过 `default<T>()` 构造默认值。
- **字段限定符：** `let f: T` 禁止在构造完成后对该字段重新赋值；`const f: T` 则同时锁定该字段及其内部内容。
- **泛型：** `struct Pair<T> { a: T, b: T }` 会按具体类型实参进行单态化（在无歧义时可由字段值自动推导）。
- **可调整大小尾部：** 普通结构体的**最后一个**字段可以是可调整大小数组（`T[>..]` 或 `T[>..<]`），从而使该结构体本身变为可调整大小类型——它依然存储在一块连续布局中，但最多只能拥有*一个*可调整大小尾部。

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

请记住，`==` 会按成员逐一比较普通结构体（跳过 `pad` 字节），而对引用或切片字段则按标识（地址和长度）进行比较，从而保证循环图绝不会陷入死循环。

---

## 8.2 `root struct`：归拢多个独立容器

普通结构体紧凑地存放在单个连续块中，因此最多只能有一个可调整大小尾部。如果一个工作区、解析器或数据结构需要共同拥有**两个或三个**独立增长的数组，该怎么办？

答案是声明一个 **`root struct`**（根结构体）：

- `root struct` 解除了单一连续块的限制：**每个字段都获得自己独立的存储空间（如果是可调整大小类型，则拥有自己的数据栈）**，且字段顺序不受限制。
- 所有字段共享所有者变量的生命周期——当所有者离开作用域时，每个组成部分的数据栈都会在 O(1) 时间内自动回退释放。
- 函数以**引用方式**接收根结构体（`Workspace<T>&`），并且收缩其中一个字段绝不会使指向另一个字段的引用失效！

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

### `root struct` 的放置权衡

由于 `root struct` 拥有多个独立的数据栈，而非单一扁平字节序列，它属于**根（root）**大小类别（第 3 章），并具有明确的放置规则：

- **仅限所有者声明：** 拥有所有权的 `root struct` 必须是**局部变量或全局变量**（`var ws = Workspace<i64> { .. };`）。
- **不可复制、按值移动或嵌套：** 不能复制根结构体、不能按值传参或返回、不能放入数组中，也不能嵌入另一个结构体中（目前不支持嵌套拥有所有权的根结构体——请将同级所有者并列声明，再传递它们的引用）。
- **引用属于普通固定大小值：** `Workspace<T>&` 和 `Workspace<T>?` 可以传入函数、作为返回值、存入普通结构体或数组，也可以配合 UFCS 使用（`ws.record(...)`）。
- **没有紧凑字节映像：** 应对符合条件的字段单独进行序列化（例如 `to_bytes(ws.items)`）。

试图复制拥有所有权的 `root struct` 或将其放入数组会在编译期被拦截：

<!-- compile-fail -->
```goose
root struct Bag { xs: i64[>..] = [] }

fn main() {
    var bags: Bag[>..] = [];         // error: root structs cannot be array elements
    print(bags.len);
}
```

> **内存模型：** 点击上方 `Workspace<T>` 示例中的 **Stacks**！你会看到编译器自动为 `ws.items` 和 `ws.scratch` 分配了各自独立的数据栈，并在 `main` 结束时将两者一并释放。

---

## 8.3 `slot_pool<T>`：可复用的独立存储槽位

每当对象需要以任意非嵌套顺序创建和销毁时——例如游戏世界中的实体、可变图中的节点、缓存记录——都可以从标准库导入 **`slot_pool`**（槽位池）。

`slot_pool<T>` 是一个 `root struct`，它将一个只增底层数组 `items: T[>..]`（适用于任意固定大小类型 `T`）与一个记录回收槽位索引的内部空闲链表配对在一起：

| 操作 | 作用 |
|---|---|
| `pool.alloc_index(v) -> i64` | 将 `v` 存入一个回收的空闲槽位（或追加新槽位）并返回其索引 |
| `pool.alloc_ref(v) -> T&` | 同上，但返回指向该槽位的直接引用 |
| `pool.alloc_with() { expr } -> T&` | 在选定槽位中**就地**构造值（用于包含 `self` / 自相对引用的类型） |
| `pool.free(i)` | 将槽位 `i` 标记为可复用（带越界检查；绝不会收缩 `items`） |
| `pool.free_ref(r)` | 通过 `pool.items.index_of(r)` 将 `r` 转为索引并释放该槽位 |
| `pool.recycle_all()` | 将所有槽位标记为可复用，同时保留已初始化的底层存储 |

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

注意 `pool.items.len` 是 `3`——它是迄今为止追加过的槽位数量的**高水位线（high-water mark）**，*包括*已释放的槽位。因为 `items` 永不收缩，指向 `pool.items` 的引用在 `pool` 的整个生命周期内都保持有效。

---

## 8.4 `slice_pool<T>`：可复用的连续区间

有时单个固定大小的存储槽位还不够：文件系统中的每个目录都需要一个变长的子项链接列表，文本编辑器中的每个已打开标签页都需要一个可增长、且能独立于其他标签页关闭的字节缓冲区。

此时可以导入 **`slice_pool`**（切片池）。`slice_pool<T>` 是一个 `root struct`，内部管理一个只增数组 `items: T[>..]` 外加一棵支持合并的空闲区间树，用来向外分配**连续切片 `T[:]`**：

| 操作 | 作用 |
|---|---|
| `pool.alloc_slice(n) -> T[:]` | 按首次适应策略分配长度为 `n` 的空闲区间（或扩展 `items`），并将每个元素重置为默认值 |
| `pool.alloc_copy(src) -> T[:]` | 分配一段直接由 `src` 初始化的区间（非常适合在 `slice_pool<u8>` 中存放字符串） |
| `pool.realloc_slice(s, n) -> T[:]` | 将 `s` 调整为长度 `n`：在可能的情况下**就地**收缩或增长，否则安全迁移 |
| `pool.free_slice(s)` | 将 `s` 归还给空闲区间树，并合并相邻的空闲区间 |
| `pool.recycle_all()` | 将整个高水位范围重置为单个空闲区间 |
| `pool.trim() -> bool` | *（仅限 `slice_pool<u8>`）* 丢弃空闲区间内部完整的未使用操作系统内存页 |

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

## 8.5 类型安全复用与内存安全（以及旧版 `reusable`）

先停下来仔细思考：如果你的代码持有指向某个 `slot_pool` 槽位的引用 `r`，随后释放了该槽位、向其中分配了新值，之后又误读了 `r`，会发生什么？

- **绝不会发生内存破坏：** 底层存储永远不会被取消映射或重新解释为其他类型；在池的整个生命周期内，`pool.items` 中的每个槽位都始终是一个存活且正确初始化的 `T`。读取 `r` 只会读到*另一个有效的 `T`*，绝不会读到垃圾字节、出现野指针或发生越界崩溃。
- **但它依然是逻辑错误**——与在 Rust `Vec` 或数据库表中持有失效整数索引属于完全相同的一类 bug。

每当你需要**在运行时严格检测失效句柄**（例如游戏中的实体引用或编辑器中的文档句柄）时，可以为每个槽位索引配对一个单调递增的**代数计数器（generation counter）**（`u32`）：

1. 在每条池化记录中存储 `gen: u32`。
2. 向调用方发放轻量级值句柄 `struct Handle { idx: i64, gen: u32 }`。
3. 释放时，先递增 `slot.gen`（并将 `slot.alive = false`），再调用 `pool.free(idx)` 归还槽位。
4. 查找时，验证 `slot.alive && slot.gen == handle.gen`——如果句柄已失效，则返回 `null`！

> **迁移提示（旧版 `reusable`）：** 较早的 Goose 代码和文档中可能会出现语言级别的 `reusable var pool: T[>..] = [];` 和 `reusable[] var runs: T[>..] = [];`。这些语言关键字现已**弃用**，由标准库中的 `slot_pool<T>` 和 `slice_pool<T>` 取代（它们都是使用普通 Goose 代码在 `root struct` 之上实现的）。在新代码中请始终优先使用 `slot_pool` 和 `slice_pool`。

---

## 8.6 实战示例：多文档缓冲区管理器

下面我们来构建一个迷你编辑器缓冲区管理器，支持以任意顺序打开、编辑、扩容和关闭文档——将 `slot_pool<Doc>`、`slice_pool<u8>` 与**带代数的句柄**结合起来，使指向已关闭文档的失效句柄能够被干净地捕获：

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

仔细对比 `h2` 与 `h4`：打开 `"plan.md"` 时复用了槽位 `1`（因此 `docs.items.len` 保持在 `3`），同时也复用了 `text` 中已释放的字节空间，但递增后的代数（`gen=2`）让失效句柄 `h2`（`gen=1`）在查找和修改操作中都会安全失败。

---

## 练习

1. **检查 `root struct` 的数据栈：** 点击第 8.2 节中 `Workspace<T>` 示例上的 **Stacks**，接着为 `Workspace<T>` 添加第三个可调整大小字段 `log: u8[>..] = []`，再次点击 **Stacks**，确认每个字段都获得了自己独立的数据栈。
2. **测试 `slice_pool<u8>.trim()`：** 在第 8.6 节的示例中，打开一个 100,000 字节的大文档，将其关闭，然后调用 `text.trim()`。检查它的布尔返回值，并确认存活的文档（`h1`、`h3`、`h4`）完全不受影响。
3. **添加 `rename_doc`：** 为第 8.6 节的缓冲区管理器扩展 `fn rename_doc(docs: slot_pool<Doc>&, h: DocHandle, new_name: const u8[:]) -> bool`，使用 `d.name.clear(); format(d.name, new_name);` 更新名称，并验证对已关闭句柄执行重命名会返回 `false`。

---

## 本章要点

- 普通结构体紧凑排列字段，不插入隐藏填充；需要外部布局对齐时使用 `pad`，需要不可变字段时使用 `let`/`const`，利用字段默认值配合 `default<T>()` 简化初始化，当只需一个可增长字段时可使用单个可调整大小尾部。
- **`root struct`**（根结构体）可将多个独立增长的容器归拢到同一个局部或全局所有者之下，所有字段共享所有者离开作用域时的生命周期。
- 对于**非嵌套生命周期**，请使用标准库提供的 **`slot_pool<T>`**（槽位池，可复用的固定大小存储槽位）和 **`slice_pool<T>`**（切片池，可复用且支持合并的连续区间），以替代已弃用的 `reusable` / `reusable[]`。
- 池的复用具备**类型安全**保证（绝不会导致内存破坏）；当需要在运行时拒绝失效句柄时，加上代数计数器即可。

至此，**第二部分：引用、类型与安全内存**全部结束！在**第三部分：抽象、控制流与数据结构**中，我们将从**第 9 章：函数、泛型与编译期展开**出发，深入探索 Goose 零开销的单态化泛型、尾随代码块、跨帧返回（non-local `return`）以及 `const for` / `const if` 编译期反射。
