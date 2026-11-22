# 第 6 章：无标注的生命周期

如果你写过 Rust，一定写过类似 `fn longest<'a>(a: &'a str, b: &'a str) -> &'a str` 这样的函数签名，并且曾经与“不能同时持有指向同一结构的两个可变引用”的规则反复较劲。

而在 Goose 中，你只需要这样写：

<!-- fragment -->
```goose
fn longest(a: u8[:], b: u8[:]) -> u8[:] {
    if a.len >= b.len { a } else { b }
}
```

这里**没有生命周期语法**，**没有借用检查器的独占性规则**，也**没有 `unsafe`**——然而，Goose 依然会在编译期静态拒绝每一个悬垂引用（dangling reference）以及每一次缩容后使用（use-after-shrink）。

理解编译器*如何*证明引用安全性，只需要掌握寥寥几个简洁的概念：**根（root）**、**活跃性（liveness）**、**缩容规则**以及**平衡调用（balanced call）**。一旦看清它们如何环环相扣，生命周期错误就会变得一目了然，几秒钟内就能读懂并修复。

学完本章后，你将能够：

- 解释 Goose 如何在没有生命周期标注的情况下，跨越函数调用、分支、临时值与引用持有者自动推断生命周期的**根**。
- 借助活跃性而非词法作用域，干净利落地对只增数组（`T[>..]`）执行缩容操作。
- 在需要自由压栈与弹栈的场景下，合理使用增缩数组（`T[>..<]`）与**平衡调用**。
- 读懂并解决各类 Goose 生命周期、缩容及整数组赋值的编译器诊断信息。

---

## 6.1 根：编译器如何推断生命周期

每一个引用 `T&`、可选引用 `T?` 和切片 `T[:]` 都拥有一个静态的**根（root）**：即在存储上限定目标生命周期的那个变量（可以是局部变量、全局变量或调用方传入的参数）。

你永远不需要手动写出“根”。由于 Goose 是一个全程序单态化（whole-program monomorphizing）编译器，它会自动**推断**根，并针对不同的根对函数进行特化：

- `&x` 和 `x[..]` 的根**精确地**指向 `x`。
- 将 `outer` 和 `inner` 传给 `longest(outer, inner)` 时，编译器会针对这两个根特化 `longest`，因此返回的切片同时以 `outer` 和 `inner` 为根。
- 将引用存入**引用持有者（holder）**（例如包含引用字段的结构体，或切片数组 `u8[:][>..]`）时，编译器会把该根记录在引用持有者身上。

### 核心生命周期规则

Goose 中的每一个引用和切片都受以下七条精炼的规则约束：

1. **存储（Store）：** 只有当引用的*所有*根都比目标所属者活得更久时，才允许将该引用存入变量、字段、数组元素或全局变量。
2. **返回（Return）：** 返回的引用必须以在调用结束后依然存活的对象为根（调用方参数、全局变量或静态数据）——绝不能以函数自身的局部变量为根。
3. **分支与汇聚值（Branches & merged values）：** `if`、`match`、`block` 或 `loop` 是在其分支作用域关闭*之后*才产生最终值的，因此它们不能返回以分支内部声明的变量为根的引用。当一个值可能来自多个来源时，它会携带这些来源之根的并集，且必须同时满足对*所有*根的约束规则。
4. **重新绑定（`.=`）：** 一个 `var` 引用会绑定到其初始绑定所在的作用域深度；`.=` 可以将它重新绑定到同一个根内，或者绑定到同一作用域深度的另一个变量上。
5. **临时值（Temporaries）：** 字面量、函数返回值、`copy(...)` 或控制流表达式的生命周期仅持续到当前语句结束。你可以把临时值的视图传给函数调用，但不能将该视图存入活过当前语句的变量中。
6. **引用持有者（Holders）：** 每一次存入引用持有者的操作都会被追踪，以确保无论是作用域退出还是数组缩容，都不会让持有者内部遗留悬垂引用。
7. **精确根与非精确根（Exact vs. inexact roots）：** 取 `&arr[i]` 会向编译器提供**精确根**——即确切的数组 `arr`。而从容器中读出一个普通的 `T&` 时，编译器只能针对作用域中所有可能存放 `T` 的**候选根（candidates）**证明存活时间下界——这对于读写和修改已然足够，但需要确切数组身份的操作（例如 `.index_of` 或存储自相对引用链接）则必须具备精确根。

### 实战观察检查器的工作机制

首先，看 `longest` 如何阻止一个以内层代码块变量为根的引用逃逸到外层变量 `w` 中：

<!-- compile-fail -->
```goose
fn longest(a: u8[:], b: u8[:]) -> u8[:] {
    if a.len >= b.len { a } else { b }
}

fn main() {
    var outer: u8[>..] = [];
    outer.append("outer");
    var w = outer[..];
    {
        var inner: u8[>..] = [];
        inner.append("inner text");
        w = longest(outer, inner);   // error: rooted at inner, which dies at `}`
    }
    print(w);
}
```

接下来，看临时值视图规则如何拦截对函数返回值直接切片（却没有先赋予其所有者变量）的写法：

<!-- compile-fail -->
```goose
fn make_word() -> u8[>..] { str("transient") }

fn main() {
    let dangling = make_word()[..];  // error: view of a temporary dies at `;`
    print(dangling);
}
```

只需为返回值赋予一个具有所有权的局部变量（`let owned = make_word(); let view = owned[..];`），问题便迎刃而解，因为 `owned` 会一直存活到 `main` 结束。

### 为什么没有别名或独占性规则

请注意 Goose **并不**限制什么：你可以自由地同时持有指向完全相同的变量或数组元素的多个可写引用！

```goose
fn main() {
    var scores: i64[>..] = [10, 20, 30];
    let a .= scores[0];
    let b .= scores[0];              // two writable references to the exact same element!
    let whole = scores[..];          // plus a writable slice covering it!

    a += 5;
    b += 10;
    whole[0] *= 2;
    scores.push(40);                 // growing `scores` invalidates none of them!

    print("a=", a, " b=", b, " scores=", scores);
}
```

```
a=50 b=50 scores=[50, 20, 30, 40]
```

> **如果你来自 Rust：** 为什么 Rust 禁止同时存在两个 `&mut` 引用，而 Goose 允许呢？Rust 使用独占性同时防范两类风险：（1）当 `Vec` 重新分配内存或 `enum` 原地切换变体时导致的迭代器/引用失效；（2）跨线程的数据竞争。而 Goose 则通过结构性设计彻底消除了这三类风险：数据栈永不重新分配；固定模式枚举禁止内部引用，变长模式枚举禁止原地替换变体（第 7 章）；线程之间不共享任何可变内存（第 14 章）。因此，单线程下的别名在 Goose 中是完全内存安全的。

---

## 6.2 缩容只增数组（`T[>..]`）

“只增（Grow-only）”指的是一种**安全保证**，而非禁止清空或弹出元素：**只要还有任何以 `T[>..]` 为根的引用或切片处于活跃状态，该 `T[>..]` 就绝不会缩容。**

只要之后不再使用任何以该数组为根的引用或切片，在 `T[>..]` 上调用 `clear()`、`pop()`、`pop_n()`、向下 `resize()` 以及整数组重新赋值，全都是合法的。

### 基于活跃性，而非词法作用域

关键在于，Goose 检查的是**活跃性（liveness）**（即某个引用在缩容*之后*是否还会被读或写），而不是该引用变量是否仍在词法作用域内。这意味着声明在循环外部的可复用暂存缓冲区，可以在每次迭代开始时被安全清空，即便上一轮迭代内部曾切出过它的切片——只要这些切片的最后一次使用发生在下一次清空之前即可！

更棒的是，缩容检查可以**跨越函数调用**：如果辅助函数 `reset(buf)` 调用了 `buf.clear()`，编译器会记录 `reset` 会对其参数执行缩容，并在每一次调用 `reset` 处校验调用方的活跃视图：

```goose
fn reset(buf: u8[>..]&) {
    buf.clear();
}

fn main() {
    var line: u8[>..] = [];
    for i in 3 {
        reset(line);                 // safe: no slice of `line` is live here
        format(line, "row-", i + 1, ":ok");
        let prefix = line[..5];
        print(prefix, " -> ", line[6..]); // last use of `prefix`
    }

    // Even after the loop, clearing is allowed once no view is live:
    line.clear();
    print("final len=", line.len);
}
```

```
row-1 -> ok
row-2 -> ok
row-3 -> ok
final len=0
```

现在看看，如果一个**引用持有者**（`views: u8[:][>..]`）让 `line` 的切片跨越了 `line.clear()` 依然保持活跃，会发生什么：

<!-- compile-fail -->
```goose
fn main() {
    var line: u8[>..] = [];
    line.append("hello");

    var views: u8[:][>..] = [];
    views.push(line[..]);            // `views` now holds a slice rooted in `line`

    line.clear();                    // error: `views` is still used below!
    print(views);
}
```

同时还要留意语句级的求值顺序：当 `use` 按引用接收第一个参数时，`use(a[0], a.pop())` 会被编译器拒绝（因为 `a.pop()` 可能在 `use` 执行前释放 `a[0]`）；而 `a[0] + a.pop()` 则是合法的，因为按值求值的 `+` 会在计算 `a.pop()` 之前先读取 `a[0]`。

---

## 6.3 增缩数组（`T[>..<]`）与平衡调用

如果你需要一个栈、队列或二叉堆，希望能在任意位置自由弹出元素——包括将其放在全局变量中或结构体的尾部——而无需担心别处的某个容器偷偷存下了指向它的引用，该怎么办？

请使用 **`T[>..<]`**（增缩数组）。它的规则简洁而严格：

> **内存模型：** 指向 `T[>..<]` 的引用和切片**只能存活于局部变量（以及函数参数和返回值）中**——绝不允许存入结构体字段、数组元素或全局变量。由于没有任何容器或引用持有者能够隐藏指向它的引用，编译器只需纯粹针对当前仍在使用的局部变量来检查每一次缩容。

尝试将指向 `T[>..<]` 的引用存入结构体或另一个数组会被立即拒绝：

<!-- compile-fail -->
```goose
struct RefBox { r: i64& }

fn main() {
    var stack: i64[>..<] = [10, 20, 30];
    let b = RefBox { r: stack[0] };  // error: references into T[>..<] cannot be stored
    print(b.r);
}
```

### 平衡调用

有时，函数需要借用调用方的 `T[>..<]` 暂存栈作为临时工作区，而此时调用方已经持有了指向该栈较早条目的切片。

Goose 能够识别**平衡调用（balanced call）**：即一个函数先保存 `let mark = stack.len;`，在 `mark` 之上压入自己的临时条目，最后以 `stack.resize(mark);` 结束，且全程从未缩容到 `mark` 以下。由于 `mark` 之下的任何元素都从未被释放，**平衡调用在调用点不会被视为一次缩容**，因此调用方既有的视图能够安全地跨越此次调用继续存活！

```goose
fn sum_with_scratch(stack: i64[>..<]&, n: i64) -> i64 {
    let mark = stack.len;
    for i in 1..n + 1 {
        stack.push(i * i);
    }
    var total = 0;
    for i in mark..stack.len {
        total += stack[i];
    }
    stack.resize(mark);              // restores exact starting length -> balanced!
    return total;
}

fn main() {
    var stack: i64[>..<] = [100, 200];
    let saved = stack[..];           // view of [100, 200]

    // Safe! `sum_with_scratch` is balanced, so `saved` survives the call:
    let sq_sum = sum_with_scratch(stack, 4);
    print("saved=", saved, " sq_sum=", sq_sum, " stack.len=", stack.len);

    // `saved` is never used again after the line above, so popping is now legal:
    let top = stack.pop();
    print("popped=", top, " remaining=", stack);
}
```

```
saved=[100, 200] sq_sum=30 stack.len=2
popped=200 remaining=[100]
```

正是这一 `mark` / `resize(mark)` 模式，构成了第 11 章中跨递归调用共享单一暂存栈的核心基石。

---

## 6.4 整数组赋值与定容数组

对整个数组重新赋值（`xs = ...`）会如何与生命周期和缩容规则交互？

1. **可调整大小数组的整体赋值（`xs = expr;`）：** 先清空 `xs`，再直接在 `xs` 原有元素所在的位置上构造 `expr`。因此：
   - 它遵循与 `xs.clear()` 完全相同的缩容规则（不能存在指向 `xs` 的活跃视图）。
   - 因为 `xs` 在构造 `expr` *之前*就被清空了，`expr` **完全不能读取 `xs`**——哪怕读取 `xs.len` 也不行（`xs = f(xs);` 会报编译错误）。应当先在单独的变量中构造新数据，再通过 `copy(next)` 赋值。
2. **变长类别左值（`u8[]`、`Order`、`varint` 字段）：** 在构造完成后即被冻结，**绝不能**原地重新赋值；只能修改其中的固定大小字段，或重建外层容器。
3. **定容数组（`T[..k]`、`T[..]`）：** 容量与元素类型永不改变，因此指向 `T[..k]` 的引用即便跨越 `pop()` 和 `clear()` 依然保持内存安全——它会读取当前占据该存储槽位的任意有效 `T`。

```goose
struct Rec { name: u8[], score: i32 }

fn main() {
    // Whole resizable reassignment via separate staging variable:
    var xs: i64[>..] = [1, 2, 3];
    var next: i64[>..] = [];
    for x in xs { next.push(x * 10); }
    xs = copy(next);

    // Variable-class elements: fixed fields are mutable, variable fields are frozen:
    var recs: Rec[>..] = [];
    recs.push(Rec { name: "ada", score: 10 });
    for r in recs { r.score += 5; }
    recs = [Rec { name: "grace", score: 20 }]; // whole container rebuilt cleanly

    // Limited array: fixed capacity means references survive pop/push safely:
    var lim: i64[..4] = [7, 8];
    let slot0 .= lim[0];
    lim.clear();
    lim.push(42);
    print("xs=", xs, " recs=", recs, " slot0=", slot0);
}
```

```
xs=[10, 20, 30] recs=[Rec { "grace", 20 }] slot0=42
```

---

## 6.5 读懂 Goose 的生命周期与缩容诊断信息

每当检查器拒绝某段涉及生命周期或缩容的代码时，请**完整阅读报错信息**：Goose 总会明确指出冲突的**两端**——一边是涉及的引用（或引用持有者）*以及*它被绑定或存储的代码行，另一边则是与之冲突的作用域退出点或缩容操作。

使用下面这份四步排查清单，即可快速化解任何生命周期或缩容错误：

1. **在缩容前结束视图的使用：** 很多时候，只需将 `print(v)` 或从 `v` 中读取标量的语句挪到 `buf.clear()` *之前*，就能终止视图的活跃性，代码随即顺利通过编译。
2. **扩大所有者的作用域：** 如果内层变量的切片逃逸到了外层变量，要么将所有者声明在外层作用域，要么将使用方移入内层代码块。
3. **物化一份副本或内联值：** 如果指向临时缓冲区的切片确实需要活得比该缓冲区更久，请改用拥有所有权的 `u8[..k]` / `u8[]`，或调用 `copy(...)`。
4. **为生命周期模式挑选合适的容器：** 对于遵循栈式规则的暂存数据，使用配合平衡调用 `mark`/`resize` 的 `T[>..<]`；当对象生命周期完全不嵌套时，则使用 `slot_pool<T>` / `slice_pool<T>`（第 8 章）。

---

## 6.6 综合示例：可复用缓冲区的批处理求值器

让我们把第 6 章的全部规则融会贯通，编写一个小型批处理器，从数据流中解析由数字组成的文本行。它综合运用了：

- 一个**可复用的 `u8[>..]` 行缓冲区**，在每批处理前通过辅助函数清空（演示 `T[>..]` 基于活跃性的缩容机制）。
- 一个**共享的 `i64[>..<]` 暂存栈**，在调用方持有早期基线前缀活跃视图的同时，将其传入**平衡**的 `batch_stats` 辅助函数！
- 多个**别名引用**，干净利落地累加统计结果，完全不受借用检查器的掣肘。

```goose
import std;

fn reset_line(buf: u8[>..]&) {
    buf.clear();
}

// Balanced on `scratch`: pushes parsed values above `mark`, computes their sum
// and max, and restores `scratch.resize(mark)` before returning.
fn batch_stats(line: const u8[:], scratch: i64[>..<]&) -> i64, i64 {
    let mark = scratch.len;
    each_split(line, ',') {
        let v, ok = parse_int(trim(it));
        if ok { scratch.push(v); }
    };

    if scratch.len == mark {
        return 0, 0;
    }

    var sum = 0;
    var best = scratch[mark];
    for i in mark..scratch.len {
        let v = scratch[i];
        sum += v;
        if v > best { best = v; }
    }

    scratch.resize(mark);            // balanced: caller's views below `mark` stay live!
    return sum, best;
}

fn main() {
    let batches: const u8[:][3] = [
        "10, 25, 5",
        "4, 100, -10, 6",
        "7, 7, 7",
    ];

    var line_buf: u8[>..] = [];
    var scratch: i64[>..<] = [1000, 2000];
    let baseline = scratch[..];      // live across every balanced `batch_stats` call!

    var totals: i64[>..] = [];
    for raw, idx in batches {
        reset_line(line_buf);        // safe: no slice of `line_buf` survives the iteration
        format(line_buf, raw);
        let view = line_buf[..];
        let sum, best = batch_stats(view, scratch);
        totals.push(sum);
        print("batch ", idx, ": sum=", sum, " max=", best);
    }

    // Aliased references to the first total both mutate cleanly:
    let first_a .= totals[0];
    let first_b .= totals[0];
    first_a += 1;
    first_b += 2;

    print("baseline still intact: ", baseline);
    print("totals=", totals, " scratch.len=", scratch.len);
}
```

```
batch 0: sum=40 max=25
batch 1: sum=100 max=100
batch 2: sum=21 max=7
baseline still intact: [1000, 2000]
totals=[43, 100, 21] scratch.len=2
```

注意这套架构有多么干净：每批处理零内存分配、零生命周期标注，`line_buf` 在每轮迭代中被安全复用，而 `baseline` 因为 `batch_stats` 被静态验证为平衡调用，从而在全部三次调用期间都得到了安全保障！

---

## 练习

1. **刻意破坏平衡性：** 在第 6.6 节的综合示例中，修改 `batch_stats`，将 `scratch.resize(mark);` 改为 `scratch.clear();`。点击 **▶ Run**，查看编译器是如何精准指出 `baseline` 冲突的。
2. **测试引用持有者的缩容追踪：** 在第 6.2 节正常工作的 `reset(line)` 循环中，在 `for` 循环前声明 `var kept: u8[:][>..] = [];`，并在循环体内把 `prefix` 压入 `kept`。观察 Goose 如何在下一次调用 `reset(line)` 时捕获这个悬垂切片，然后通过把 `kept` 改为 `u8[][>..]`（存储拥有所有权的内联副本）来修复它。
3. **整数组自赋值：** 尝试对 `var xs: i64[>..] = [1, 2];` 执行 `xs = [xs[0] + 1, xs[1] + 1];`。阅读编译器关于“为何右侧表达式不能读取 `xs`”的解释，然后使用临时中转变量和 `copy(...)` 修复它。

---

## 本章要点

- Goose 通过全程序特化在编译期静态推断每个引用的**根**——在**零 `'a` 标注**、**无别名/独占性限制**的前提下，依然提供完整的悬垂引用安全性。
- 只增数组（`T[>..]`）在没有任何以它为根的活跃变量或引用持有者时，依然可以执行缩容（`.clear()`、`.pop()`、`.resize()`、整数组重新赋值）——该检查基于**活跃性**而非词法作用域，并能跨函数调用进行追踪。
- 增缩数组（`T[>..<]`）禁止将其引用存入容器，从而得以在任意位置弹出元素；**平衡调用**（`let mark = s.len; ... s.resize(mark);`）能够保留调用前获取的调用方视图。
- 整数组重新赋值会先清空再构造，因此右侧表达式绝不能读取目标数组。

接下来，在 **第 7 章：两种大小的枚举与标签分发** 中，我们将探索 Goose 如何在没有继承与虚函数表（vtable）的情况下实现动态多态——以及如何在**固定模式（`Shape`）**与**变长模式（`Shape..`）**之间做出权衡，从而同时解锁 4 倍的内存节省与安全的原地载荷引用。
