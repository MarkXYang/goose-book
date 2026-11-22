# 第 11 章：栈模型下的递归

在第 3 章中，你学到 Goose 编译器会在程序运行之前静态计算出精确的数据栈数量 `N`。现在不妨停下来思考一下，这对**递归**意味着什么：

如果一个递归函数声明了一个可增长数组 `var buf: u8[>..] = [];`，并在调用自身时让 `buf` 保持存活，那么深度为 50 的递归就需要 50 个同时存在的数据栈——深度为 1,000 就需要 1,000 个。由于递归深度是一个运行时属性，编译器无法为每一次递归调用激活静态分配一个独立的数据栈。

Goose 没有悄无声息地回退到堆分配器，而是通过 **`recursive fn`** 将递归显式化，并强制执行一条清晰的规则：**任何非固定大小的局部变量或临时值都不得跨越递归调用保持存活。** 一旦掌握了应对这条规则的四种惯用模式，Goose 中的递归代码就能纯粹运行在原生调用栈上，每次调用零分配——而且往往比传统的堆分配版本*更简洁*。

读完本章后，你将能够：

- 编写 `recursive fn` 递归环，并理解编译器对函数签名、局部变量、临时值和存储操作的每一条规则。
- 当临时缓冲区仅在递归之前或之后需要时，使用**块作用域临时缓冲区**。
- 使用**调用者拥有的缓冲区**（例如递归归并排序）以及**采用平衡调用的 `T[>..<]` 临时栈**来组织分治算法。
- 通过在非递归驱动函数内**嵌套递归函数**并结合跨帧返回 `return ... from`，构建简洁的递归下降解析器。

---

## 11.1 `recursive fn` 与核心栈规则

Goose 中的递归需要显式声明，并在函数签名中清晰可见：

1. **用 `recursive fn` 标记每个递归环的入口**（对于 `a` 和 `b` 之间的相互递归，标记进入该环的那个函数——或两者都标记）。
2. **为递归环中的每个函数编写显式的参数类型和返回类型**（递归环中不允许推导返回类型）。
3. **禁止按值传递非固定大小参数：** 递归环中的函数必须通过引用（`T[>..]&`）或切片（`T[:]`）接收数组或容器，绝不能按值传递。
4. **禁止非固定大小的局部变量或临时值跨越递归调用存活：** 可增长或变长局部变量不能跨越回到递归环的调用——调用语句本身的非固定大小临时值同样如此（例如 `recurse(str("x", n))`，或在 `print` 执行 `recurse` 期间持有临时缓冲区的 `print(recurse(n))`）。
5. **以局部为根的引用仅限于局部：** 你可以将指向递归环函数自身固定大小局部变量的引用*向下*传递给被调用者，但不能将其存入外部的引用持有者中，也不能将其返回（同一个局部变量的多次递归激活在静态上无法区分）。以递归环*外部*为根的引用（调用者的池、外层非递归函数的局部变量、全局变量）则不受此限制。

下面展示了当一个递归函数试图让可增长局部变量跨越递归调用存活时会发生什么：

<!-- compile-fail -->
```goose
recursive fn bad_walk(n: i64) -> i64 {
    var buf: u8[>..] = [];           // holds a data stack until the end of `bad_walk`
    format(buf, "level ", n);
    if n > 0 {
        let sub = bad_walk(n - 1);   // error: `buf` is still in scope across recursive call!
        return sub + buf.len;
    }
    return buf.len;
}

fn main() {
    print(bad_walk(3));
}
```

> **编译器提示：** 同样要留意**调用语句中的临时值**：如果 `print` 在调用递归函数时打开了一个非固定大小的格式化临时缓冲区，`print("depth=", count(root))` 可能会报错。先绑定递归调用的结果（`let c = count(root); print("depth=", c);`），错误就会立刻消失。

---

## 11.2 模式一：递归前的块作用域临时缓冲区

如果一个递归函数只在发起递归调用*之前*（或*之后*）需要可增长缓冲区——例如格式化一行输出——只需将该缓冲区包裹在一个**在递归调用之前就结束**的 `{ ... }` 代码块中。到达右花括号 `}` 时，数据栈会干净地回退，因此在递归期间持有的存活数据栈数量为零！

```goose
struct Node {
    key: i32,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

recursive fn print_tree(n: Node&, depth: i64) {
    {
        // Scratch lives ONLY inside this block, freed before we recurse!
        var line: u8[>..] = [];
        for i in depth { line.append("  "); }
        format(line, "- ", n.key);
        print(line);
    }
    let l = n.left;
    if l { print_tree(l, depth + 1); }
    let r = n.right;
    if r { print_tree(r, depth + 1); }
}

recursive fn count_nodes(n: Node?) -> i64 {
    guard n else { return 0; }
    return 1 + count_nodes(n.left) + count_nodes(n.right);
}

fn main() {
    var pool: Node[>..] = [];
    let root .= pool.push(Node { key: 50 });
    let left .= pool.push(Node { key: 25 });
    let right .= pool.push(Node { key: 75 });
    left.left .= pool.push(Node { key: 10 });
    root.left .= left;
    root.right .= right;

    print_tree(root, 0);
    let total = count_nodes(root);
    print("total nodes: ", total);
}
```

```
- 50
  - 25
    - 10
  - 75
total nodes: 4
```

---

## 11.3 模式二：调用者拥有的缓冲区（递归归并排序）

对于需要与输入规模成正比的工作存储空间的分治算法，可以在非递归调用者中分配**一个**临时缓冲区，然后将切片沿递归向下传递。每一层递归都会切分切片（`xs[..mid]`、`tmp[..mid]`），全程零分配，也不需要手动维护索引偏移量：

```goose
import std;

fn insertion_sort(xs: i32[:]) {
    for i in 1..xs.len {
        let v = xs[i];
        var j = i;
        while j > 0 && xs[j - 1] > v {
            xs[j] = xs[j - 1];
            j--;
        }
        xs[j] = v;
    }
}

recursive fn merge_sort(xs: i32[:], tmp: i32[:]) {
    if xs.len <= 4 {
        insertion_sort(xs);
        return;
    }
    let mid = xs.len / 2;
    merge_sort(xs[..mid], tmp[..mid]);
    merge_sort(xs[mid..], tmp[mid..]);

    var i = 0;
    var j = mid;
    var k = 0;
    while i < mid && j < xs.len {
        if xs[j] < xs[i] { tmp[k] = xs[j]; j++; }
        else             { tmp[k] = xs[i]; i++; }
        k++;
    }
    while i < mid    { tmp[k] = xs[i]; i++; k++; }
    while j < xs.len { tmp[k] = xs[j]; j++; k++; }
    xs.move_from(tmp[..xs.len]);
}

fn main() {
    var nums: i32[>..] = [42, 7, 19, 3, 88, 12, 5, 29, 1, 14];
    var scratch: i32[>..] = [];
    scratch.resize(nums.len, 0);     // one scratch buffer for the entire recursion

    merge_sort(nums, scratch);
    print("sorted: ", nums);
}
```

```
sorted: [1, 3, 5, 7, 12, 14, 19, 29, 42, 88]
```

请注意它的运行速度有多快：即使对一百万个整数排序，临时缓冲区也只在 `main` 中分配**一次**，而每一次递归调用仅使用一个极小的原生栈帧。

---

## 11.4 模式三：带平衡调用的共享增缩临时栈

如果每一次递归激活都需要事先*未知*大小的动态临时存储——并且这部分存储**必须**跨越其递归子调用保持存活，该怎么办？（想象在递归 BSP 遍历期间裁剪任意顶点数的多边形，或在归约前收集不定数量的子节点结果。）

可以从调用者传入单个增缩数组 **`T[>..<]&`**，并应用第 6 章中的**平衡调用**模式：

1. 在函数入口处保存 `let mark = scratch.len;`。
2. 将当前激活自身的工作元素压入 `scratch`。
3. 取出覆盖当前激活区段的切片 `let mine = scratch[mark..];`。
4. 发起递归调用——每个子调用都会在 `mine` *之上*压入数据，并在返回前通过 `scratch.resize(child_mark)` 恢复长度！
5. 递归返回后安全地读取 `mine`，最后以 `scratch.resize(mark);` 收尾！

```goose
// Each depth level pushes `depth + 1` values onto `scratch`, keeps a slice `mine`
// alive across its recursive child call, reads `mine` afterward, and restores `mark`.
recursive fn nested_work(scratch: i64[>..<]&, depth: i64) -> i64 {
    let mark = scratch.len;
    for k in depth + 1 {
        scratch.push(depth * 10 + k);
    }
    let mine = scratch[mark..];      // stays valid across the balanced recursive call!

    var sub = 0;
    if depth > 0 {
        sub = nested_work(scratch, depth - 1);
    }

    var local_sum = 0;
    for v in mine { local_sum += v; }

    scratch.resize(mark);            // balanced: caller's slice below `mark` survives!
    return local_sum + sub;
}

fn main() {
    var scratch: i64[>..<] = [];
    let total = nested_work(scratch, 3);
    print("total=", total, " scratch.len=", scratch.len);
}
```

```
total=210 scratch.len=0
```

每一次递归激活都在完美的后进先出（LIFO）协同下共享**同一个**数据栈，每层的元素数量没有固定上限，同时保持零堆分配！

---

## 11.5 模式四：在驱动函数内嵌套递归函数

在编写递归下降解析器、解释器或编译器遍（pass）时，你通常会用到若干张表（Token 数组、AST 节点池、游标索引、源码文本）。

与其声明一个庞大的 `ParserState` 结构体并将其传递给 15 个递归函数，不如把这些表声明为**一个非递归驱动函数的局部变量**，并把递归语法函数直接嵌套在它里面！

- 驱动函数拥有所有可增长数组（`tokens`、`nodes` 等）。
- 每个嵌套的递归函数都将这些局部变量作为自由变量直接访问——而它们的根位于递归环*外部*！
- 遇到任何深层语法错误时，只需执行 `return ..., str(...) from driver;`（第 10 章），即可将所有递归解析栈帧一路展开并直接返回到驱动函数！

---

## 11.6 综合示例：递归下降表达式求值器

让我们使用模式四构建一个简洁、自包含的算术计算器（在 `evaluate` 内嵌套相互递归的函数、使用固定模式的 `Token[>..]`、支持运算符优先级 `+`/`-`、`*`/`/`、一元 `-`、括号 `(...)`，并使用跨帧返回 `return ... from evaluate` 处理语法和运行时错误）：

```goose
import std;

enum Token {
    Num { v: f64 },
    Op  { c: u8 },
    End,
}

fn evaluate(src: const u8[:]) -> f64, u8[] {
    var tokens: Token[>..] = [];     // owned outside the recursive cycle!
    var pos = 0;

    fn tokenize() {
        var i = 0;
        while i < src.len {
            let c = src[i];
            if c == ' ' || c == '\t' { i++; continue; }
            if ascii::is_digit(c) || c == '.' {
                let start = i;
                while i < src.len && (ascii::is_digit(src[i]) || src[i] == '.') { i++; }
                let v, ok = parse_flt(src[start..i]);
                guard ok else {
                    return 0.0, str("bad number '", src[start..i], "'") from evaluate;
                }
                tokens.push(Token.Num { v: v });
                continue;
            }
            guard find("+-*/()", src[i..i + 1]) >= 0 else {
                return 0.0, str("unexpected '", src[i..i + 1], "'") from evaluate;
            }
            tokens.push(Token.Op { c: c });
            i++;
        }
        tokens.push(Token.End);
    }

    fn peek_op(c: u8) -> bool {
        match tokens[pos] { Op o => o.c == c, _ => false }
    }

    // Grammar:
    //   expr   := term (('+' | '-') term)*
    //   term   := unary (('*' | '/') unary)*
    //   unary  := '-' unary | atom
    //   atom   := Num | '(' expr ')'
    recursive fn parse_expr() -> f64 {
        var v = parse_term();
        loop {
            if peek_op('+')      { pos++; v += parse_term(); }
            else if peek_op('-') { pos++; v -= parse_term(); }
            else                 { break; }
        }
        return v;
    }

    fn parse_term() -> f64 {
        var v = parse_unary();
        loop {
            if peek_op('*') { pos++; v *= parse_unary(); }
            else if peek_op('/') {
                pos++;
                let d = parse_unary();
                guard d != 0.0 else { return 0.0, "division by zero" from evaluate; }
                v /= d;
            } else { break; }
        }
        return v;
    }

    recursive fn parse_unary() -> f64 {
        if peek_op('-') { pos++; return -parse_unary(); }
        return parse_atom();
    }

    fn parse_atom() -> f64 {
        match tokens[pos] {
            Num n => { pos++; return n.v; },
            Op o  => {
                guard o.c == '(' else {
                    return 0.0, "expected number or '('" from evaluate;
                }
                pos++;
                let v = parse_expr();
                guard peek_op(')') else {
                    return 0.0, "missing closing ')'" from evaluate;
                }
                pos++;
                return v;
            },
            End => { return 0.0, "unexpected end of input" from evaluate; },
        }
    }

    tokenize();
    let result = parse_expr();
    match tokens[pos] {
        End => { return result, ""; },
        _   => { return 0.0, "trailing tokens after expression" from evaluate; },
    }
}

fn main() {
    let cases: const u8[:][6] = [
        "1 + 2 * 3",
        "(1 + 2) * (10 - 4) / 2",
        "-3 * -(2 + 5)",
        "10 / (3 - 3)",
        "(1 + 2",
        "4 + @",
    ];

    for expr in cases {
        let val, err = evaluate(expr);
        if err.len > 0 { print(expr, "  => ERROR: ", err); }
        else           { print(expr, "  => ", val); }
    }
}
```

```
1 + 2 * 3  => 7.0
(1 + 2) * (10 - 4) / 2  => 9.0
-3 * -(2 + 5)  => 21.0
10 / (3 - 3)  => ERROR: division by zero
(1 + 2  => ERROR: missing closing ')'
4 + @  => ERROR: unexpected '@'
```

点击这个计算器示例上的 **Stacks**！每一条嵌套的递归规则（`parse_expr`、`parse_term`、`parse_unary`、`parse_atom`）自身使用的数据栈数量都是**零**——所有 Token 存储都位于 `evaluate` 单独的 `tokens` 数组中，并在 `evaluate` 返回的瞬间通过一次指针写入彻底释放。

---

## 练习

1. **为计算器添加乘方运算（`^`）：** 扩展 11.6 节的代码以识别 `'^'`（右结合，优先级高于 `*`/`/` 且低于一元 `-`，使用 `import math;` 和 `pow(base, exp)`）。验证 `2 ^ 3 ^ 2` 的求值结果为 `512.0`。
2. **尝试编写平衡递归的排列或子集生成器：** 使用模式三（带 `mark` / `resize(mark)` 的 `scratch: i64[>..<]&`），编写一个 `recursive fn`，计算 `[1, 2, 4]` 所有子集的和，并将结果压入调用者拥有的 `out: i64[>..]&`。
3. **揪出跨越递归的隐藏临时值：** 在 11.2 节的 `main()` 中，尝试将 `let total = count_nodes(root); print("total nodes: ", total);` 替换为单行代码 `print("total nodes: ", count_nodes(root));`。观察 `print` 是接受它还是因存在存活临时值而拒绝——并测试 `print_tree` 直接调用 `print(str(...))` 与在 `{ ... }` 中调用之间的区别。

---

## 本章要点

- 用 **`recursive fn`** 显式标记递归环，并为环中的函数写出完整签名。
- **核心规则：** 切勿让非固定大小的局部变量或临时值跨越递归调用保持存活——递归仅使用原生调用栈，绝不动态增加数据栈的数量。
- 掌握四种简洁的惯用模式：
  1. **块作用域临时缓冲区**（`{ var buf: u8[>..] = []; ... }` 在递归前闭合）。
  2. **调用者拥有的缓冲区/切片**（`merge_sort(xs, tmp)`）。
  3. **平衡调用的 `T[>..<]` 临时栈**（`let mark = s.len; ... s.resize(mark);`）。
  4. **在非递归驱动函数内嵌套递归函数**，共享驱动函数的局部变量并配合跨帧返回 `return ... from`。

在**第 12 章：相对引用与无指针数据结构**中，我们将把注意力转向使用 **1 字节、2 字节和 4 字节相对偏移量**构建链表、二叉搜索树、目录树和 AST——这些数据结构比传统指针图小得多，并且完全位置无关！
