# 第 3 章：值的栖身之所：大小类别与数据栈

你在本书其余章节中将遇到的每一条规则——为什么 Goose 拥有七种数组类型、为什么引用在 `push` 之后依然有效、为什么非固定大小的值绝不会静默拷贝、以及为什么递归函数不能持有可增长的局部变量——都源于同一块基石：**值在内存中的栖身之所**。

在大多数编程语言中，内存管理是被隐藏在 `new`、`Box` 或垃圾回收器背后的实现细节。而在 Goose 中，内存放置简单、静态且完全透明。一旦你在脑海中勾勒出编译器如何将变量映射到**数据栈**上，整门语言的设计便会豁然贯通。

学完本章后，你将能够：

- 将任意 Goose 类型归入其对应的大小类别（**固定大小**、**变长**、**可调整大小**或**根**），并判断它是否属于**扁平类型**。
- 阐述 Goose 的三大内存区域，以及使指针递增分配（bump allocation）安全可靠的栈不变量。
- 使用 `goose check --stacks`（或在线演练场中的 **Stacks** 按钮）查看数据栈分配结果，并预测不同作用域何时会共享或分离数据栈。
- 借助显式的 `copy(...)` 与**原地构造保证**（目标处直接构造），彻底消除隐蔽的 O(n) 拷贝开销。

---

## 3.1 四种大小类别（与扁平类型）

Goose 中的每一个类型都严格属于一种**大小类别**，它向编译器指明了该类型值的字节大小究竟是在程序运行前就已确定、在构造时固定下来，还是允许在之后发生变化：

| 大小类别 | 字节大小 | 示例 | 典型存储位置 |
|---|---|---|---|
| **固定大小（fixed）** | 编译期常量 | 标量（`i64`、`f32`、`bool`）、定长数组 `T[k]`、静态定容数组 `T[..k]`、仅含固定大小字段的结构体、固定模式枚举 `E`、引用 `T&`、切片 `T[:]` | 原生调用栈 / CPU 寄存器（或内联于容器中） |
| **变长（variable）** | 构造时确定，此后永久冻结 | 变长数组 `T[]`、`T[varint]`、运行时定容数组 `T[..]`、含 `varint` 或变长字段的结构体、变长模式枚举 `E..` | 数据栈（或内联于变长 / 可调整大小容器中） |
| **可调整大小（resizable）** | 构造后可增长（或收缩） | 只增数组 `T[>..]`、增缩数组 `T[>..<]`、以可调整大小字段结尾的结构体 | 独占数据栈的栈顶 |
| **根（root）** | 无单一连续内存布局 | 将独立存储的字段（如多个可调整大小数组）打包在一起的根结构体（`root struct`） | 由局部或全局声明拥有；每个字段使用各自的存储空间 |

对于任何普通复合类型（结构体或数组），其大小类别等于各组成部分大小类别中的**最大者**（`fixed < variable < resizable`）：

- `struct Point { x: f64, y: f64 }` 是**固定大小**的（16 字节）。
- `struct Tag { id: i32, name: u8[..16] }` 依然是**固定大小**的（容量为 16 的空间直接内联存储！）。
- `struct Record { id: i32, name: u8[] }` 是**变长**的（大小取决于构造时 `name` 的长度）。
- `struct Buffer { header: i32, data: u8[>..] }` 是**可调整大小**的（其尾部可以持续增长）。
- 根结构体（`root struct`）始终保持**根**类别，即便它的所有字段碰巧都是固定大小的。

### 扁平类型

与大小类别正交的是一个非此即彼的属性：如果一个类型在任何嵌套深度上都**不包含引用、切片或相对引用**，且本身不是根结构体（`root struct`），那么它就是一个**扁平类型**。

扁平类型的值是完全自包含的原始字节——即使它是变长或可调整大小的（例如 `struct Row { y: i32, pixels: u8[] }`）。只要字节需要离开当前内存域——例如跨线程发送消息（第 14 章），或在不产生悬空地址的前提下干净利落地拷贝原始数据——扁平性就至关重要。

---

## 3.2 三大内存区域与栈不变量

运行中的 Goose 程序**没有通用堆**。其完整的内存模型仅由三个区域组成：

```text
+-------------------------------------------------------------------+
| 1. Static Read-Only Data                                          |
|    String literals ("hello"), compile-time `const` globals        |
|    Shared across all threads; never freed until process exit      |
+-------------------------------------------------------------------+

+-------------------------------------------------------------------+
| 2. Native Call Stack (& CPU Registers)                            |
|    Fixed-size locals, parameters, return addresses, and the       |
|    length headers of outermost resizable locals                   |
+-------------------------------------------------------------------+

+-------------------------------------------------------------------+
| 3. N Data Stacks  (N computed statically by the compiler)         |
|                                                                   |
|   Data Stack 0:  [  2 GB reserved virtual space  ][1 MB guard]    |
|                  [==== live values ====>          ]               |
|                                         ^ top0 (bump pointer)     |
|                                                                   |
|   Data Stack 1:  [  2 GB reserved virtual space  ][1 MB guard]    |
|                  [== active T[>..] ==>            ]               |
|                                       ^ top1 (bump pointer)       |
|                                                                   |
|   Data Stack 2:  [  2 GB reserved virtual space  ][1 MB guard]    |
|                  [^ top2 (reused by sequential scopes)            |
+-------------------------------------------------------------------+
```

每个数据栈都是一段大块的虚拟地址空间保留区（默认 2 GB，可通过 `--stack-reserve 16G` 调整），相邻数据栈之间由 1 MB 未映射的保护间隔（guard gap）隔开。在 64 位操作系统上，保留虚拟地址空间几乎不消耗任何物理内存——操作系统只有在程序实际写入时才会按需延迟提交物理页。

> **内存模型：** 运行时没有任何分配器元数据、没有空闲链表（free list），也没有每对象分配头。在运行时，一个数据栈名副其实地只是一个递增指针（bump pointer）。

### 栈不变量

一门语言如何在不使用 `malloc` 且不移动内存的情况下提供任意大小的动态数组？答案是在**编译期静态强制执行**四条规则：

1. **严格嵌套的生命周期。** 单个数据栈上的值按作用域顺序压入，并在所属作用域退出时*一次性全部弹出*。释放它们只需一条指令：将 `top` 重置回之前保存的标记位置。
2. **每个数据栈上最多只能有一个存活的可调整大小值，且必须始终位于栈顶。** 在 `T[>..]` 或 `T[>..<]` 的整个生命周期内，同一数据栈上不得在其上方分配任何其他对象。它的长度头保存在所属调用栈帧中（位于数据栈之外），因此向数组追加元素只需直接在 `top` 处写入并将 `top` 向前推进——无需容量检查、无需重新分配、无需内存搬移。
3. **同时存活的可调整大小值分配到不同的数据栈；先后顺序出现的值则复用同一数据栈。** 如果三个可增长数组在同一时刻存活，编译器会为它们分配三个独立的数据栈。一旦某个代码块结束并释放了它占用的数据栈，紧随其后的下一个代码块即可立即复用该数据栈。
4. **变长值构造完成后即可被“压在下方”。** 变长类别的值（如 `u8[]`、`Order`）在构造期间于数据栈上增长；构造完成的那一刻，其大小即永久冻结，后续的值便可以安全地存放在同一数据栈的上方。

你永远无需在 Goose 代码中为数据栈命名——语言中根本不存在任何数据栈语法。编译器会根据程序的调用图自动计算出所需的总数 `N`。

### 观察编译器如何分配数据栈

点击下方示例中的 **Stacks**（或运行 `goose check --stacks`），即可查看编译器精确的数据栈分配结果：

```goose
fn main() {
    // Two simultaneously live grow-only arrays -> 2 distinct data stacks.
    var a: i64[>..] = [];
    var b: i64[>..] = [];
    for i in 5 {
        a.push(i);
        b.push(i * 10);
    }

    // Two sequential blocks -> they reuse the SAME third data stack!
    {
        var tmp1: u8[>..] = [];
        format(tmp1, "first=", a.len);
        print(tmp1);
    }
    {
        var tmp2: u8[>..] = [];
        format(tmp2, "second=", b.len);
        print(tmp2);
    }
}
```

```
first=5
second=5
```

留意 `goose check --stacks` 的输出报告：`tmp1` 与 `tmp2` 的生命周期在时间上互不重叠，因此即便你继续添加第三个、第四个甚至第一百个顺序执行的代码块，也**不会**增加任何额外的数据栈！

### 向操作系统归还峰值内存

当作用域退出并回退数据栈的递增指针时，这段内存会立刻可供你的程序再次使用；但出于性能考量，操作系统通常会继续保持这些已提交页面的映射状态（避免下一次循环迭代再次触发缺页中断开销）。如果你的程序刚完成了一项异常庞大的瞬时任务，希望将闲置的物理页归还给操作系统，可以调用 `os` 模块中的 `trim_memory()`：

```goose
import os;

fn main() {
    {
        var huge: u8[>..] = [];
        for i in 200_000 {
            huge.push((i % 256) as! u8);
        }
        print("peak=", huge.len, " bytes");
    }
    // `huge` is already freed; now ask the OS to discard its unused pages.
    trim_memory();
    print("trimmed unused pages");
}
```

```
peak=200000 bytes
trimmed unused pages
```

`trim_memory()` 会完好保留所有存活的值与全部虚拟地址空间保留区，后续的数据增长依然无缝运作。

---

## 3.3 永不搬移：O(1) 释放与稳定引用

栈不变量直接赋予了程序两项极为实用的超能力。

### 作用域退出即 O(1) 释放

声明在循环体内的可增长缓冲区在每次迭代时都会全新启程，并在右花括号 `}` 处瞬间彻底消失——无论它有多大、嵌套有多深：

```goose
fn main() {
    for round in 3 {
        var scratch: u8[][>..] = [];
        for i in 500 {
            scratch.push(str("item-", round, "-", i));
        }
        print("round ", round, ": ", scratch.len, " inline strings");
    } // One store resets the stack top — zero per-string teardown!
}
```

```
round 0: 500 inline strings
round 1: 500 inline strings
round 2: 500 inline strings
```

### 引用在无限增长中依然有效

由于可增长数组占据着一块专属虚拟保留区的顶端，向其中压入元素**永远不会重新分配或搬移**已有元素。`push` 会返回一个指向新添加元素的直接引用，只要数组本身仍然存活，该引用便始终有效：

```goose
struct Entry { id: i32, score: f32 }

fn main() {
    var entries: Entry[>..] = [];
    let first .= entries.push(Entry { id: 1, score: 10.0 });
    let second .= entries.push(Entry { id: 2, score: 20.0 });

    for i: i32 in 3..50_001 {
        entries.push(Entry { id: i, score: 0.0 });
    }

    // Both references still point straight at elements 0 and 1:
    first.score += 5.5;
    second.score *= 2.0;
    print("count=", entries.len, " first=", entries[0], " second=", entries[1]);
}
```

```
count=50000 first=Entry { 1, 15.5 } second=Entry { 2, 40.0 }
```

> **致 C++ / Rust 开发者：** 在 C++ 中，除非提前调用 `reserve` 预留已知上限，否则跨越 `push_back` 持有 `&entries[0]` 一旦触发 vector 重新分配就会导致未定义行为。在安全 Rust 中，跨越 `entries.push(...)` 持有 `&mut entries[0]` 会被借用检查器直接拒绝，迫使你退而使用整数下标。而在 Goose 中，跨越增长依然稳定的强类型引用是默认特性——第 12 章与第 13 章将直接基于这一保证构建完整的树与图结构。

---

## 3.4 拷贝处处可见

在 C++ 中，对 `std::vector` 写下 `auto b = a;` 会在暗中触发一次 O(n) 的堆分配与深拷贝。而在 Goose 中，**昂贵的拷贝绝不会静默发生**：

- **固定大小的值会隐式拷贝**：在赋值和按值传参时，标量、固定大小结构体、`T[k]`、`T[..k]`、引用和切片都会像 C 语言一样隐式按值拷贝。
- **非固定大小的值绝不隐式拷贝**：当一个已存在的变长或可调整大小变量、字段或元素被绑定到未标注类型的 `let`/`var`、传递给引用 / 切片 / 无类型标注形参，或在 `for` 循环中迭代时，它会**按引用绑定**——开销仅为 O(1)。
- 每当你确实需要一份非固定大小值的独立副本时，都必须显式写下 `copy(x)`。

```goose
struct Point { x: i32, y: i32 }

fn main() {
    // Fixed-size struct: assignment copies silently.
    var p = Point { 1, 2 };
    var q = p;
    q.x = 99;

    // Resizable array: `let alias = a` binds by reference (NO copy!).
    var a: i64[>..] = [10, 20, 30];
    let alias = a;
    alias.push(40);

    // Explicit O(n) copy: `b` gets its own data stack and storage.
    var b = copy(a);
    b.push(999);

    print("p=", p, " q=", q);
    print("a=", a, " b=", b);
}
```

```
p=Point { 1, 2 } q=Point { 99, 2 }
a=[10, 20, 30, 40] b=[10, 20, 30, 40, 999]
```

如果目标位置*明确要求*一个非固定大小类型的自有值（例如按值接收非固定大小参数的形参，或显式标注了类型的局部变量 `var b: i64[>..] = ...`），却直接传入一个已有变量而不使用 `copy(...)`，编译器会直接报告**编译错误**：

<!-- compile-fail -->
```goose
fn main() {
    var a: i64[>..] = [1, 2, 3];
    var b: i64[>..] = a;             // error: non-fixed values never copy implicitly; use copy(a)
    print(b);
}
```

这条规则极其清爽：只要你在 Goose 源代码里没有看到 `copy(...)`，就绝对没有任何非固定大小的值在背后被偷偷拷贝。

---

## 3.5 原地构造保证

既然非固定大小的值绝不会隐式拷贝，那函数又是如何在不产生拷贝的情况下按值返回一个可增长数组 `i64[>..]` 或一个新字符串 `u8[>..]` 的呢？

答案在于 Goose 的**原地构造保证**（目标处直接构造）：

> **内存模型：** 每一个新构造的非固定大小值——无论是字面量、`str(...)` 调用、`copy(...)`，还是函数的返回值——无论经过多少层函数调用和分支，都会**直接在其最终目标位置构造**。绝不会产生中间临时对象再执行拷贝。

仔细看下面这三行代码背后发生的过程：

```text
1. let nums = make_squares(5);
   Caller allocates the stack destination for `nums` and passes it into `make_squares`.
   `make_squares` pushes elements straight into `nums`. Zero copies on return!

2. words.push(str("word", i));
   `words.push(...)` opens the new element slot right at `words`'s bump pointer and passes
   that destination to `str(...)`. `str` writes the length and bytes straight into `words`!

3. orders.push(Order { id: 1, customer: "alice", items: parse_items(...) });
   `orders.push` writes `id` and `customer` directly at `orders`'s top, then hands the
   exact next byte address to `parse_items(...)` to stream `Item`s straight into `orders`!
```

当函数内部构建单个局部变量（例如 `var out: i64[>..] = [];`），且每一条 `return` 语句都返回这同一个局部变量时，从它声明的那一行起，`out` *本身就是*调用方的目标存储位置（具名返回值优化，NRVO）：

```goose
fn make_squares(n: i64) -> i64[>..] {
    var out: i64[>..] = [];          // built directly in the caller's destination!
    for i in n {
        out.push(i * i);
    }
    return out;
}

fn format_label(prefix: const u8[:], id: i64) -> u8[>..] {
    str(prefix, "#", id)             // built directly in whatever receives the result
}

fn main() {
    let squares = make_squares(6);
    var labels: u8[][>..] = [];
    for s, i in squares {
        labels.push(format_label("sq", i));
    }
    print("squares=", squares);
    print("labels=", labels);
}
```

```
squares=[0, 1, 4, 9, 16, 25]
labels=["sq#0", "sq#1", "sq#2", "sq#3", "sq#4", "sq#5"]
```

这一保证彻底改变了你在 Goose 中设计 API 的方式：按值返回新构造的 `T[>..]` **与传入输出参数（out-parameter）一样快**，因此只有当你希望在多次调用间持续向同一个已有缓冲区*追加*内容时（例如 `format(buf, ...)`），才需要使用输出参数（`out: u8[>..]&`）。

> **注意事项（构造进行中）：** 因为 `xs.push(expr)` 会直接在 `xs` 的数据栈顶构造新元素，所以当该元素尚处于半构建状态时，`expr` 内部（及其调用的任何函数）都不得同时让 `xs` 增长。编译器会在静态检查阶段拒绝 `xs.push(grow_and_return(xs))`——如果确实需要先修改 `xs`，请先将结果计算到局部变量中。

---

## 3.6 实战示例：滑动窗口传感器处理器

让我们把第 3 章的所有概念串联起来，编写一条贴近实际场景的数据处理流水线：

- `generate_samples` 直接在 `main` 的目标变量中原地构造 `i32[>..]`（原地返回）。
- `summarize_windows` 使用一个**循环作用域内的临时缓冲区**处理定长窗口，在每个窗口结束时以 O(1) 开销瞬间释放，同时将 `WindowSummary` 结构体流式写入其返回的 `WindowSummary[>..]` 中。
- `main` 还会在追加一条额外基线记录前获取首条汇总记录的**稳定引用**，并通过显式 `copy(...)` 保存一份快照。

```goose
struct WindowSummary {
    window_id: i32,
    min_val: i32,
    max_val: i32,
    avg_val: f64,
}

fn generate_samples(count: i64) -> i32[>..] {
    var out: i32[>..] = [];          // built in place at caller's destination
    for i in count {
        // Deterministic sawtooth wave in [10, 49]
        let v = 10 + ((i * 7 + 3) % 40) as i32;
        out.push(v);
    }
    return out;
}

fn summarize_windows(samples: i32[:], win_size: i64) -> WindowSummary[>..] {
    var results: WindowSummary[>..] = [];
    var pos = 0;
    var win_id: i32 = 0;

    while pos < samples.len {
        let end = if pos + win_size < samples.len { pos + win_size } else { samples.len };

        // Scoped scratch buffer: lives only for this window, freed in O(1) at `}`.
        var scratch: i32[>..] = [];
        for i in pos..end {
            scratch.push(samples[i]);
        }

        var lo = scratch[0];
        var hi = scratch[0];
        var sum = 0;
        for v in scratch {
            if v < lo { lo = v; }
            if v > hi { hi = v; }
            sum += v;
        }

        results.push(WindowSummary {
            window_id: win_id,
            min_val: lo,
            max_val: hi,
            avg_val: sum as f64 / scratch.len,
        });

        pos = end;
        win_id++;
    }
    return results;
}

fn main() {
    let raw = generate_samples(12);
    var summaries = summarize_windows(raw, 4);

    // Explicit copy before we append an extra record to `summaries`.
    let snapshot = copy(summaries);

    // Reference into `summaries` survives subsequent pushes!
    let first .= summaries[0];
    summaries.push(WindowSummary { window_id: 99, min_val: 0, max_val: 0, avg_val: 0.0 });
    first.max_val += 1;

    print("raw=", raw);
    print("snapshot len=", snapshot.len, " first.max=", snapshot[0].max_val);
    print("live     len=", summaries.len, " first.max=", summaries[0].max_val);
    for s in summaries {
        print("  win ", s.window_id, ": min=", s.min_val, " max=", s.max_val, " avg=", s.avg_val);
    }
}
```

```
raw=[13, 20, 27, 34, 41, 48, 15, 22, 29, 36, 43, 10]
snapshot len=3 first.max=34
live     len=4 first.max=35
  win 0: min=13 max=35 avg=23.5
  win 1: min=15 max=48 avg=31.5
  win 2: min=10 max=43 avg=29.5
  win 99: min=0 max=0 avg=0.0
```

在在线演练场中点击这个实战示例的 **Stacks** 按钮吧！留意一下：尽管 `generate_samples`、`summarize_windows`、`scratch`、`snapshot` 和 `summaries` 都在管理动态数组，整条跨函数处理流水线总共需要的数据栈数量却少得惊人。

---

## 练习

1. **体验数据栈复用：** 在在线演练场编辑器中打开第 3.2 节的示例，将 `tmp2` 移到 `tmp1` 所在的 `{ ... }` 代码块*内部*，使两者同时存活，然后点击 **Stacks**。对比修改前后报告的数据栈数量。
2. **触发“构造进行中”检查：** 编写一个小函数 `fn push_and_zero(xs: i64[>..]&) -> i64 { xs.push(0); return 0; }`，并在 `main` 中尝试调用 `xs.push(push_and_zero(xs));`。阅读 Goose 输出的确切错误信息，然后用两行代码将其安全改写。
3. **验证零拷贝绑定与显式 `copy` 的区别：** 修改第 3.6 节的实战示例，将 `let snapshot = copy(summaries);` 改为 `let snapshot = summaries;`。运行程序，观察 `snapshot.len` 与 `snapshot[0].max_val` 如何变化，并解释原因。

---

## 本章要点

- 每个类型都属于四种大小类别之一（**固定大小**、**变长**、**可调整大小**、**根**）；不含引用、切片或相对引用（且非 `root struct`）的类型是**扁平类型**。
- Goose 用 **N 个静态分配的数据栈**取代了堆：每个数据栈栈顶最多只有一个存活的可调整大小值、通过指针递增实现增长、永不搬移内存，并在作用域退出时以单条存储指令完成 O(1) 释放。
- 固定大小的值会静默拷贝；非固定大小的值**绝不隐式拷贝**——绑定它们会创建引用，而 O(n) 的复制始终需要显式调用 `copy(x)`。
- **原地构造保证**（目标处直接构造）会跨越任意调用深度，直接在最终目标位置构建新创建的非固定大小值，让按值返回可增长数组实现零拷贝。

在牢固建立起数据栈的心智模型之后，**第 4 章**将全面探索**数组家族与字符串**，带你学会为任何问题挑选最恰当的容器布局——从定长数组 `T[k]`、内联静态定容数组 `T[..k]`，一直到 `T[varint]` 与增缩数组 `T[>..<]`。
