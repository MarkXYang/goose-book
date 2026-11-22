# 第 2 章：熟悉的语法，审慎的设计选择

如果你写过 C、C++、Rust 或 Go，几乎一眼就能读懂 Goose 代码。它使用花括号界定代码块、分号结束语句、后置 `: Type` 类型注解、`let` 与 `var` 声明绑定、`fn` 定义函数，以及 `match` 进行分支匹配。

然而，在每一处熟悉语法的背后，都隐藏着服务于 Goose 核心目标——**速度、安全与表达力**——的审慎设计决策：

- 算术运算严格按照**声明的精确位宽**执行，使紧凑循环能够留在 32 位或 8 位寄存器中，从而顺利完成自动向量化。
- `%` 采用**欧几里得取模**（结果绝不为负），因此回绕索引绝不会产生负的越界下标。
- `let` 控制*是否可重新绑定*，而 `const` 控制*内容是否可写*——将两个截然不同的概念清晰分离。
- 控制流面向表达式设计（`if`、`match`、`block`），并辅以 `guard` 实现扁平化的提前退出，以及 `defer` 实现确定性资源清理。
- 函数通过调用约定**直接返回多个值**——无需元组打包——并采用统一函数调用语法（Uniform Function Call Syntax，UFCS），而非类或 `impl` 块。

学完本章后，你将能够：

- 准确运用 `let`、`var`、`const`、具名常量与类型别名。
- 编写精确位宽的整数、浮点、位运算以及聚合类型的逐成员算术代码，避免意外的类型提升或隐蔽的溢出。
- 使用表达式块、`match`、`guard`、`defer` 以及 Goose 的 `for` 循环组织清晰易读的控制流。
- 编写支持多返回值、默认参数与方法风格 UFCS 调用的自由函数。

---

## 2.1 绑定、常量与类型语法

Goose 将**名称能否被重新赋值**与**其内容能否被修改**明确区分开来：

- `let` 声明一个不能作为整体被重新赋值的绑定。
- `var` 声明一个*可以*重新赋值的变量。
- `const` 将内容标记为只读（`const x = e;` 是类型为 `const T` 的 `let` 绑定的简写形式）。

```goose
fn main() {
    let answer = 42;                 // inferred as i64; cannot reassign `answer`
    var count: i32 = 0;              // explicit type; reassignable
    count += 1;
    count++;                         // statement only, not an expression

    let xs = [1, 2, 3];
    xs[0] = 9;                       // fine: `let` forbids `xs = ...`, not element writes

    const limits = [10, 20, 30];     // neither reassignable nor writable inside
    var view: const u8[:] = "goose"; // reassignable binding to read-only bytes
    view = view[1..];

    print(answer, " ", count, " ", xs, " ", limits, " ", view);
}
```

```
42 2 [9, 2, 3] [10, 20, 30] oose
```

注意其中的对称性：
- `let xs = [1, 2, 3];` 表示*绑定 `xs` 本身固定不变*，但其内部元素仍然可写。
- `var view: const u8[:] = "goose";` 则正好相反：*绑定 `view` 可以重新指向其他位置*，但它所查看的字节内容不可覆写。

试图修改 `const` 绑定内部元素的操作会在编译期被捕获：

<!-- compile-fail -->
```goose
fn main() {
    const limits = [10, 20, 30];
    limits[0] = 99;                  // error: cannot write through const
}
```

> **如果你有 Rust 背景：** 在 Rust 中，除非加上 `mut`，否则 `let` 意味着不可变。而在 Goose 中，`let` 表示*单次赋值绑定*（类似于 Java 的 `final` 或 C 中的 `const` 指针），而数据是否只读则是类型本身的属性（`const T`）。

### 确定性初始化与遮蔽

每个局部变量在任意控制流路径上被读取之前都必须完成初始化。未带初始值表达式的 `let` 绑定在每条路径上必须被赋值**恰好一次**：

```goose
fn main() {
    let label: const u8[:];
    let ready = true;
    if ready {
        label = "go";
    } else {
        label = "wait";
    }
    print(label);
}
```

```
go
```

在循环体内为声明于循环外部的未初始化 `let` 赋值会被编译器拒绝，因为循环可能会对其赋值多次。内层作用域可以自由遮蔽外层同名标识符。

### 具名常量与类型别名

带有编译期常量初始化表达式的**无显式类型** `let` 或 `const` 全局声明称为**具名常量**。与原始字面量一样，整型具名常量可以自适应任何能够容纳其值的整数类型，可用作定长数组的长度，也能出现在 `match` 分支中：

```goose
let WIDTH = 16;
let HALF = WIDTH / 2;

type ByteId = u8;

fn main() {
    let grid: u8[WIDTH] = [0; WIDTH];
    let id: ByteId = HALF;           // HALF adapts to u8
    let big = WIDTH * 1_000_000_000; // WIDTH adapts to i64
    let tag = match 8 {
        HALF => "middle",
        _    => "other",
    };
    print(grid.len, " ", id, " ", big, " ", tag);
}
```

```
16 8 16000000000 middle
```

*带有*显式类型标注的全局声明（例如 `let WIDTH: i64 = 16;`）只是普通的 `i64` 全局变量，**不会**自适应更窄的类型。`type` 声明创建的是透明类型别名——`ByteId` 与 `u8` 是完全相同的类型。

### 值得了解的词法细节

- **可嵌套块注释：** `/* outer /* inner */ still a comment */` 能够正确嵌套，因此注释掉一段本身已包含 `/* ... */` 的代码绝不会破坏编译。
- **数字分隔符：** 十进制、十六进制及浮点字面量的数字之间允许插入下划线（`1_000_000`、`0xFF_AA`、`3.141_592`、`0x1.8p3`）。
- **求值顺序：** 在任何位置均严格遵循从左到右——包括操作数、函数实参、结构体字段以及赋值目标。
- **后置类型语法：** 除 `const` 之外，所有类型构造器均为后置形式（`T[4]`、`T[>..]`、`T[:]`、`T&`、`T?`）。我们将在第 4 章至第 12 章逐一详细介绍。

---

## 2.2 精确位宽标量、欧几里得 `%` 与类型转换

Goose 提供固定位宽的二进制补码整数（`i8`、`i16`、`i32`、`i64`、`u8`、`u16`、`u32`、`u64`）、IEEE 浮点数（`f32`、`f64`）、1 字节的 `bool`，以及紧凑的 `varint` 存储类型（将在第 4 章随结构体和数组一起介绍）。

### 算术运算保持操作数的位宽

在 C 语言中，两个 `uint8_t` 相加会悄无声息地将两者提升为 `int`。Goose 绝不会隐式提升整数：**`u8 + u8` 就是 8 位加法**，**`i32 * i32` 就是 32 位乘法**。这使数值计算核心能够保留在窄寄存器中，让 C 后端得以自动向量化。

整数字面量本身不绑定具体类型：它们会自适应所遇到的任意整数类型（无约束时默认为 `i64`，若超过 `i64.max` 则为 `u64`）。如果字面量超出目标类型的表示范围，则会触发编译错误。

```goose
fn main() {
    let b: u8 = 255;
    let answer = 42;                 // unconstrained literal -> i64
    let wrapped: u8 = b + 1;         // 8-bit unsigned add wraps to 0
    let widened = b + answer;        // u8 widens implicitly into i64 -> 297
    let fits_u8 = b - 55;            // 55 adapts to u8 -> 200
    print(wrapped, " ", widened, " ", fits_u8);
}
```

```
0 297 200
```

当两个带类型的操作数在二元运算符中相遇时，Goose 会按照严格规则选择公共类型：

1. 类型相同时保持该类型。
2. 字面量自适应另一操作数的类型（若越界则编译报错）。
3. 如果其中一个类型可以无损隐式拓宽为另一类型（`u8 → i16`、`i32 → i64`、`f32 → f64`），则取更宽的类型。
4. 任意整数与浮点数相遇时，均转换为该浮点类型（在 `f32` 上下文中 `n * 0.5` 为 `f32`，否则为 `f64`）。
5. 同位宽的有符号与无符号整数混用（`u32 + i32`），或 `u64` 与任意有符号整数混用，均属于**编译错误**——必须显式转型，让意图一目了然。

### 无符号回绕、有符号溢出以及 `as` 与 `as!` 的区别

- **无符号溢出在所有构建模式下都按模 2^width 回绕**，这是有意为之的设计——哈希函数、伪随机数生成器（PRNG）和位运算技巧都依赖于此。
- **有符号溢出在 `--debug` 构建中会直接中止程序（abort）**，在 release 构建中则回绕。除以零或对零取余始终中止程序（`i64.min / -1` 亦然）。
- **`x as T`** 用于数值类型转换，在 `--debug` 构建中会执行**范围检查**（若数值发生变化则中止），在 release 构建中则截断或回绕。
- **`x as! T`** 即使在 `--debug` 模式下也是**有意不做检查的转换**——每当你需要刻意进行位截断或符号重解释时，应使用 `as!`。

```goose
fn main() {
    // Unsigned math wraps cleanly (FNV-1a hash):
    var h: u64 = 14695981039346656037;
    for c in "goose" {
        h = (h ^ c) * 1099511628211;
    }

    let big = 300;
    let low_byte: u8 = big as! u8;   // intentional truncation: 44
    let pi_approx = 3.75;
    let whole = pi_approx as i64;    // truncates toward zero: 3
    print(h, " ", low_byte, " ", whole);
}
```

```
3870774399305851480 44 3
```

> **编译器提示：** Goose 会对多余的类型转换发出警告（例如当 `b` 为 `u8` 且已在上下文中隐式拓宽为 `i64` 时还写成 `b as i64`）。善用隐式拓宽，把 `as` / `as!` 留给可能发生窄化、改变符号或决定运算位宽的转换。

### 欧几里得 `%` 与 `rem`、`div_euclid`

在 C、C++ 和 Rust 中，`-7 % 3` 的结果是 `-1`。每当你需要将坐标回绕到宽度为 `W` 的网格或环形缓冲区中时，负数余数总会迫使你写下丑陋的 `((x % W) + W) % W` 变通写法——还会干扰越界检查消除。

在 Goose 中，**整数 `%` 是欧几里得取模**：结果始终落在 `[0, |b|)` 区间内，**绝不为负**。由于对正数 `n` 而言，`x % n` 保证非负且 `< n`，编译器能够自动消除 `buf[x % n]` 上的越界检查。

整数除法 `/` 依然向零截断（`-7 / 3 == -2`）。当你需要严格配套的商或向零截断的余数时，`std` 提供了 `div_euclid`（与 `%` 配套）和 `rem`（与 `/` 配套）：

```goose
import std;

fn main() {
    print("truncating: ", -7 / 3, " rem ", rem(-7, 3));
    print("euclidean:  ", div_euclid(-7, 3), " mod ", -7 % 3);
    // Tile coordinate and in-tile offset for position x = -1 with tile width 10:
    print("tile=", div_euclid(-1, 10), " offset=", -1 % 10);
}
```

```
truncating: -2 rem -1
euclidean:  -3 mod 2
tile=-1 offset=9
```

### 位运算符与聚合类型的逐成员运算

以下两项贴心设计让 Goose 的系统编程与图形代码格外清爽：

1. **位运算符优先级遵循 Rust 而非 C：** `~`、`&`、`|`、`^`、`<<`、`>>` 的绑定优先级**高于** `==` 与 `!=`，因此 `flags & MASK != 0` 会直接解析为 `(flags & MASK) != 0`。像 `~4` 和 `1 << k` 这样的常量表达式也会自适应目标整数位宽。
2. **结构体与定长数组的逐成员算术运算：** Goose 没有运算符重载；取而代之的是，`+`、`-`、`*`、`/`、`%` 以及一元 `-` 会对字段全为数值类型的结构体和定长数组按元素逐一运算；当所有叶子字段共享同一类型时，`*` 和 `/` 还支持任意一侧为标量。

```goose
struct Vec3 { x: f32, y: f32, z: f32 }

fn main() {
    // Bitwise flags with clean precedence and constant adaptation:
    var flags: u8 = 0;
    let READ: u8 = 1;
    let WRITE: u8 = 2;
    flags |= READ | WRITE;
    flags &= ~READ;
    let bit40: u64 = 1 << 40;
    print("writable=", flags & WRITE != 0, " bit40=", bit40);

    // Memberwise struct and fixed-array math:
    let a = Vec3 { 1, 2, 3 };
    let b = Vec3 { 0.5, 0.5, 0.5 };
    var c = (a + b) * 2;
    print("c=", c, " sum=", [1, 2, 3] + [10, 20, 30]);
}
```

```
writable=true bit40=1099511627776
c=Vec3 { 3.0, 5.0, 7.0 } sum=[11, 22, 33]
```

另请注意，`bool` 绝不会从整数隐式转换而来：必须写 `if count != 0`，而不能写 `if count`。

---

## 2.3 表达式、`block`、`guard` 与 `defer`

Goose 是**面向表达式**的语言：`if`、`match`、`block` 以及裸代码块 `{ ... }` 都会求值为其末尾的尾随表达式（不带分号）。语言不设三元运算符 `?:`，因为 `if cond { a } else { b }` 已经足够简洁明了。

以块结尾的语句（`if`、`match`、`block`、各类循环、`guard ... else { ... }`）末尾无需分号。若要将它们作为操作数嵌入更大的表达式中，用圆括号括起来即可：`let v = (if ok { 10 } else { 20 }) + 1;`。

### `if` 与 `match`

作为表达式使用时，`if` 必须带有末尾的 `else` 分支，而针对整数的 `match` 必须包含通配分支 `_`。整数 `match` 分支支持字面量、具名常量、左闭右开区间 `lo..hi` 以及逗号分隔的值列表：

```goose
let ZERO = 0;

fn describe_hp(hp: i64) -> const u8[:] {
    let status = if hp > 50 { "healthy" } else { "hurt" };
    match hp {
        ZERO     => "down",
        1..20    => "critical",
        20..51   => status,
        _        => "healthy",
    }
}

fn main() {
    for hp in [0, 12, 35, 80] {
        print(hp, " -> ", describe_hp(hp));
    }
}
```

```
0 -> down
12 -> critical
35 -> hurt
80 -> healthy
```

### 使用 `guard` 与 `block` 编写扁平校验逻辑

层层嵌套的 `if` 阶梯会把正常执行路径埋藏得很深。Goose 提供了两种可以优雅组合的工具：

- `guard cond;` 仅在 `cond` 为真时执行当前代码块的剩余部分（在循环体中，守卫条件不成立时会直接跳到下一次迭代）。
- `guard cond else { ... }` 在条件不成立时改而执行 `else` 块——其中通常包含 `return`、`break` 或 `continue`。
- `block { ... }` 创建一个可供 `break value;` 跳出的代码块目标，无需借助伪循环。

```goose
struct Unit { alive: bool, energy: i32, stunned: bool }

fn action(u: Unit) -> const u8[:] {
    block {
        guard u.alive else { break "fallen"; }
        guard !u.stunned else { break "stunned"; }
        guard u.energy >= 10 else { break "resting"; }
        "ready"
    }
}

fn main() {
    print(action(Unit { true, 25, false }));
    print(action(Unit { true, 5, false }));
    print(action(Unit { false, 50, false }));
}
```

```
ready
resting
fallen
```

在第 5 章中，你将看到 `guard` 还能一举两得——将可空的可选引用（`T?`）类型收窄为安全的非空引用（`T&`），供代码块剩余部分直接使用。

### 使用 `defer` 实现确定性作用域清理

Goose 没有析构函数——内存会在作用域退出、数据栈回退时自动回收。对于**非内存资源**（关闭图形窗口、恢复状态计数器、释放外部句柄、刷新日志缓冲），Goose 提供了 `defer`。

无论所在代码块通过**哪条路径**退出——正常结束、`break`、`continue`、`return`，或是跨帧返回（`return ... from`，详见第 10 章）——`defer stmt;` 或 `defer { ... }` 都会执行。多个 `defer` 按**后进先出（LIFO）顺序**运行（最后注册的最先执行），且只有控制流实际经过的 `defer` 才会被执行。

```goose
var depth = 0;

fn scan(xs: i64[:]) -> i64 {
    depth++;
    defer depth--;                   // runs on every return from `scan`
    var visited = 0;
    for x in xs {
        defer visited++;             // runs at the end of each iteration, even on `continue`
        if x < 0 { continue; }
        if x > 100 { return visited; }
    }
    return visited;
}

fn main() {
    print("r1=", scan([1, -2, 3]), " depth=", depth);
    print("r2=", scan([10, 200, 30]), " depth=", depth);
}
```

```
r1=3 depth=0
r2=1 depth=0
```

> **注意：** 仔细观察 `r2=1`：在第 0 次迭代（`x == 10`）末尾，`defer visited++` 执行，使 `visited` 变为 `1`。在第 1 次迭代（`x == 200`）中，`return visited` 会*先*构造返回值（`1`），随后在各层作用域退出时依次执行循环体的 `defer visited++` 和函数的 `defer depth--`。另需注意，`defer` 代码块内部不能向外层跳转（不可使用指向外层作用域的 `return`/`break`/`continue`），且在硬中止（`abort`、`assert` 失败或 `exit`）时不会执行。

---

## 2.4 循环

Goose 拥有三种循环结构——`for`、`while` 和 `loop`：

- `for i in n` 依次遍历 `0, 1, ..., n - 1`。
- `for i in a..b` 遍历左闭右开区间 `[a, b)`。
- `for i: u8 in 0..256` 允许显式指定循环变量的精确类型——注意 `256` 可以作为 `u8` 的开区间上界，让你干净利落地遍历全部 256 个字节值而不会溢出！
- `for x in xs` 遍历任意数组或切片的元素（固定大小元素按值拷贝，变长元素按引用绑定）。
- `for &x in xs` 将每个元素按引用绑定，从而支持原地修改。
- `for x, i in xs` 同时绑定元素及其下标（默认为 `i64`，当长度放得下时也可采用更窄类型）。
- `loop { ... }` 进行无条件循环，并可通过 `break expr;` 产生求值结果。

```goose
fn main() {
    var xs: i64[>..] = [2, 4, 6];
    for &x in xs {
        x *= 10;                     // mutates elements in place
    }

    var weighted = 0;
    for x, i in xs {
        weighted += x * (i + 1);
    }

    var bytes = 0;
    for b: u8 in 0..256 {
        if b % 64 == 0 { bytes++; }
    }

    var n = 0;
    let first_hit = loop {
        n += 7;
        if n % 5 == 0 { break n; }
    };

    print("xs=", xs, " weighted=", weighted, " quarter_marks=", bytes, " hit=", first_hit);
}
```

```
xs=[20, 40, 60] weighted=280 quarter_marks=4 hit=35
```

> **内存模型：** `for` 循环会**原地**遍历数组，不发生拷贝。对于只增数组 `T[>..]`，每次迭代前都会重新读取 `.len`（因此在 `for` 循环内部追加元素会延长遍历），同时数组在循环进行期间不允许缩容——这一安全规则我们将在第 6 章深入探讨。

---

## 2.5 函数、多返回值、默认参数与 UFCS

Goose 中的函数全部是**自由函数**——没有类、方法或 `impl` 块。取而代之的是，Goose 支持**统一函数调用语法（UFCS）**：写作 `x.f(a, b)` 等价于调用 `f(x, a, b)`。编译器会优先查找内置字段和成员；若不存在，则解析为以 `x` 作为第一个参数的自由函数。

```goose
struct Rect { w: i64, h: i64 }

fn area(r: Rect) -> i64 { r.w * r.h }

fn clamp_val(x: i64, lo: i64 = 0, hi: i64 = 100) -> i64 {
    if x < lo { lo } else if x > hi { hi } else { x }
}

fn countdown_sum(var n: i64) -> i64 {  // `var` makes a by-value parameter reassignable
    var total = 0;
    while n > 0 {
        total += n;
        n--;
    }
    return total;
}

fn main() {
    let r = Rect { w: 4, h: 7 };
    print("area=", r.area(), " clamped=", 250.clamp_val(), " ", (-5).clamp_val(-2, 10));
    print("sum=", countdown_sum(5));
}
```

```
area=28 clamped=100 -2
sum=15
```

留意上面展示的两条实用参数规则：
- 形参默认是 `let` 绑定。若希望在函数体内对按值传入的形参重新赋值，只需加上 `var` 前缀（如 `var n: i64`）。
- 末尾形参可以声明默认值（`lo: i64 = 0, hi: i64 = 100`），并在省略对应实参的每个调用点处求值。

### 多返回值

Goose **没有元组类型**。相反，作为调用约定的一部分，函数可以在 `->` 后声明以逗号分隔的多个返回类型。按照惯例，对于简单的成败二元结果操作，通常在末尾附加一个 `bool` 来报告成功与否：

```goose
fn divide(a: i64, b: i64) -> i64, bool {
    guard b != 0 else { return 0, false; }
    return a / b, true;
}

fn checked_half(n: i64) -> i64, bool {
    return divide(n, 2);             // forwards all return values directly
}

fn main() {
    let q, ok = divide(17, 5);
    var r = 0;
    var ok2 = false;
    r, ok2 = divide(9, 0);           // reassign existing variables with `=`
    let h, ok3 = checked_half(14);
    let only_first = divide(20, 4);  // single-value context takes the first result
    print(q, " ", ok, " | ", r, " ", ok2, " | ", h, " ", ok3, " | ", only_first);
}
```

```
3 true | 0 false | 7 true | 5
```

> **注意：** 由于多返回值是一种调用约定而非元组值，`if` 或 `match` 分支不能直接求值为 `a, b`。写成 `return if ok { 1, true } else { 0, false };` 会导致编译错误——应当像上面的 `divide` 那样，在各个分支中直接 `return`。

---

## 2.6 综合示例：环形网格探测车

让我们把第 2 章的特性汇总到一个紧凑而自包含的模拟程序中：一辆在环形回绕二维网格上移动的探测车，综合运用具名常量、带默认字段值的结构体（`..`）、逐成员向量运算、欧几里得 `%` 回绕、`guard`/`block` 状态检查、`defer` 步数统计、UFCS 以及多返回值。

```goose
let GRID_W = 8;
let GRID_H = 6;

struct Vec2 { x: i64, y: i64 }

struct Rover {
    pos: Vec2,
    battery: i32 = 10,
    steps: i32 = 0,
}

fn wrap(p: Vec2) -> Vec2 {
    // Euclidean `%` wraps negative coordinates straight into [0, GRID_W / GRID_H).
    Vec2 { x: p.x % GRID_W, y: p.y % GRID_H }
}

fn status(r: Rover) -> const u8[:] {
    block {
        guard r.battery > 0 else { break "depleted"; }
        guard r.battery > 3 else { break "low-battery"; }
        "nominal"
    }
}

fn step(r: Rover&, delta: Vec2) -> Vec2, bool {
    guard r.battery > 0 else { return r.pos, false; }
    defer r.steps++;                 // counted whenever a move succeeds
    r.battery -= 2;
    r.pos = (r.pos + delta).wrap();  // memberwise `+` and UFCS `.wrap()`
    return r.pos, true;
}

fn main() {
    var rover = Rover { pos: Vec2 { 1, 0 }, .. };
    let moves: Vec2[6] = [
        Vec2 { -2, -1 },             // wraps across both edges!
        Vec2 { 3, 2 },
        Vec2 { 0, -4 },
        Vec2 { 1, 1 },
        Vec2 { 1, 0 },
        Vec2 { 1, 0 },               // battery hits 0 before this move
    ];

    for mv, i in moves {
        let pos, moved = rover.step(mv);
        print("move ", i, ": moved=", moved, " pos=", pos,
              " bat=", rover.battery, " [", rover.status(), "]");
    }
    print("total steps taken: ", rover.steps);
}
```

```
move 0: moved=true pos=Vec2 { 7, 5 } bat=8 [nominal]
move 1: moved=true pos=Vec2 { 2, 1 } bat=6 [nominal]
move 2: moved=true pos=Vec2 { 2, 3 } bat=4 [nominal]
move 3: moved=true pos=Vec2 { 3, 4 } bat=2 [low-battery]
move 4: moved=true pos=Vec2 { 4, 4 } bat=0 [depleted]
move 5: moved=false pos=Vec2 { 4, 4 } bat=0 [depleted]
total steps taken: 5
```

留意第 5 步移动：`guard r.battery > 0 else { return r.pos, false; }` 在执行到达 `defer r.steps++` *之前*就已返回，因此 `rover.steps` 保持在 `5` 而不是 `6`。这正是 Goose 的 `defer` 感知作用域执行顺序的原因——只有经过 `defer` 所在行之后，对应的清理或记账逻辑才会在退出时运行。

---

## 练习

1. **体验精确位宽算术：** 在在线演练场（playground）中修改第 2.2 节的代码片段，尝试不加类型转换直接将 `u32` 与 `i32` 相加。阅读编译器给出的确切诊断信息，然后使用最小的拓宽转换将其修复。
2. **为探测车添加地形消耗：** 扩展第 2.6 节的环形网格探测车，使用 `match (p.x + p.y) % 4` 新增 `fn tile_cost(p: Vec2) -> i32` 函数，使部分地块消耗 `1` 点电量、部分消耗 `2` 点，而岩石地块（`3`）消耗 `3` 点。当 `rover.battery < cost` 时拒绝此次移动。
3. **预测 `defer` 的执行顺序：** 编写一个包含三条 `defer print(...)` 语句的函数——一条放在 `if` 之前，一条放在 `for` 循环体内，一条放在 `guard` 之后——验证在正常结束和提前 `return` 两种情况下它们的确切执行顺序。

---

## 本章要点

- `let` 禁止重新绑定，`var` 允许重新绑定，`const` 使内容只读；无显式类型的常量全局变量像字面量一样自适应类型。
- 算术运算严格保持操作数的精确位宽，不会隐式提升为 `int`；无符号溢出按定义回绕，有符号溢出在 `--debug` 下中止；当截断是有意为之时，使用 `as!` 跳过调试期范围检查。
- 整数 `%` 为欧几里得取模（绝不为负），令回绕索引既安全又便于越界检查消除；`std` 另提供 `div_euclid` 与 `rem`。
- 面向表达式的 `if`、`match`、`block`、`guard` 与 `defer` 使控制流保持扁平，同时保证资源清理的确定性。
- 自由函数与 UFCS（`x.f(a)`）、末尾默认参数以及零开销调用约定级别的多返回值相得益彰。

既然你已经掌握了 Goose 的日常语法，**第 3 章**将直入这门语言的核心：**大小类别、数据栈、栈不变式以及原地构造保证**。
