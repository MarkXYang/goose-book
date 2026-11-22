# 第 7 章：双尺寸枚举与标签分派

Goose 没有继承、没有类、没有接口，也没有虚表（vtable）。它唯一的动态多态形式是**代数数据类型（`enum`）**——一种编译器知晓所有可能变体的带标签联合体。

如果你使用过 Rust 的 `enum` 或 C++ 的 `std::variant`，Goose 的枚举乍看之下会非常熟悉——直到你遇到让它与众不同的两个核心设计：

1. **两种存储模式，在每个使用处按需选择：**枚举可以以**固定模式**（`Shape`，类似 Rust 或 C++——每个元素占用最大变体的大小）存储，也可以以**变长模式**（`Shape..`，每个元素*仅*占用自身变体的大小，包括内联字符串）存储。
2. **分支函数（Case 函数）：**你不必在各处重复编写庞大的 `match` 代码块，而是可以为每个变体类型（`Shape.Circle`、`Shape.Rect`、`Shape.Dot`）各编写一个函数重载，并直接在该枚举上调用。编译器会检查穷尽性，并通过跳转表按标签进行分派。

学完本章后，你将能够：

- 声明类 C 枚举、带负载枚举和泛型枚举，并对它们进行穷尽匹配。
- 在**固定模式（`E`）**和**变长模式（`E..`）**之间做出选择，并解释原地替换变体与持有内部引用之间的安全性权衡。
- 将变长字符串以零指针开销直接打包进变长模式枚举中。
- 编写穷尽的**分支函数**（按值传参与按引用传参两种形式），作为 Goose 无虚表的虚分派惯用法。

---

## 7.1 声明枚举与穷尽 `match`

`enum` 用于列举其所有变体。每个变体既可以携带类似结构体的字段（支持可选默认值和 `..` 部分字面量），也可以完全不带负载：

```goose
enum Shape {
    Circle { r: f64 },
    Rect { w: f64, h: f64 },
    Dot,
}

enum Maybe<T> {
    None,
    Some { val: T },
}

fn describe(s: Shape) -> const u8[:] {
    match s {
        Circle c => if c.r > 2.0 { "large circle" } else { "circle" },
        Rect r   => if r.w == r.h { "square" } else { "rectangle" },
        Dot      => "dot",
    }
}

fn is_round(s: Shape) -> bool {
    match s {
        Circle, Dot => true,         // multiple variants in one arm (no payload bound)
        Rect        => false,
    }
}

fn main() {
    let c = Shape.Circle { r: 3.0 };
    let r = Shape.Rect { w: 2.0, h: 2.0 };
    let d = Shape.Dot;
    let m: Maybe<i64> = Maybe<i64>.Some { val: 42 };

    print(describe(c), ", ", describe(r), ", ", describe(d));
    print("round: ", is_round(c), " ", is_round(r));
    match m {
        Some s => print("maybe=", s.val),
        None   => print("empty"),
    }
}
```

```
large circle, square, dot
round: true false
maybe=42
```

请注意对枚举使用 `match` 时的规则：

- **枚举类型与变体类型的区别：**不带类型注解时，`let c = Shape.Circle { r: 3.0 };` 会赋予 `c` 具体的*变体类型* `Shape.Circle`（当传给 `describe(c)` 时会隐式转换为 `Shape`）。若要将局部变量绑定为通用枚举类型以便直接对其执行 `match`，请显式加上类型注解（`let m: Maybe<i64> = ...`）。

- **穷尽性：**每个变体都必须出现在某个分支中，或者由末尾的 `_ => ...` 分支覆盖其余变体。同一变体被列出两次将导致编译错误。
- **面向表达式：**当 `match` 的所有分支产生共同类型时，整个 `match` 会求值为一个结果。
- **无负载枚举**（例如 `enum Color { Red, Green, Blue }`）仅占 **1 字节**——相当于支持穷尽匹配的类 C 枚举。

---

## 7.2 两种存储模式：固定模式（`Shape`）与变长模式（`Shape..`）

在 Rust 和 C++ 中，`enum` 或 `std::variant` 的每个值都必须为**最大**变体预留足够字节。如果你的节点中有 80% 是 1 字节的 `Dot` 或小型整数，而某个罕见变体携带了 64 字节的负载，那么每个元素都要付出 65 字节的代价。

在 Goose 中，你只需在**每个容器或变量处**写成 `Shape` 或 `Shape..`，即可决定存储模式：

```text
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }
Elements pushed: [ Circle(1.0), Rect(2.0, 3.0), Dot, Dot ]

Fixed mode — Shape[>..]  (17 bytes per element = 68 bytes total):
+---+--------+--------+---+--------+--------+---+-----------------+---+-----------------+
| C |  1.0   | (pad)  | R |  2.0   |  3.0   | D |    (unused)     | D |    (unused)     |
+---+--------+--------+---+--------+--------+---+-----------------+---+-----------------+
  1B    8B       8B    1B     8B       8B    1B         16B        1B         16B

Variable mode — Shape..[>..]  (9 + 17 + 1 + 1 = 28 bytes total!):
+---+--------+---+--------+--------+---+---+
| C |  1.0   | R |  2.0   |  3.0   | D | D |
+---+--------+---+--------+--------+---+---+
  1B    8B    1B     8B       8B    1B  1B
```

### 健全性权衡

为什么要提供两种模式而不是一种？因为如果**同时**允许原地覆盖变体*以及*持有指向其负载的引用，就会导致类型混淆（假设你持有一个 `r: Rect&`，而其他代码把该枚举替换成了 `Circle`，那么 `r` 就会把 `Circle` 的字节当作 `Rect` 来读取！）。

Goose 不需要借用检查器，而是将这一对能力拆分给两种模式，干净地解决了该问题：

| 特性 | 固定模式（`Shape`） | 变长模式（`Shape..`） |
|---|---|---|
| 大小类别 | **固定大小**（`1 + 最大负载`） | **变长**（`1 + 自身负载`） |
| 数组访问 | **可索引**（`shapes[i]`）且可弹出 | **顺序访问**（使用 `for` 迭代，或借助旁路引用数组索引） |
| 原地替换变体？ | **是**（`shapes[1] = Shape.Circle { ... }`） | **否**（标签永久冻结；相邻元素紧跟其后） |
| 指向负载的引用（`Rect &r`）？ | **否**（`match` 仅按值复制负载） | **是**（`match s { Rect &r => r.w += 1.0; }` 原地修改！） |
| 变长负载字段（`u8[varint]`）？ | **否**（所有负载必须为固定大小） | **是**（字符串与数组零指针内联打包！） |

让我们并排运行这两种模式，并用 `bytes_of(...).len` 测量它们的精确字节大小：

```goose
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }

fn main() {
    // Fixed mode: indexable and variant-overwritable, 17 bytes each.
    var fixed: Shape[>..] = [];
    fixed.push(Shape.Circle { r: 1.0 });
    fixed.push(Shape.Rect { w: 2.0, h: 3.0 });
    fixed.push(Shape.Dot);
    fixed.push(Shape.Dot);
    fixed[2] = Shape.Circle { r: 5.0 };          // replace Dot with Circle in place!
    print("fixed bytes=", bytes_of(fixed).len, " fixed[2]=", fixed[2]);

    // Variable mode: exact variant size (9 + 17 + 1 + 1 = 28 bytes) + in-place payload edit!
    var packed: Shape..[>..] = [];
    packed.push(Shape.Circle { r: 1.0 });
    packed.push(Shape.Rect { w: 2.0, h: 3.0 });
    packed.push(Shape.Dot);
    packed.push(Shape.Dot);

    for s in packed {                            // variable-size elements bind by reference
        match s {
            Rect &r => { r.w += 10.0; },         // edits payload straight inside `packed`!
            _       => {},
        }
    }
    print("packed bytes=", bytes_of(packed).len, " packed=", packed);
}
```

```
fixed bytes=68 fixed[2]=Circle { 5.0 }
packed bytes=28 packed=[Circle { 1.0 }, Rect { 12.0, 3.0 }, Shape.Dot, Shape.Dot]
```

如果你试图从**固定模式**枚举中获取内部负载引用（`Rect &r`），编译器会立即拒绝，以维护类型安全：

<!-- compile-fail -->
```goose
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }

fn main() {
    var fixed: Shape[>..] = [];
    fixed.push(Shape.Rect { w: 2.0, h: 3.0 });
    match fixed[0] {
        Rect &r => { r.w += 1.0; },  // error: cannot take payload reference into fixed-mode enum
        _       => {},
    }
}
```

> **内存模型：**在异构工作负载中（例如事件日志、编译器 AST，或小变体远多于大变体的混合记录流），变长模式枚举是 Goose 最强大的性能利器之一：在 `records` 基准测试中，与固定大小的 Rust `enum` 相比，其内存占用约减少了 **4 倍**，速度提升了约 **2 倍**。

---

## 7.3 变体类型与分支函数

枚举的每个变体本身也是一个**独立类型**：`Shape.Circle`、`Shape.Rect`、`Shape.Dot`。你可以直接使用具体变体类型声明变量、结构体字段、函数参数和引用（`Shape.Rect&`）。

这一设计催生了**分支函数（Case 函数）**——Goose 用于替代虚方法的简洁方案：

1. 为每个变体定义一个重载（`fn area(s: Shape.Circle)`、`fn area(s: Shape.Rect)`、`fn area(s: Shape.Dot)`）。
2. 传入枚举值（`Shape` 或 `Shape..`）调用 `area(s)`。
3. 编译器验证**每个变体都有且仅有一个匹配重载**，检查所有返回类型一致，并生成直接**基于标签的跳转表**——任何对象中都不必存储虚表指针！

更棒的是：如果某个重载**按引用**接收变体（`s: Shape.Rect&`），那么在**变长模式**枚举（`Shape..`）上调用它时，会直接绑定并原地修改负载！

```goose
import math;

enum Shape {
    Circle { r: f64 },
    Rect { w: f64, h: f64 },
    Dot,
}

// By-value case functions: work on BOTH fixed `Shape` and variable `Shape..`!
fn area(s: Shape.Circle) -> f64 { PI * s.r * s.r }
fn area(s: Shape.Rect) -> f64   { s.w * s.h }
fn area(s: Shape.Dot) -> f64    { 0.0 }

// By-reference mutating case functions: work on variable-mode `Shape..`!
fn scale(s: Shape.Circle&, k: f64) { s.r *= k; }
fn scale(s: Shape.Rect&, k: f64)   { s.w *= k; s.h *= k; }
fn scale(s: Shape.Dot&, k: f64)    {}

fn main() {
    var packed: Shape..[>..] = [];
    packed.push(Shape.Circle { r: 1.0 });
    packed.push(Shape.Rect { w: 2.0, h: 3.0 });
    packed.push(Shape.Dot);

    var before = 0.0;
    for s in packed { before += s.area(); }      // UFCS + tag dispatch!

    for s in packed { s.scale(2.0); }            // in-place mutation via case function!

    var after = 0.0;
    for s in packed { after += s.area(); }

    let c = Shape.Circle { r: 2.0 };             // variant type used directly (no dispatch)
    print("before=", before, " after=", after, " direct=", c.area());
}
```

```
before=9.141592653589793 after=36.56637061435917 direct=12.566370614359172
```

请注意，在具体的 `Shape.Circle` 上调用 `c.area()` 会零标签检查地直接调用 `area(Shape.Circle)`，而在 `Shape..` 上调用 `s.area()` 则会干净利落地按标签分派。此外，如果日后你为 `Shape` 新增了一个 `Tri` 变体，编译器会立即标记出每一处分支函数调用点，直到你为 `Tri` 补齐所有重载为止！

---

## 7.4 预览：自引用变长模式树

将变长模式枚举与窄相对引用（`Expr..&<u16>`，详见第 12 章）以及分支函数结合起来，整棵表达式树就能紧凑地装进寥寥数十字节的连续内存中：

```goose
enum Expr {
    Num { v: f64 },
    Add { l: Expr..&<u16>, r: Expr..&<u16> },
    Neg { x: Expr..&<u16> },
}

fn eval(e: Expr.Num&) -> f64           { e.v }
recursive fn eval(e: Expr.Add&) -> f64 { eval(e.l) + eval(e.r) }
recursive fn eval(e: Expr.Neg&) -> f64 { -eval(e.x) }

fn main() {
    var pool: Expr..[>..] = [];
    let a = pool.push(Expr.Num { v: 10.5 });     // 9 bytes (1 tag + 8 f64)
    let b = pool.push(Expr.Num { v: 4.5 });      // 9 bytes
    let sum = pool.push(Expr.Add { l: a, r: b });// 5 bytes (1 tag + 2 + 2)
    let neg = pool.push(Expr.Neg { x: sum });    // 3 bytes (1 tag + 2)
    print("result=", eval(neg), " total=", bytes_of(pool).len, " bytes");
}
```

```
result=-15.0 total=26 bytes
```

每个 `Neg` 节点仅占 **3 字节**，每个 `Add` 节点仅占 **5 字节**——没有为 `f64` 浪费任何填充字节，没有堆分配，并且采用直接跳转表分派！

---

## 7.5 综合实战：在同一程序中兼选两种模式

为了巩固何时选用固定模式、何时选用变长模式的直觉，我们来构建一个由两部分组成的小型遥测程序：

1. **迷你栈虚拟机（固定模式 `Op`）：**指令存放在 `Op[>..]` 中，这样程序计数器就能以 O(1) 时间**索引** `code[pc]`，而汇编阶段也能原地**回填**跳转指令（`code[1] = Op.JumpIfZero { to: ... }`）。
2. **内联事件流（变长模式 `Event..`）：**异构事件——1 字节的 `Tick`、9 字节的 `Gauge` 以及携带内联 `u8[varint]` 字符串的变长 `Note`——全部打包进同一个扁平的 `Event..[>..]` 中，配合用于随机访问的旁路索引 `(Event..&)[>..]`，并通过穷尽的**分支函数**进行原地修改与摘要输出！

```goose
// --- Part 1: Fixed-mode bytecode (indexable & patchable) ------------------
enum Op {
    Push { v: i64 },
    Add,
    Mul,
    JumpIfZero { to: u16 },
    Halt,
}

fn run_vm(code: Op[:]) -> i64 {
    var stack: i64[>..<] = [];
    var pc = 0;
    loop {
        let op = code[pc];
        pc++;
        match op {
            Push p       => { stack.push(p.v); },
            Add          => { let b = stack.pop(); let a = stack.pop(); stack.push(a + b); },
            Mul          => { let b = stack.pop(); let a = stack.pop(); stack.push(a * b); },
            JumpIfZero j => { if stack.pop() == 0 { pc = j.to; } },
            Halt         => { break; },
        }
    }
    return stack.pop();
}

// --- Part 2: Variable-mode events with inline text & case functions -------
enum Event {
    Tick,
    Gauge { id: i32, value: f32 },
    Note  { msg: u8[varint] },       // variable-size field -> variable mode only!
}

fn describe_event(e: Event.Tick&) -> u8[>..]  { str("tick") }
fn describe_event(e: Event.Gauge&) -> u8[>..] { str("gauge#", e.id, "=", e.value) }
fn describe_event(e: Event.Note&) -> u8[>..]  { str("note:", e.msg) }

fn calibrate(e: Event.Tick&, delta: f32)  {}
fn calibrate(e: Event.Gauge&, delta: f32) { e.value += delta; }
fn calibrate(e: Event.Note&, delta: f32)  {}

fn main() {
    // 1. Assemble and patch a tiny fixed-mode program: compute (6 + 4) * 3 = 30.
    var code: Op[>..] = [];
    code.push(Op.Push { v: 6 });
    code.push(Op.Push { v: 0 });     // placeholder, patched below!
    code.push(Op.Add);
    code.push(Op.Push { v: 3 });
    code.push(Op.Mul);
    code.push(Op.Halt);
    code[1] = Op.Push { v: 4 };      // overwrite variant/payload in place
    print("vm result=", run_vm(code), " (", bytes_of(code).len, " bytes for ", code.len, " ops)");

    // 2. Pack heterogeneous events inline and keep an index of references beside them.
    var events: Event..[>..] = [];
    var index: (Event..&)[>..] = [];
    index.push(events.push(Event.Tick));
    index.push(events.push(Event.Gauge { id: 1, value: 20.5 }));
    index.push(events.push(Event.Note { msg: "boot ok" }));
    index.push(events.push(Event.Tick));
    index.push(events.push(Event.Note { msg: "ready" }));

    // Mutate all gauges in place via a by-reference case function:
    for e in events { e.calibrate(1.5); }

    // Random access in reverse order via `index`:
    print("packed ", events.len, " events in ", bytes_of(events).len, " bytes:");
    for idx in [4, 1, 2, 0] {
        print("  [", idx, "] ", describe_event(index[idx]));
    }
}
```

```
vm result=30 (54 bytes for 6 ops)
packed 5 events in 27 bytes:
  [4] note:ready
  [1] gauge#1=22.0
  [2] note:boot ok
  [0] tick
```

看看这些数字：5 条异构事件——包含两个变长字符串 `"boot ok"` 和 `"ready"`、一条仪表读数和两次时钟滴答——总共只占用 **27 字节连续内存，且零堆分配**，同时依然支持原地负载修改以及通过 `index` 实现的 O(1) 索引访问！

---

## 练习

1. **体验穷尽的分支函数检查：**在 7.3 节的 `enum Shape` 中新增第四个变体 `Square { side: f64 }`。先不要添加对应的 `area` 或 `scale` 重载，点击 **▶ Run** 查看 Goose 给出的精确诊断信息，然后再补全这两个重载。
2. **测量不同模式的空间节省：**在 7.2 节的示例中，向 `fixed` 和 `packed` 各压入 100 个 `Shape.Dot` 值，对比 `bytes_of(fixed).len` 与 `bytes_of(packed).len`。
3. **扩展迷你虚拟机：**为 7.5 节的 `enum Op` 新增 `Sub` 和 `Dup` 两个变体，在 `run_vm` 中实现它们，并汇编一段程序计算 `(7 * 7) - 9`。

---

## 本章要点

- 代数数据类型（`enum`）是 Goose 唯一的动态多态形式——穷尽检查、零虚表且内联打包。
- 每个枚举都支持两种在使用处选择的存储模式：
  - **固定模式（`E`）**：每个值占 `1 + 最大负载` 字节；支持索引与原地替换变体；不允许获取内部负载引用。
  - **变长模式（`E..`）**：每个值占 `1 + 自身负载` 字节；顺序访问且标签冻结；允许获取内部负载引用（`Variant &v`）并支持内联变长字段（`u8[varint]`）。
- **分支函数**通过为每个变体类型（`E.Variant` 或 `E.Variant&`）各编写一个重载，经由穷尽跳转表按枚举标签进行分派。

在**第 8 章：结构体、根结构体与非嵌套生命周期**中，我们将为第二部分画上句号，探讨结构体内存布局、用于将多个可增长容器归于同一所有者的根结构体（`root struct`），以及标准库中面向非嵌套生命周期数据的 `slot_pool<T>`（槽位池）和 `slice_pool<T>`（切片池）。
