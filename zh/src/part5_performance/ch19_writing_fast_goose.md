# 第 19 章：编写高性能 Goose 代码

在第 18 章中，你已经看到 Goose 如何通过越界检查消除（BCE）、索引类型以及无检查函数（`checkfree fn`），在不牺牲内存安全的前提下移除热点循环中的安全开销。消除越界检查固然是高性能的重要支柱，但它往往并不是最关键的瓶颈。在现代硬件上，远在一次整数比较成为瓶颈之前，CPU 核心就常常因为等待内存中的缓存行、分配器元数据或分支预测失败而陷入停顿。

本章是一份面向 Goose 性能工程的实战指南。其中的每一条建议都立足于编译器生成 C 代码的实际机制，并结合了 Goose 自身的 16 项基准测试套件（与 C++ 和 Rust 对比）的实测数据。

学完本章后，你将能够：

- 选择合适的数据布局——内联的变长模式 ADT、紧凑的固定大小结构体、定宽整数以及恰当的链接表示——从而最小化每次操作触及的字节数。
- 合理组织循环结构，使 Goose 能够外提数组视图、将栈顶缓存在寄存器中、用魔数乘法消除除法运算，并让 C 后端顺利完成自动向量化。
- 运用 `inline fn`、`simd fn` 以及（来自 `math` 模块的）`mul_add`，在保持浮点计算结果可复现的同时，解锁 AVX2/AVX-512 多版本分发与融合乘加（FMA）。
- 顺应编译器保证的目标处直接构造与具名返回值优化（NRVO），避免隐蔽的拷贝开销。
- 审查生成的 C 代码，理解各后端之间的差异（TinyCC JIT、clang、GCC、MSVC），并以诚实、公正的方式开展基准测试——既清楚 Goose 具备决定性优势的场景，也了解它与对手持平乃至落后的领域。

---

## 1. 数据布局工程：缩减触及的字节数

Goose 的大部分性能优势都源于一条统领全局的原则：**在连续内存中、以零分配器开销，针对每单位工作触及更少的字节**。当工作集缩小到原来的 1/2 甚至 1/4 时，那些在富含指针的语言中不得不溢出到 L3 缓存或主存的数据结构，在 Goose 中便能轻松驻留在 L1 或 L2 缓存内。

### 变长模式 ADT 与固定模式枚举的对比

在 C++ 和 Rust 中，一个枚举（`std::variant` 或 `enum`）所占的空间总是等于其**最大变体**的大小加上对齐填充。如果你的 AST 节点或日志记录中有 80% 是 4 字节整数，只有 5% 是 48 字节的结构体，那么每一个元素都不得不为这 48 字节买单。

而在 Goose 中，**变长模式枚举**（`Record..`）会在同一块连续缓冲区内，按照每个元素当前活跃变体的精确大小来存储数据——甚至包括内联的变长字符串：

```text
Fixed-mode array (Record[>..]):
┌────────────────────────────┬────────────────────────────┬────────────────────────────┐
│ tag │ Small (4B) │ 40B pad │ tag │ Small (4B) │ 40B pad │ tag │ Big (44B)            │
└────────────────────────────┴────────────────────────────┴────────────────────────────┘

Variable-mode array (Record..[>..]):
┌──────────────┬──────────────┬────────────────────────────┐
│ tag│Small(4B)│ tag│Small(4B)│ tag│Big(44B)               │
└──────────────┴──────────────┴────────────────────────────┘
```

让我们直接在 Goose 中使用 `bytes_of` 测量二者确切的内存差异：

```goose
enum Event {
    Tick { seq: u32 },
    Sample { seq: u32, x: f32, y: f32 },
    Note { seq: u32, text: u8[..48] },
}

enum EventVar {
    Tick { seq: u32 },
    Sample { seq: u32, x: f32, y: f32 },
    Note { seq: u32, text: u8[] },
}

fn main() {
    var fixed: Event[>..] = [];
    var packed: EventVar..[>..] = [];

    for i in 1000 {
        let s = i as u32;
        if i % 20 == 0 {
            fixed.push(Event.Note { seq: s, text: "sensor recalibrated" });
            packed.push(EventVar.Note { seq: s, text: "sensor recalibrated" });
        } else if i % 4 == 0 {
            fixed.push(Event.Sample { seq: s, x: 1.5, y: 2.5 });
            packed.push(EventVar.Sample { seq: s, x: 1.5, y: 2.5 });
        } else {
            fixed.push(Event.Tick { seq: s });
            packed.push(EventVar.Tick { seq: s });
        }
    }

    let fixed_bytes = bytes_of(fixed).len;
    let packed_bytes = bytes_of(packed).len;
    let ratio_x10 = (fixed_bytes * 10) / packed_bytes;

    print("fixed bytes:  ", fixed_bytes);
    print("packed bytes: ", packed_bytes);
    print("compression:  ", ratio_x10 / 10, ".", ratio_x10 % 10, "x");
}
```

```text
fixed bytes:  54000
packed bytes: 7750
compression:  6.9x
```

固定模式数组占用了 **54,000 字节**，因为每一个 `Tick` 都必须为 48 字节的 `Note` 缓冲区（加上标签、`seq` 和长度头）预留空间。而变长模式数组以完整内联字符串、零堆分配、零指针追踪的方式保存了完全相同的 1,000 条事件，仅占 **7,750 字节**，空间缩减了 **6.9 倍**。

> **权衡取舍：** 变长模式数组是顺序存储的（如果不借助辅助索引或相对链接，就无法在 $O(1)$ 时间内直接索引 `packed[500]`），且元素一旦压入，其变体标签便不可再更改。当你需要随机访问或原地修改变体类型时，应选用固定模式数组；当你只需构建一次、随后顺序扫描或通过相对链接遍历时，则应选用变长模式数组。

### 定宽整数与 `varint` 的对比

Goose 的 `varint` 类型能用 1 字节编码小整数、最多用 10 字节编码大整数，这对于序列化以及取值范围宽广且不可预测的冷字段非常理想。但必须注意：**每次读取 `varint` 字段都会执行一次 ULEB128 解码循环**。（数组的长度前缀采用了单字节快速路径，而 `varint` 值字段则始终走解码循环。）

在 Goose 自身的 `calc` 基准测试中，用 `varint` 代替 `u8` 存储小数字，导致热点求值器变慢了 **11%**。只有当空间是首要考量或数值范围确实无界时才使用 `varint`；只要提前知道取值范围，就应优先选用定宽的 `u8`、`u16`、`u32` 或 `i32`。

### 在自相对链接、池相对链接与索引类型之间做选择

第 12 章介绍了自相对引用（`T&<u32>`）和池相对引用（`T&<u32 in pool>`），第 18 章介绍了索引类型（`u32 in pool`）。从性能角度看，它们**并不**可以随意互换：

| 链接形式 | 大小 | 一次构建 / 多次遍历 | 高频重连（如 LRU） | 可拷贝 / 可序列化 |
|---|---|---|---|---|
| `T&<u32>?`（可选自相对引用） | 4 B | 快（无需基址寄存器，位置无关） | 比索引约慢 1.5 倍（指针追踪关键路径上增加了加减运算） | 不可拷贝；可直接 `to_bytes`/`from_bytes` 序列化 |
| `T&<u32>`（非可选自相对引用） | 4 B | 更快（无空值判别；在 MSVC 下快约 1.10 倍） | 相同的偏移运算，但少了空值分支 | 不可拷贝；可直接序列化 |
| `T&<u32 in pool>`（池相对引用） | 4 B | 快（clang 能很好地调度 `base + off`） | 在全局池场景下快于自相对引用 | 可自由拷贝；不可直接 `to_bytes` 序列化 |
| `u32 in pool`（带类型的索引） | 4 B | 快，且支持 `checkfree` 验证 | 修改与重连速度最快 | 可自由拷贝且可序列化 |

由上表可以得出两条实用法则：

1. **一次构建、多次遍历的树与 AST**（解析器、场景树、霍夫曼树、二进制缓存）：优先选择**自相对引用**（`T&<u32>` / `T&<u32>?`）——如果你希望避免每跳一次都做空值判断，可以使用以 `self` 作为哨兵初始化的非可选链接。
2. **高频重连的可变数据结构**（LRU 缓存、侵入式工作列表、动态图）：优先选择 **`u32 in pool` 索引类型**或 **`in pool` 池相对链接**，以免重连操作把地址减法塞进关键依赖链中。

---

## 2. 编写编译器能够充分优化的循环

即使所有的越界检查都已消除，C 编译器也只有在能够把指针、长度和栈顶保存在 CPU 寄存器中，并且能够证明各次迭代互不干扰的情况下，才能生成极速机器码。在把 C 代码交给后端之前，Goose 会先行实施若干有针对性的优化。

### 循环不变量视图（模板卷积场景提速 4 倍）

以引用方式传入的数组（`src: u8[>..]&`）将其数据指针和长度存放在头部指针所指向的内存中。在 C 语言语义里，任意一次字节写入（`dst[i] = ...`）理论上都可能与该头部产生别名冲突，从而迫使 C 编译器在每次迭代中都重新加载 `src` 的基址指针和长度。

Goose 的优化器通过**视图外提（view hoisting）**解决了这一问题：只要循环体内只对引用参数的*元素*进行读写（没有 `push`、`pop`、`resize`、`clear`、整数组赋值、重绑定或任何可能改变其大小的函数调用），Goose 就会在循环开始前一次性把基址指针和长度加载到局部 C 变量中。在 Goose 的 `blur` 基准测试中，仅视图外提一项就在 clang 下带来了 **4 倍提速**。

> **实用法则：** 保持元素处理循环中不包含扩容操作。如果需要填充输出缓冲区，要么在循环前预先调用 `resize`（使循环体只做元素写入），要么将结果构建到另一个独立数组中，将其扩容行为与只读源视图隔离开来。

### 栈顶与长度缓存

当函数在紧凑循环中不断向数组追加元素（`out.push(...)`）时，每次 `push` 都向内存写回栈顶和元素计数会阻塞后续读取并妨碍优化。只要函数内不存在可能指向同一数据栈的第二个名字（别名），Goose 就会自动将栈顶和长度缓存在局部 C 寄存器中。

通过引用参数执行压栈的辅助函数（如 `fn emit(a: u8[>..]&, b: u32[>..]&)`），在满足以下条件时可获得完整的寄存器缓存：

1. 函数体内用到的每一个胖引用都是形参（或其不可变的 `let` 别名）——而不是从结构体字段或数组元素中读出的胖引用。
2. 在每一个调用点，所有引用参数都拥有互不相同的精确根。
3. 函数不返回非固定大小的值，不捕获可调整大小的类型，也不触及与所指类型相同的全局变量。

当你编写热点生产者（如序列化器、词法分析器或网格构建器）时，请**将互不相同的输出数组直接作为形参传入**，而不要把胖引用打包进临时辅助结构体里。

### 自动循环改写与魔数除数

在 Goose 中，你通常无需手动展开循环或手写位运算技巧——编译器能识别多种惯用循环形态，并将其改写为 clang 能够激进优化的形式：

- **循环不变量除数外提：** 在循环体内，除以或取模于未修改变量（`x / d`、`x % m`）的整数运算，会在循环之前自动替换为 libdivide 魔数乘法与移位。对于 `u32`（32 位及更窄）操作数，Goose 会生成可**自动向量化**的 32 位魔数乘数——相比标量除法可提速 4 倍至 7 倍。
- **通道分块（`i % K`）：** 用 `i % K`（$K \in [2, 16]$）索引小型定长数组的循环（如 `for x, i in data { lanes[i % 8] ^= x; }`）会被分块拆解为长度为 $K$ 的常量索引块，使 `lanes` 全程驻留在寄存器中。
- **保序浮点归约（`s += term(x)`）：** 将浮点数累加到 `s` 的简单循环会先在可向量化的内层块中一次计算 8 个项，再严格按照原始顺序依次累加到 `s` 中——既获得了计算 `term(x)` 的 SIMD 吞吐量，又保证了逐位一致的 IEEE 舍入结果。
- **有界小数组展开：** 遍历小型定容数组（例如类型为 `T[..8]` 的 `cell.neighbors`）时，会自动生成带有上界迭代次数的循环，clang 会将其完全展开。
- **倒计数循环改写：** `while i > 0 { i--; ... }` 会被改写为向上计数的 `for` 循环，便于 C 后端更好地做指令调度。
- **直线型 `&&` / `||`：** 在直线型内层循环（无函数调用、`break`、`continue` 或嵌套循环）中，纯计算且已证明不越界的 `a[j] == x && b[j] == y` 会被编译为无短路分支的按位 `&`，使后端能够将其向量化。

### 以每周期 16 字节的速度扫描字节

带有提前退出条件的逐字节 `while` 循环（`if buf[i] == '\n' { break; }`）大约只能以每周期 1 字节的速度运行，因为没有任何 C 编译器能够对包含数据相关提前退出的循环进行向量化。

Goose 提供了两种快得多的惯用写法：

1. **配合 `std` 中的 `ByteSet` 使用 `find_any` / `find_pair`：** 正如第 17 章所介绍的，`find_any` 在 x86-64 上利用 SSE2/SSSE3、在 AArch64 上利用 NEON，每次扫描 16 字节。记得在循环*之外*一次性构建好 `ByteSet`。
2. **按固定大小分块计数：** 在针对 32 字节切片的直线型 `for j in 32` 循环中统计匹配数（`if a[j] == x && b[j] == y { hits += 1; }`），可以触发 Goose 的无分支 `&&` 推测执行并顺利向量化；若 `hits > 0`，再回过头扫描这 32 字节以定位确切索引。

下面是一个可运行的程序，同时展示了这两种技术：

```goose
import std;

checkfree fn count_block_matches(a: const u8[:], b: const u8[:], x: u8, y: u8) -> i64 {
    assert(a.len == b.len);
    let n = a.len;
    var total = 0;
    var base = 0;

    // Straight-line 32-byte chunks: bounds-proven and branchless &&.
    while base + 32 <= n {
        let sa = a[base..base + 32];
        let sb = b[base..base + 32];
        var hits = 0;
        for j in 32 {
            if sa[j] == x && sb[j] == y {
                hits += 1;
            }
        }
        total += hits;
        base += 32;
    }

    while base < n {
        if a[base] == x && b[base] == y {
            total += 1;
        }
        base += 1;
    }
    return total;
}

fn count_delimiters(text: const u8[:]) -> i64 {
    // Prepare the SIMD ByteSet once outside the loop.
    let delims = byte_set(",;\n");
    var pos = 0;
    var count = 0;
    while pos < text.len {
        let off = find_any(text[pos..], delims);
        if off < 0 { break; }
        count += 1;
        pos += off + 1;
    }
    return count;
}

fn main() {
    let csv = "id,name,score;1,ada,99\n2,grace,100;3,linus,95\n";
    print("delimiters: ", count_delimiters(csv));

    var p1: u8[>..] = [];
    var p2: u8[>..] = [];
    for i in 80 {
        p1.push((i % 5) as u8);
        p2.push((i % 3) as u8);
    }
    // i % 5 == 2 and i % 3 == 1 => i % 15 == 7 -> 7, 22, 37, 52, 67 (5 hits)
    print("pair matches: ", count_block_matches(p1, p2, 2, 1));
}
```

```text
delimiters: 12
pair matches: 5
```

---

## 3. 函数级调优：`inline fn`、`simd fn` 与 `mul_add`

当数据布局和循环结构都已经足够干净之后，Goose 还提供了两个精准的函数修饰符来引导代码生成：`inline fn` 与 `simd fn`。

### `inline fn`：保证在每个调用点内联

在 `-O1` 和 `-O2` 优化级别下，Goose 会自动内联小型函数（`-O1` 下最多 8 个 AST 节点，`-O2` 下最多 16 个）以及单次使用的辅助函数。在函数前加上 `inline` 则会强制 Goose 在**每一个**调用点将其内联（`-O0` 除外，此时为便于调试会禁用内联）：

```goose
inline fn clamp_u8(v: i32) -> u8 {
    if v < 0 { return 0; }
    if v > 255 { return 255; }
    return v as u8;
}
```

修饰符规则：
- `inline` 不能与 `extern`、`export`、`simd`、`recursive` 或 `thread_fn` 组合使用。
- 它**可以**与 `checkfree` 组合：`checkfree inline fn ...`。
- 修饰符顺序是固定的：`export|extern` → `checkfree` → `simd` → `inline` → `recursive` → `fn|thread_fn`。

### `simd fn`：运行时分发的 AVX2 与 AVX-512

默认情况下，编译生成的 C 代码以平台基准指令集（x86-64 上为 SSE2，AArch64 上为 NEON）为目标，以确保二进制文件随处可运行。但在现代 x86-64 CPU 上，对于算术密集型的字节和浮点循环，这一基准配置白白浪费了 2 倍至 4 倍的吞吐能力。

使用 `simd fn` 标注热点循环函数后，在 x86-64 平台上用 clang 编译时，Goose 会为该函数生成**三个版本**：

1. **Level 2（`AVX-512`）：** AVX-512F、BW、CD、DQ、VL（`x86-64-v4`）
2. **Level 1（`AVX2`）：** AVX2、FMA、BMI1、BMI2、LZCNT、POPCNT
3. **Baseline（基准版本）：** 可移植的 SSE2 回退实现

首次调用时，一个轻量级的运行时探测逻辑（`cpuid` + `xgetbv`，结果以原子方式缓存）会选出 CPU 与操作系统共同支持的最高版本，后续调用只需付出一次极易预测的分支开销。在 GCC、MSVC、ARM64 和 TinyCC 下，`simd` 只会生成一个普通函数，不产生任何分发开销。

为什么 `simd` 需要显式标注，而不是对所有循环自动开启？因为 Goose 的基准实验表明：盲目对每个函数做多版本生成会让二进制体积膨胀至原来的三倍，平均收益却只有 1%–4%（落在误差范围内），而且部分循环在 AVX-512 下反而会*变慢*（例如重度依赖 gather 的查表操作或内存带宽受限循环）。请将 `simd fn` 精准地用在满足以下三个条件的**含循环计算核**上：

1. **可向量化的循环体：** 无提前退出（遇到非法数据时不要 `break`，改为累加位掩码标志）、零越界检查（`checkfree`），且循环内部没有栈顶同步操作。
2. **以算术运算为主而非查表：** 利用回绕减法进行的范围判断（如 `if c - 'a' < 26 { ... }`）可以向量化为 SIMD 比较与混合指令，而查表操作则会退化为昂贵的 gather 指令。在 `stdlib/base64.goose` 中，基于算术运算的 `simd fn` 编解码器在 AVX-512 下的速度比 SSE2 快了 **5.8 倍**。
3. **由 `simd fn` 持有循环本身：** 不要把 `simd` 标在被非 `simd` 循环调用的微小逐元素辅助函数上；应该把 `simd` 标在外层循环函数上，而把 `inline` 标在它调用的辅助函数上。

### 确定性浮点与显式 FMA（`mul_add`）

许多 C 编译器默认开启浮点收缩（`-ffp-contract=on`），在面向 AVX2/AVX-512 或 ARM64 编译时，会悄悄把 `a * b + c` 融合成单次舍入的 FMA 指令。这会破坏结果的可复现性：同一个程序在 AVX2 和 SSE2 上打印出的数字居然会不一样！

Goose 有意在整个生成的 C 编译单元中禁用隐式收缩（`#pragma clang fp contract(off)`），从而保证 `simd fn` 的 AVX2 与 AVX-512 版本和基准版本产生**逐位一致**的 IEEE-754 计算结果。

当你确实*需要* FMA 的高吞吐量——并且接受单次舍入带来的精度变化——只需导入 `math` 并调用 `mul_add(a, b, c)`：

- 在 `simd fn` 的 AVX2/AVX-512 版本中（以及在 ARM64 平台上），`mul_add` 会直接编译为硬件 FMA 指令。
- 在被 `simd fn` 调用的 `inline fn` 辅助函数中，Goose 会将该辅助函数分别内联进各个 SIMD 版本，因此其中的 `mul_add` 同样会被融合！
- 在没有硬件 FMA 的目标平台上（或在 MSVC 下，其 C 标准库 `fma()` 是很慢的库函数调用），它会干净地回退为 `a * b + c`。

> **结构体逐元素数学运算：** 对于完全由 2、4、8 或 16 个同宽浮点数构成的紧凑结构体或定长数组（总大小为 16、32 或 64 字节，例如 `struct Vec4 { x: f32, y: f32, z: f32, w: f32 }`），Goose 还会将其上的逐元素 `+`、`-`、`*`、`/` 和一元 `-` 直接降级为 GCC/clang 向量运算（`GS_VEC`），在不改变结构体布局的前提下为 FFT 蝶形运算带来 1.25 倍至 1.6 倍的提速。

### 编写可向量化的浮点循环：行序矩阵乘法

由于编译器不能对浮点加法做结合律重排，经典的 `i-j-k` 点积循环（`sum += a[i, k] * b[k, j]`）在 `sum` 上存在跨迭代循环依赖，很难向量化。将循环顺序调整为 `i-k-j`（`c_row[j] += aik * b_row[j]`）后，对每一个 `c[i, j]` 来说执行的仍然是**完全相同且顺序完全一致的加法**，但内层循环却变成了一段干净、连续的向量乘加！

让我们用代码验证两个版本产出完全一致的结果，同时将高速计算核声明为 `checkfree simd fn`：

```goose
import std;
import math;

const N = 16;

checkfree inline fn fma_step(acc: f64, a: f64, b: f64) -> f64 {
    return mul_add(a, b, acc);
}

// Scalar reference: i-j-k dot product
checkfree fn matmul_ref(a: const f64[:], b: const f64[:], c: f64[:]) {
    assert(a.len == N * N);
    assert(b.len == N * N);
    assert(c.len == N * N);

    for i in N {
        let a_row = a[i * N..i * N + N];
        let c_row = c[i * N..i * N + N];
        for j in N {
            var acc = 0.0;
            for k in N {
                acc += a_row[k] * b[k * N + j];
            }
            c_row[j] = acc;
        }
    }
}

// Vectorizable i-k-j row-order kernel: checkfree + simd + mul_add
checkfree simd fn matmul_simd(a: const f64[:], b: const f64[:], c: f64[:]) {
    assert(a.len == N * N);
    assert(b.len == N * N);
    assert(c.len == N * N);

    for idx in c.len {
        c[idx] = 0.0;
    }

    for i in N {
        let a_row = a[i * N..i * N + N];
        let c_row = c[i * N..i * N + N];
        for k in N {
            let aik = a_row[k];
            let b_row = b[k * N..k * N + N];
            for j in N {
                c_row[j] = fma_step(c_row[j], aik, b_row[j]);
            }
        }
    }
}

fn main() {
    var a: f64[>..] = [];
    var b: f64[>..] = [];
    var c_ref: f64[>..] = [];
    var c_fast: f64[>..] = [];

    for idx in N * N {
        a.push((idx % 7) * 0.25);
        b.push(((idx * 3) % 5) * 0.5);
        c_ref.push(0.0);
        c_fast.push(0.0);
    }

    matmul_ref(a, b, c_ref);
    matmul_simd(a, b, c_fast);

    var max_diff = 0.0;
    var sum = 0.0;
    for idx in N * N {
        let d = abs(c_ref[idx] - c_fast[idx]);
        if d > max_diff { max_diff = d; }
        sum += c_fast[idx];
    }

    print("n=", N, " checksum=", sum, " max_diff=", max_diff);
}
```

```text
n=16 checksum=3038.5 max_diff=0.0
```

观察 `matmul_simd` 有多么简洁：
- `c_row`、`a_row` 和 `b_row` 都是行切片，其长度在每行外层即被证明等于 `n`，因此内层的 `for j in n` 循环具有**零越界检查**（由 `checkfree` 强制保证）。
- `simd fn` 在 x86-64 的 clang 下会生成 AVX2+FMA 和 AVX-512+FMA 版本。
- `fma_step` 是一个 `inline fn`，因此它的 `mul_add` 会直接内联到每一个 SIMD 版本中，并编译成硬件 `vfmadd231pd` 指令！在 Goose 自身的实测中，把矩阵乘法从 `i-j-k` 改为行序 `i-k-j` 后，运行时间从 2.67 秒降至 SSE2 下的 0.91 秒，再降至 **AVX2 下的 0.49 秒**（总计提速 5.4 倍）。

---

## 4. 避免隐蔽的拷贝

Goose 采用值语义且没有隐式移动构造函数：拷贝一个大数组需要显式调用 `copy(x)`，其代价为 $O(\text{size})$；而**新构造的结果值会直接在目标位置上就地构造**（目标处直接构造）。只需养成三个习惯，就能让你的代码始终走在零拷贝路径上：

1. **每个返回非固定大小值的函数只返回同一个具名局部变量：**
   当函数内的每一条路径都返回同一个局部变量（`return out;`）时，Goose 会直接在调用方的接收数据栈上构建 `out`（即保证生效的具名返回值优化，NRVO）。如果一条分支返回 `local_a` 而另一条分支返回 `local_b`，或者你返回了一个切片 `return out[0..n]`，Goose 就不得不对结果执行拷贝。
   *（提示：在错误分支中返回空字面量 `return [];` 搭配正常路径的 `return out;` 依然开销极低，因为移动零字节不花任何成本——只有返回第二个非空局部变量才会迫使编译器做真正的拷贝。）*
2. **优先使用 `v.append(make_items())` 与 `v.push(make_item())`：**
   对于返回 `T[]` 的函数，`v.append(f())` 会为 `f` 编译出一个专用的元素流孪生版本，直接将元素写入 `v` 的尾部，无需分配任何临时数组。
3. **警惕 `u64` 循环变量：**
   正如第 18 章指出的，普通的 `u64` 变量会脱离越界检查证明的覆盖范围。请将循环计数器和计算索引保持为 `i64`（或索引类型 `u32 in a`）。对 `u64` 哈希值，请先直接与 `a.len` 比较，或用 `%` / `&` 归约后再转换为 `i64`。

---

## 5. 审查生成的 C 代码、选择后端与公正的基准测试

因为 Goose 编译生成的是可读、自包含的 C99 代码，你永远不需要靠猜测来推断编译器到底生成了什么。

### 诊断与审查工作流

```bash
# 1. Check how many bounds checks remain and on which lines:
goose check --stats --bce-lines kernel.goose

# 2. Check static data-stack allocation and specialization counts:
goose check --stacks --specs kernel.goose

# 3. Emit the generated C to inspect loops, views, SIMD dispatch, or assembly:
goose -o /tmp/kernel.c kernel.goose
clang -O2 -S -o /tmp/kernel.s /tmp/kernel.c   # inspect assembly if needed
```

在 `/tmp/kernel.c` 中，重点查找以下标记：
- `/* loop shape: ... */` 注释：标明了 Goose 在哪些位置应用了通道分块、保序浮点归约、有界展开或倒计数改写。
- `GS_IDX(...)` 调用：任何残留的越界检查都会一目了然。
- `<fn>_simd1` 与 `<fn>_simd2`：由 `simd fn` 生成的 AVX2 与 AVX-512 版本。

### 选择合适的后端

Goose 面向所有后端生成的 C 代码完全相同，但不同 C 编译器对其优化能力的差异却相当显著：

| 后端 | 预期角色 | 优势与注意事项 |
|---|---|---|
| **TinyCC（JIT，`goose run`）** | 即时编辑—运行反馈 | 毫秒级编译，零外部工具链依赖。**切勿用 `goose run` 跑基准测试**——TinyCC 几乎不做任何优化或向量化。 |
| **clang（`goose build`）** | 综合发布性能最佳 | 能够向量化带检查的循环，擅长调度基址+偏移加载，支持 `simd fn`（AVX2/AVX-512）、`GS_VEC` 以及无 `errno` 开销的 `__builtin_elementwise_sqrt`。 |
| **GCC（`goose build`）** | 强劲的 Linux 发布性能 | 自动向量化能力强，支持 `GS_VEC` 与外提除数检查；目前尚未生成 `simd fn` 多版本代码。 |
| **MSVC（`goose build`）** | Windows 原生构建 | 在递归碰撞分配（bump-allocation）负载上很快；但**对残留越界检查极为敏感**（哪怕一处检查都可能阻断向量化），且在扁平二维模板计算上偏慢（`blur` 扁平写法 952 ms，行切片写法 298 ms）。若以 MSVC 为目标，请优先采用行切片计算核。 |

### 公正地开展基准测试

在将 Goose 与 C++、Rust 或你自己的早期版本进行性能对比时，务必避开四大经典基准测试陷阱：

1. **始终构建发布版二进制文件（`goose build`），切勿使用 `--debug` 或 `goose run`。** 调试构建会开启有符号溢出检查、范围检查类型转换，并关闭全部优化。
2. **对完整生命周期计时，包括销毁阶段。** 在 Rust/C++ 中销毁一棵百万节点的 `Box` 或 `std::unique_ptr` 树，需要遍历并逐一释放每个节点——这与构建时一样是对缓存极不友好的遍历。而 Goose 只需一次指针重置即可释放整条数据栈。如果只测“构建”耗时，把内存回收丢给进程退出时的操作系统，就会掩盖每一个长驻服务器或游戏帧循环都必须承担的真实开销。
3. **对比等价的接口语义。** 将 Goose 拥有所有权的内联字符串（`u8[][>..]`）与 C++ 的 `std::vector<std::string_view>` 或 Rust 的 `Vec<&str>` 相比，实质上是在比较两种不同的生命周期契约。应当坚持“所有权对所有权、借用对借用”的对照原则。
4. **报告峰值 RSS（已提交物理页），而非虚拟地址空间。** Goose 为每条数据栈保留 2 GB 虚拟地址空间，但操作系统只会在页面实际被触及时才提交物理页。请将已提交 RSS 与逻辑有效载荷大小一并测量——你会看到在 Goose 中二者之间的差距大幅收窄。

---

## 6. 来自 Goose 基准测试套件的诚实经验

Goose 维护着一套包含 16 项测试的基准套件（`bench/`），将惯用 Goose 代码与惯用 C++、专家级 C++（自定义内存池、预分配向量、手动打包）以及惯用安全 Rust（在合乎惯例处采用 `Vec` 加索引的内存池）进行对比。每项测试都在三种数据规模下运行，并校验完全一致的校验和。

在 2026 年 10 月的大规模套件测试中（全部 16 项基准的几何平均值）：

| 对比基线 | 速度优势（MSVC / clang） | 峰值内存优势 |
|---|---|---|
| 对比惯用 C++ | **快 3.51× / 3.54×** | **内存减少 1.95×** |
| 对比专家级 C++ | **快 1.24× / 1.26×** | **内存减少 1.32×** |
| 对比最佳安全 Rust | **快 1.13× / 1.19×** | **内存减少 1.25×** |

比起笼统的几何平均数，更有价值的是看清*哪些*工作负载大获全胜、*哪些*势均力敌或略有落后——这能告诉你 Goose 的设计在何处最具红利，以及你应该把优化精力花在什么地方：

| 基准测试 | 测试内容 | 对比最佳安全 Rust / C++ 的结果 | 原因分析 |
|---|---|---|---|
| **`records`** | 含内联文本的异构记录 | 比 Rust **快 2.02–2.26×**，**内存少 3.98×** | 变长模式枚举按每条记录的精确载荷大小内联存储字符串——一整块扁平内存取代了逐字符串分配与最大变体填充。 |
| **`interp`** | AST 表达式解释器 | **快 1.29–1.41×**，**内存少 2.04×** | 变长模式 AST 节点 + 紧凑相对链接 + 标签分发（无虚函数表，无最大变体膨胀）。 |
| **`sexp`** | S 表达式解析与查询 | 比 Rust 内存池**内存少 2.43×**（比 C++ 节点**少 5.04×**） | 紧凑的变长模式节点 + 4 字节自相对链接 + $O(1)$ 销毁。 |
| **`bintrees`** | 深层树构建 + 遍历 + 销毁 | **204 ms** 对比 Rust 内存池 **235 ms**、C++ 内存池 **393 ms**（逐节点分配器 > 3,000 ms） | 碰撞分配的带类型引用 + 零逐节点释放/析构开销。 |
| **`graph` (CSR)** | 图构建 + 无检查 BFS | **617 ms**（clang）对比 C++ **761 ms**、Rust **811 ms** | `u32 in dist` 与 `u32 upto out` 在构建期一次性验证完毕；`checkfree fn bfs` 以零越界检查运行。 |
| **`respond`** | Web DTO 构建与渲染 | **491 ms** 对比 Rust DTO **1,338 ms**（流式版本：400 ms vs. 540 ms） | 内联字符串与记录数组避免了每个请求中嵌套的堆分配。 |
| **`particles`** | 扁平数值物理更新 | Goose、Rust 与 clang C++ **难分伯仲** | 扁平浮点数组在每种语言中本身就足够紧凑；底层 LLVM 后端生成几乎相同的机器码。 |
| **`blur`** | 二维图像模板卷积（`checkfree`） | clang **266–269 ms**（媲美 Rust 的 299 ms）；MSVC 下扁平写法 **952 ms** vs. 行切片 **298 ms** | 即使零越界检查，MSVC 仍需要行切片形式才能完成向量化；clang 对两种写法都能向量化。 |
| **`calc`** | 大量小型解析 + 频繁错误返回 | **481/491 ms** 对比 Rust 内存池 **436 ms**（小幅落后） | 单次输入工作量极小，放大了单次调用开销与 `varint`/分发的权衡成本。 |
| **`lru`** | 重度修改的双向链表缓存 | clang **2,068 ms**（领先 Rust 的 2,249 ms），MSVC **2,760 ms**（落后） | 重连自相对链接会把地址偏移运算放入指针追踪关键路径；MSVC 对基址+偏移的调度不佳。 |
| **`scene`** | 每帧修改的场景图 | clang **371 ms**（与 Rust 的 379 ms 持平），MSVC **460 ms**（落后） | 高频修改叠加 MSVC 后端的代码生成差异。 |

**结论非常明确：**
- 只要你的问题涉及**树、AST、异构记录、锯齿数组、字符串、DTO、图或序列化**，Goose 的内联内存模型就能提供结构性优势，即便是专家级 C++ 或 Rust 也需要极其冗长的手工代码才能勉强追平。
- 只要你的问题是**扁平数值数组计算核**，当你写出干净的 `checkfree` / `simd` 行切片循环并用 clang 或 GCC 编译时，Goose 会与 C++ 和 Rust 旗鼓相当——不要指望在相同的 LLVM 代码生成之上出现魔法般的额外提速。
- 只要你的问题属于**高频重连的可变操作**，请使用 `u32 in pool` 索引类型或 `in pool` 池相对链接代替自相对链接，并在目标 C 后端下实测验证。

---

## 7. 综合实战：布局对比与无检查 SIMD 滤波

让我们把数据布局工程、定宽算术、`inline fn`、无检查函数（`checkfree fn`）和 `simd fn` 融会贯通，构建一个自包含的信号处理示例。我们将：

1. 使用 `bytes_of` 对比以带填充的固定模式表示与以紧凑打包流方式存储 512 帧传感器数据的内存差异。
2. 用**三抽头水平 + 垂直可分离盒式模糊**处理一块二维灰度图像块：通过 `checkfree` 证明零越界检查，通过显式拓宽到 `u16` 避免 `u8` 溢出，并用 `simd fn` 为行处理核生成多版本代码。
3. 将 `simd fn` 的输出与简单的标量参考实现逐字节交叉验证。

```goose
// 1. Compact exact-width pixel kernel: accumulate in u16 so sums never wrap at 8 bits.
checkfree inline fn avg3(a: u8, b: u8, c: u8) -> u8 {
    var sum: u16 = a;
    sum += b;
    sum += c;
    return (sum / 3) as u8;
}

// 2. Separable horizontal pass on one row: checkfree + simd fn.
//    No early exits, pure arithmetic, zero bounds checks.
checkfree simd fn blur_row_simd(src: const u8[:], dst: u8[:]) {
    assert(src.len == dst.len);
    let w = src.len;
    if w == 0 { return; }
    if w == 1 {
        dst[0] = src[0];
        return;
    }

    dst[0] = avg3(src[0], src[0], src[1]);
    for x in 1..w - 1 {
        dst[x] = avg3(src[x - 1], src[x], src[x + 1]);
    }
    dst[w - 1] = avg3(src[w - 2], src[w - 1], src[w - 1]);
}

// 3. Full 2D blur using row slices (friendly to both clang and MSVC).
checkfree fn blur_2d_simd(
    src: const u8[:],
    tmp: u8[:],
    dst: u8[:],
    w: i64,
    h: i64,
) {
    assert(w >= 2 && h >= 2);
    assert(src.len == w * h);
    assert(tmp.len == w * h);
    assert(dst.len == w * h);

    // Pass 1: horizontal blur row by row into tmp.
    for y in h {
        let lo = y * w;
        blur_row_simd(src[lo..lo + w], tmp[lo..lo + w]);
    }

    // Pass 2: vertical blur using three hoisted row slices.
    for y in h {
        let ym = if y > 0 { y - 1 } else { 0 };
        let yp = if y + 1 < h { y + 1 } else { h - 1 };
        let r0 = tmp[ym * w..ym * w + w];
        let r1 = tmp[y * w..y * w + w];
        let r2 = tmp[yp * w..yp * w + w];
        let out = dst[y * w..y * w + w];
        for x in w {
            out[x] = avg3(r0[x], r1[x], r2[x]);
        }
    }
}

// 4. Scalar reference for verification.
fn blur_2d_ref(src: const u8[:], dst: u8[:], w: i64, h: i64) {
    var tmp: u8[>..] = [];
    tmp.resize(w * h, 0);

    for y in h {
        for x in w {
            let xm = if x > 0 { x - 1 } else { 0 };
            let xp = if x + 1 < w { x + 1 } else { w - 1 };
            var s: i64 = src[y * w + xm];
            s += src[y * w + x];
            s += src[y * w + xp];
            tmp[y * w + x] = (s / 3) as u8;
        }
    }

    for y in h {
        let ym = if y > 0 { y - 1 } else { 0 };
        let yp = if y + 1 < h { y + 1 } else { h - 1 };
        for x in w {
            var s: i64 = tmp[ym * w + x];
            s += tmp[y * w + x];
            s += tmp[yp * w + x];
            dst[y * w + x] = (s / 3) as u8;
        }
    }
}

fn main() {
    let w = 64;
    let h = 32;
    let total = w * h;

    var img: u8[>..] = [];
    var tmp: u8[>..] = [];
    var fast_out: u8[>..] = [];
    var ref_out: u8[>..] = [];

    for i in total {
        img.push(((i * 37 + 13) % 256) as u8);
    }
    tmp.resize(total, 0);
    fast_out.resize(total, 0);
    ref_out.resize(total, 0);

    blur_2d_simd(img, tmp, fast_out, w, h);
    blur_2d_ref(img, ref_out, w, h);

    var mismatches = 0;
    var checksum: u64 = 0;
    for i in total {
        if fast_out[i] != ref_out[i] {
            mismatches += 1;
        }
        checksum = checksum * 131 + fast_out[i];
    }

    print("pixels=", total, " mismatches=", mismatches, " checksum=", checksum);
}
```

```text
pixels=2048 mismatches=0 checksum=2968905902751271861
```

每一个像素都完全一致（`mismatches=0`），整条二维处理流水线都通过了编译器的 `checkfree` 验证，`blur_row_simd` 在 clang 下会自动分发至 AVX2/AVX-512，而用 `resize` 预先分配 `tmp` 使两趟循环都没有任何数组扩容操作，从而让 Goose 将每一行视图都外提到了寄存器中。

---

## 练习

1. **审查生成的 C 代码：** 取第 3 节中的 `matmul_simd` 程序，运行 `goose -o /tmp/matmul.c matmul.goose`（或 `goose check --stats --bce-lines matmul.goose`）。在生成的 C 代码中找到 `matmul_simd_simd1` 和 `matmul_simd_simd2`，并确认 `matmul_ref` 和 `matmul_simd` 内部均未出现 `GS_IDX`。
2. **实测 `varint` 与定宽整数的差异：** 定义两个变长模式枚举——一个用 `varint` 存储小计数器，另一个用 `u16`——向各自压入 100,000 个元素，比较二者的 `bytes_of(...).len`，然后使用 `goose build`（不要用 `goose run`）编译，并借助 `clock()`（来自 `import os;`）测量对全部元素求和 500 次的耗时。
3. **消除隐蔽拷贝：** 编写一个函数 `fn filter_positive(xs: const i64[:]) -> i64[>..]`，起初在两条分支上返回两个不同的局部数组（例如当没有任何匹配项时返回一个单独的 `fallback` 数组）。审查生成的 C 代码或传入大规模输入来观察拷贝行为，然后将其重构为所有路径都向同一个具名结果 `out` 构造或返回 `out`。
4. **可向量化的 ASCII 分类器：** 编写一个 `checkfree simd fn count_ascii_letters(s: const u8[:]) -> i64`，使用回绕减法（`(c - 'a') < 26` 和 `(c - 'A') < 26`——注意无符号减法按模 256 回绕！），且不包含提前退出和查表。在包含混合 ASCII 字符与高位字节（`0x80`–`0xFF`）的字符串上，将其结果与简单的 `for c in s` 循环对照验证。

---

## 本章要点

- **数据布局是最大的性能杠杆。** 变长模式枚举（`T..[>..]`）、内联变长字符串、紧凑结构体和窄链接可将工作集缩小 2 倍至 7 倍，并彻底消除分配器与销毁开销。
- **根据访问模式挑选合适的链接形式。** 一次构建、多次遍历的树以及可序列化格式应使用自相对链接（`T&<u32>`）；高频重连的可变结构应使用 `u32 in pool` 索引类型或 `in pool` 池相对链接。
- **配合视图外提与栈顶缓存。** 保持热点元素循环远离扩容操作，预先用 `resize` 分配目标缓冲区，并将互不相同的输出数组直接作为形参传给热点压栈辅助函数。
- **精准使用 `simd fn` 与 `inline fn`。** 将含循环、无越界检查、无分支的算术计算核标注为 `simd fn`，将逐元素辅助函数标注为 `inline fn`，并在需要硬件 FMA 的地方显式调用 `mul_add`（来自 `math`），同时在其他地方保持跨平台结果确定性。
- **诚实、公正地做基准测试。** 始终使用 `goose build` 编译（切勿用 TinyCC JIT 或 `--debug`），把销毁阶段纳入计时区间，对比等价的所有权接口，并在某个后端表现反常时直接审查生成的 C 代码。
