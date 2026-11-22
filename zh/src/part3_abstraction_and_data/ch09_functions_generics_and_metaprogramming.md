# 第 9 章：函数、泛型与编译期展开

在许多语言中，追求优雅的抽象——泛型工具、像 `filter` 和 `map` 这样的高阶迭代器，或结构化的结构体检查——往往伴随着一笔隐蔽的性能账单：堆分配的闭包、间接函数指针调用、特征对象虚表（vtable）或运行时反射元数据。

在 Goose 中，**所有这些抽象都会在编译期彻底消失**。由于 Goose 对整个程序进行全程序编译，并对每个泛型和函数值执行单态化，`xs.filter() { it % 2 == 0 }` 会被编译成与手写完全一致的循环，直接在调用方的目标位置构造结果；而 `const for` 会在结构体字段或枚举变体上展开为普通的、带类型的 Goose 代码，没有任何运行时元数据开销。

学完本章后，你将能够：

- 熟练使用静态重载、捕获自由变量的嵌套函数以及函数限定符。
- 编写单态化的泛型函数（包括无类型标注的参数和显式 `<T>` 写法），并理解字面量参数与泛型 `.=` 的规则。
- 使用编译期**函数值**、尾随 `{ ... }` 代码块和非局部 `return` 来编写与调用高阶函数。
- 利用 **`const for`** 和 **`const if`**（`fields<T>`、`variants<E>`、`types<...>`、`same`、`has_default`）生成零开销的结构化代码。

---

## 9.1 重载、嵌套函数与函数限定符

### 静态重载

函数按参数类型进行重载，解析完全在编译期完成（绝不依赖预期的返回类型）。对于每个实参，Goose 按从优到劣的顺序对候选函数排序：

1. 精确类型匹配
2. 泛型精确匹配
3. 隐式转换（数组 → 切片、字面量适配、隐式拓宽）
4. 整数 → 浮点数转换（最低优先级）

候选函数的综合优先级取其所有实参中**最差**的那一项；若出现并列，则报编译错误。

### 嵌套函数

你可以直接在另一个函数内部声明辅助函数。嵌套函数在常规生命周期规则下，可以像访问自由变量一样读写外层函数的局部变量——这使它非常适合编写需要共享局部状态表的有状态辅助函数或解析器，而无需在每次调用时层层传递庞大的上下文结构体。

同一代码块中的同级嵌套函数可以按任意顺序相互调用，甚至彼此重载（包括嵌套的分支函数（Case 函数））。与函数值一样，嵌套函数是编译期实体，不能逃逸出其所在的作用域。

```goose
fn describe(x: i64) -> u8[>..]     { str("int(", x, ")") }
fn describe(x: f64) -> u8[>..]     { str("float(", x, ")") }
fn describe(xs: i64[:]) -> u8[>..] { str("slice[", xs.len, "]") }

struct Employee { name: u8[..12], dept: u8[..8], salary: i32 }

fn main() {
    let nums: i64[3] = [4, 8, 15];
    print(describe(7), " | ", 2.5.describe(), " | ", nums.describe());

    var staff: Employee[>..] = [];
    staff.push(Employee { name: "ada",   dept: "eng", salary: 120 });
    staff.push(Employee { name: "linus", dept: "ops", salary: 90 });
    staff.push(Employee { name: "grace", dept: "eng", salary: 135 });

    // Nested function directly updates `main`'s locals `budget` and `count`:
    var budget = 0;
    var count = 0;
    fn tally(e: Employee&) {
        budget += e.salary;
        count++;
    }

    for &e in staff {
        if e.dept == "eng" { tally(e); }
    }
    print("eng budget=", budget, " over ", count, " people");
}
```

```
int(7) | float(2.5) | slice[3]
eng budget=255 over 2 people
```

### 函数限定符速览

在阅读本书的过程中，你会陆续遇到七种函数限定符。每当组合使用它们时，必须遵循固定顺序：

```text
export | extern  ->  checkfree  ->  simd  ->  inline  ->  recursive  ->  fn | thread_fn
```

我们将在第 19 章介绍 `inline` 和 `simd`，第 18 章介绍 `checkfree`，第 11 章介绍 `recursive`，第 14 章介绍 `thread_fn`，第 15 章介绍 `extern` / `export`。

---

## 9.2 无需特征约束的单态化泛型

在 Goose 中，**任何未标注类型的参数都自动成为泛型参数**：`fn twice(x) { x + x }` 对整数、浮点数、数值结构体和定长数值数组同样适用。

在以下情况下使用显式类型参数列表 `<T>`：
- 两个参数必须共享**相同**类型（`fn span<T>(a: T, b: T) -> T`），或
- 某个类型参数未出现在任何实参中（`fn zero<T>() -> T { default<T>() }`）。

Goose 会为每个具体调用点对泛型函数进行单态化，并直接对实例化后的函数体做类型检查——因此**无需声明任何特征（trait）或接口约束**。如果某次实例化尝试执行该具体类型不支持的操作，编译器会在精确的调用链位置报错。

三条微妙的规则让 Goose 泛型用起来格外顺手：

1. **字面量参数：** 如果只有字面量绑定了 `T`，它会在特化体内自动适配——例如 `push_n(bytes, 1, 4)` 会无需强制转换地把 `1` 作为 `u8` 推入 `u8[>..]`，而 `push_n(bytes, 300, 4)` 则会在编译期清晰地报错。
2. **浮点上下文：** 当泛型函数从浮点字面量或整数表达式返回裸类型 `T` 时，若目标位置类型为 `f32`，整个表达式都会被细化为 32 位浮点运算（如 `let s: f32 = max(0.5, min(w / 960.0, h / 600.0));`）。
3. **泛型 `.=`：** 在泛型函数内部，当 `T` 是值类型时，`a .= b` 执行普通的值赋值；而当 `T` 是引用类型时，它会重新绑定**引用本身**！这使得 `swap_ends` 或 `std` 的 `sort` 等泛型算法可以用同一份实现，既能重排值数组，也能重排引用数组。

```goose
fn twice(x) { x + x }

fn span<T>(a: T, b: T) -> T {
    if a > b { a - b } else { b - a }
}

fn zero<T>() -> T { default<T>() }

// Generic `.=` moves whole elements whether T is a value (i64) or a reference (i64&)!
fn swap_ends<T>(xs: T[:]) {
    guard xs.len >= 2;
    let last = xs.len - 1;
    let tmp: T = xs[0];              // holds a value for T=i64, or a reference for T=i64&
    xs[0] .= xs[last];
    xs[last] .= tmp;
}

fn main() {
    print(twice(21), " ", twice(1.5), " ", span(3, 8), " ", zero<f64>() + 0.5);

    var vals: i64[>..] = [10, 20, 30];
    swap_ends(vals);

    var refs: (i64&)[>..] = [vals[0], vals[1], vals[2]];
    swap_ends(refs);                 // swaps the references, NOT the underlying ints!

    print("vals=", vals, " refs=", refs);
}
```

```
42 3.0 5 0.5
vals=[30, 20, 10] refs=[10, 20, 30]
```

仔细看最后一行输出：使用完全相同的泛型函数体，`swap_ends(refs)` 反转了外层的引用（`refs` 现在指向 `[10, 20, 30]`），同时 `vals` 保持 `[30, 20, 10]` 原封不动！

---

## 9.3 函数值、尾随代码块与非局部 `return`

Goose 中的高阶函数通过泛型参数 `F` 接收**函数值**（你可以选择性地写上类似 `F: fn(i64)` 的签名提示作为文档说明）：

- 每个函数值都是**编译期实体**，绝不是运行时数据。
- 你可以紧跟在调用括号后传入一个**尾随代码块** `{ ... }`（使用隐式参数 `it` 或具名参数 `a, b => ...`），也可以传入一个**具名函数**（它会携带其完整的重载集与默认参数）。
- 每次对 `F(...)` 的调用都是直接调用并内联进调用方——**没有闭包对象，没有堆分配，也没有间接函数指针**。
- 代码块可以自由读写其外层函数的局部变量。
- **非局部 `return`：** 在尾随代码块中执行 `return expr;` 会从**词法上的外层具名函数**返回，而不是从高阶函数返回！这意味着你自己编写的任何自定义迭代器，都拥有与内置 `for` 循环完全相同的提前退出能力。

```goose
import std;

fn each_pair<T, F>(xs: T[:]) {
    for i in 0..xs.len - 1 {
        F(xs[i], xs[i + 1]);
    }
}

fn each_step<F>(lo: i64, hi: i64, step: i64 = 1) {
    var i = lo;
    while i < hi {
        F(i);
        i += step;
    }
}

fn first_multiple_of_7(lo: i64, hi: i64) -> i64 {
    each_step(lo, hi, 3) {
        if it % 7 == 0 { return it; } // returns directly from `first_multiple_of_7`!
    };
    return -1;
}

fn desc(a: i64, b: i64) -> bool { a > b }

fn main() {
    let xs: i64[5] = [4, 8, 15, 16, 23];

    var gaps: i64[>..] = [];
    each_pair(xs) { a, b => gaps.push(b - a); };

    // Built directly in place with zero intermediate temporary arrays:
    let evens = xs.filter() { it % 2 == 0 };
    let total = fold(xs, 0) { acc, x => acc + x };
    sort(gaps, desc);                // pass a named function as F

    print("gaps(desc)=", gaps, " evens=", evens, " total=", total);
    print("first mult of 7: ", first_multiple_of_7(10, 40), " / ", first_multiple_of_7(10, 12));
}
```

```
gaps(desc)=[7, 7, 4, 1] evens=[4, 8, 16] total=66
first mult of 7: 28 / -1
```

由于函数值是编译期实体，**它们不能逃逸**——试图将函数值存入局部变量、放入结构体或从函数返回，都会在编译期被拒绝：

<!-- compile-fail -->
```goose
fn double(x: i64) -> i64 { x * 2 }

fn main() {
    let f = double;                  // error: function values cannot be stored as data
    print(f(3));
}
```

> **设计权衡：** 禁止逃逸的闭包和函数指针，正是 Goose 能够在静态阶段掌握完整调用图的前提——由此才实现了静态数据栈分配、零标注的根推断以及有保证的内联。当你确实需要对封闭的行为集合进行运行时动态派发时，请使用 `enum` + 分支函数（Case 函数）（第 7 章）。

---

## 9.4 特化期展开：`const for` 与 `const if`

手写遍历结构体每个字段的样板代码（用于打印、比对差异、重置默认值或序列化）既繁琐又容易出错：日后只要新增一个字段却忘了更新辅助函数，就会埋下隐蔽的 Bug。

Goose 用 **`const for`** 和 **`const if`** 解决了这个问题——在普通泛型函数内执行编译期展开，**既不需要文本宏，也没有运行时反射开销**。

### `const for` 的四种数据源

| 数据源 | 绑定变量 | 展开对象 |
|---|---|---|
| `fields<T>` | `<N, A>` | 结构体或变体 `T` 的每个直接字段：`N` 为字段名（`const u8[:]`），`A` 为字段访问器（可用于 `v.A` 和 `T { A: ... }`） |
| `variants<E>` | `<N, V>` | 枚举 `E` 的每个变体：`N` 为变体名，`V` 为变体本身（可用于 `match` 分支、`fields<V>` 和构造器） |
| `types<T1, T2, ...>` | `<U>` | 显式指定的类型序列 |
| `[c1, c2, ...]` | `<C>` | 显式指定的编译期常量序列 |

`const for` 和 `const if` 可以出现在三个位置：**语句列表**、**结构体构造初始化列表**和 **`match` 分支列表**。

### `const if` 与编译期查询（`same`、`has_default`）

`const if` 在特化期间只选择一个分支，且**绝不对未选中的分支做类型检查**——因此某个分支可以编写仅对 `i64` 或仅对特定结构体合法的操作，而在为其他类型特化时不会报错！可以使用 `else const if ... else { ... }` 串联多个分支。

在 `const if` 条件中可使用两个内置编译期查询：
- `same<T1, T2>` —— 当且仅当 `T1` 与 `T2` 在替换后是完全相同的类型（包括 `const` 修饰）时为 `true`。
- `has_default<A>` —— 当且仅当字段访问器 `A`（来自 `fields<T>`）在结构体定义中显式声明了 `= default` 默认值时为 `true`。

```goose
struct Config {
    host: u8[..16],
    port: i32 = 8080,
    retries: i32 = 3,
}

enum Shape {
    Circle { r: i64 },
    Rect { w: i64, h: i64 },
    Dot,
}

// Reset every field that declared a default back to its default, keeping others!
fn reset_defaults<T>(src: T&) -> T {
    T {
        const for <N, A> in fields<T> {
            const if !has_default<A> { A: src.A, }
        },
        ..
    }
}

// Generic enum + payload printer using nested `variants<E>` and `fields<V>`:
fn inspect_enum<E>(v: E) {
    match v {
        const for <N, V> in variants<E> {
            V payload => {
                print("variant ", N);
                const for <F, A> in fields<V> {
                    print("  ", F, " = ", payload.A);
                }
            },
        }
    }
}

fn type_tag<T>(x: T) -> const u8[:] {
    const if same<T, i64> { return "i64"; }
    else const if same<T, f64> { return "f64"; }
    else { return "other"; }
}

fn main() {
    var cfg = Config { host: "api.local", port: 9000, retries: 10 };
    let clean = reset_defaults(cfg);
    print("reset: ", clean);

    let s: Shape = Shape.Rect { w: 4, h: 9 };
    inspect_enum(s);

    var sizes: i64[>..] = [];
    const for <C> in [1, 2, 4] {
        let buf: u8[C] = [0; C];
        sizes.push(buf.len);
    }
    const for <U> in types<i64, f64, bool> {
        print("tag=", type_tag(default<U>()), " zero=", default<U>());
    }
    print("sizes=", sizes);
}
```

```
reset: Config { "api.local", 8080, 3 }
variant Rect
  w = 4
  h = 9
tag=i64 zero=0
tag=f64 zero=0.0
tag=other zero=false
sizes=[1, 2, 4]
```

看看 `reset_defaults<T>` 和 `inspect_enum<E>` 有多简洁：没有在运行时遍历反射表的循环，没有堆分配，而且对每一个具体的 `T` 和 `E` 都经过完整的类型检查！

---

## 9.5 综合实例：零样板的结构体差异比对与校验器

让我们把第 9 章的各项特性——单态化泛型、带非局部 `return` 的尾随代码块、遍历 `fields<T>` 的 `const for`，以及 `const if`（`has_default<A>`、`same`）——组合起来，构建一个小巧而通用的诊断工具包，自动适用于**任意**结构体：

- `diff_fields<T>(a, b)` 打印 `a` 与 `b` 之间所有值发生变化的字段，并返回差异数量。
- `format_kv<T>(v)` 将任意结构体格式化为 `key=val; ...`，并用 `*` 标出声明了默认值的字段。
- `find_first_invalid<T, F>(items)` 使用校验代码块扫描切片，并借助非局部 `return` 在遇到第一条不合规记录时立即退出。

```goose
import std;

fn diff_fields<T>(a: T&, b: T&) -> i64 {
    var diffs = 0;
    const for <N, A> in fields<T> {
        if a.A != b.A {
            print("  changed ", N, ": ", a.A, " -> ", b.A);
            diffs++;
        }
    }
    return diffs;
}

fn format_kv<T>(v: T&) -> u8[>..] {
    var out: u8[>..] = [];
    var first = true;
    const for <N, A> in fields<T> {
        if !first { out.append("; "); }
        first = false;
        format(out, N);
        const if has_default<A> { out.push('*'); }
        format(out, "=", v.A);
    }
    return out;
}

fn each_with_index<T, F>(xs: T[:]) {
    for &x, i in xs { F(x, i); }
}

fn first_failing_index<T, F>(xs: T[:]) -> i64 {
    each_with_index(xs) { item, idx =>
        if !F(item) { return idx; }  // non-local return out of `first_failing_index`!
    };
    return -1;
}

struct Server {
    name: u8[..16],
    port: i32 = 443,
    workers: i32 = 4,
    tls: bool = true,
}

fn main() {
    var s1 = Server { name: "edge-1", .. };
    var s2 = Server { name: "edge-1", port: 8443, workers: 8, tls: true };

    print("s1: ", format_kv(s1));
    print("s2: ", format_kv(s2));
    print("diffing s1 vs s2:");
    let n = diff_fields(s1, s2);
    print("total diffs: ", n);

    var fleet: Server[>..] = [
        Server { name: "edge-1", .. },
        Server { name: "edge-2", port: 80, workers: 0, tls: false },
        Server { name: "edge-3", .. },
    ];
    let bad = first_failing_index(fleet) { it.workers > 0 && it.port > 0 };
    print("first invalid server index: ", bad, " (", fleet[bad].name, ")");
}
```

```
s1: name=edge-1; port*=443; workers*=4; tls*=true
s2: name=edge-1; port*=8443; workers*=8; tls*=true
diffing s1 vs s2:
  changed port: 443 -> 8443
  changed workers: 4 -> 8
total diffs: 2
first invalid server index: 1 (edge-2)
```

只要给 `struct Server` 加上第五个字段 `region: u8[..8] = "us-east"`，`format_kv` 和 `diff_fields` 就会在编译期自动将其纳入处理——而且零运行时开销！

---

## 练习

1. **体验自动反射：** 在第 9.5 节的示例中，为 `struct Server` 添加 `region: u8[..8] = "us-east"`，并将 `s2.region` 改为 `"eu-west"`。点击 **▶ Run**，确认无需修改 `format_kv` 和 `diff_fields` 的任何一行代码，两者就都自动反映出了新字段。
2. **用 `const if` 编写泛型 `sum_ints<T>`：** 借助 `const for <N, A> in fields<T>` 和 `const if same<...>`（或检查数值字段），编写辅助函数 `fn count_fields<T>() -> i64` 和 `fn count_defaulted<T>() -> i64`，并在 `Config` 与 `Server` 上测试它们。
3. **带非局部 `return` 的自定义迭代器：** 编写 `fn each_window2<T, F>(xs: T[:])`，对每对相邻元素组成的长度为 2 的切片调用 `F(xs[i..i + 2])`；再编写 `fn first_descending_pair(xs: i64[:]) -> i64`，使用 `each_window2` 和非局部 `return` 返回第一个满足 `it[0] > it[1]` 的元素。

---

## 本章要点

- 函数按实参类型静态重载，**嵌套函数**无需借助额外结构体即可清晰地共享外层函数的局部变量。
- 泛型（`fn f(x)` 或 `fn f<T>(...)`）在每个具体调用点进行单态化，**无需特征约束**，可自动适配字面量参数，并通过泛型 `.=` 统一值移动与引用重绑定。
- **函数值**（`F` + 尾随 `{ it }` 代码块）是编译期实体：直接调用、可内联、零分配、不可逃逸，并支持从词法外层函数跳出的**非局部 `return`**。
- **`const for`**（`fields<T>`、`variants<E>`、`types<...>`、`[c1, ...]`）与 **`const if`**（`same`、`has_default`）无需宏或运行时元数据即可提供零开销的编译期反射。

接下来，在**第 10 章：错误处理与跨帧返回**中，我们将看到 Goose 如何利用支撑非局部 `return` 的同一套零栈展开调用图机制，实现 **`return E from f`**——无需 `Result`、无需满屏 `?` 噪音，也无需异常，就能写出优雅的深层错误处理代码！
