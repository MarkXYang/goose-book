# 附录 C：编译器诊断信息实战指南

Goose 会在编译期拒绝悬垂指针、不安全的缩容操作、非法的递归栈、隐藏的复制操作，以及无检查函数（`checkfree fn`）中残留的越界检查。由于诊断信息围绕 **根**、**存储** 和 **边界** 等概念展开，将出错模式与简洁的修复方案并排对照，能让每一条错误信息都一目了然、立刻可操作。

下文中的每一段错误示例代码都经过验证会编译失败（`compile-fail`），而每一段修复代码都经过验证能够零警告编译并运行。

---

## 1. 返回指向局部变量的引用

**症状：** 试图返回一个引用或切片，而它指向函数内部声明的数组。

<!-- compile-fail -->
```goose
fn make_greeting() -> const u8[:] {
    let buf = str("hello");
    return buf[..]; // error: slice points into local `buf`, which ends on return
}

fn main() {
    print(make_greeting());
}
```

**原因：** `buf` 位于 `make_greeting` 的数据栈上，该函数一返回，其数据栈便会重置。返回指向它的视图会导致悬垂引用。

**修复：** 直接按值返回拥有所有权的 `u8[>..]`——Goose 会在调用方的栈上直接构造它，零复制开销！

```goose
fn make_greeting() -> u8[>..] {
    return str("hello"); // built straight at the caller's destination
}

fn main() {
    print(make_greeting());
}
```

```text
hello
```

---

## 2. 在视图仍处于活跃状态时缩容数组

**症状：** 在指向某个 `T[>..]` 的引用或切片稍后仍会被使用的情况下，对该数组调用 `pop`、`clear`、`resize` 或整数组赋值。

<!-- compile-fail -->
```goose
fn main() {
    var xs: i64[>..] = [10, 20, 30];
    let first .= xs[0];
    xs.clear();          // error: `first` is still used after `xs` shrinks
    print(first);
}
```

**原因：** 缩容会回收数据栈上的元素，从而使 `first` 失效。

**修复：** 要么在调用 `clear()` *之前* 完成对 `first` 的使用，要么在缩容前复制该固定大小的值（使用 `let first = xs[0];`，不带 `.=`）：

```goose
fn main() {
    var xs: i64[>..] = [10, 20, 30];
    let first = xs[0];   // copies the i64 value (not a reference)
    xs.clear();          // safe: no live views into `xs`
    print("saved=", first, " len=", xs.len);
}
```

```text
saved=10 len=0
```

---

## 3. 在向数组构造元素的同时对数组扩容

**症状：** 调用 `xs.push(f(xs))`，其中 `xs` 保存着变长元素（或相对引用链接），同时 `f` 也会向 `xs` 追加元素。

<!-- compile-fail -->
```goose
struct Node { next: Node&<u32>?, val: i32 }

fn push_child(pool: Node[>..]&) -> Node& {
    return pool.push(Node { next: null, val: 1 });
}

fn main() {
    var pool: Node[>..] = [];
    // error: `push_child` grows `pool` while `pool.push(...)` is building a Node in place
    pool.push(Node { next: push_child(pool), val: 2 });
}
```

**原因：** 包含相对引用链接或变长的元素会直接在 `pool` 的数据栈顶原地构造。如果在字段初始化表达式中扩容 `pool`，就会覆盖尚未构造完成的元素。

**修复：** 在调用 `pool.push(...)` *之前*，先求值 `push_child(pool)` 并将其保存到引用中：

```goose
struct Node { next: Node&<u32>?, val: i32 }

fn push_child(pool: Node[>..]&) -> Node& {
    return pool.push(Node { next: null, val: 1 });
}

fn main() {
    var pool: Node[>..] = [];
    let child .= push_child(pool);
    let parent .= pool.push(Node { next: child, val: 2 });
    let next = parent.next;
    if next {
        print("parent=", parent.val, " child=", next.val);
    }
}
```

```text
parent=2 child=1
```

---

## 4. 在递归调用期间持有可增长的局部变量

**症状：** 某个 `recursive fn` 声明了一个 `T[>..]`（或调用了 `str(...)`），且该局部变量在跨越递归调用后依然处于活跃状态。

<!-- compile-fail -->
```goose
recursive fn countdown(n: i64) {
    if n == 0 { return; }
    var tag = str("level ", n); // error: growable local alive across recursive call
    countdown(n - 1);
    print(tag);
}

fn main() { countdown(3); }
```

**原因：** Goose 在编译期分配的数据栈数量是固定的；无界的递归调用链不可能让每一层都独占一个新的活跃数据栈。

**修复：** 要么在递归*之前*结束临时缓冲区的作用域（块级作用域临时缓冲区），要么通过引用传入由调用方拥有的缓冲区：

```goose
recursive fn countdown(n: i64) {
    if n == 0 { return; }
    {
        let tag = str("level ", n);
        print(tag);
    }                    // `tag`'s data stack resets before recursing!
    countdown(n - 1);
}

fn main() { countdown(3); }
```

```text
level 3
level 2
level 1
```

---

## 5. 修改只读（`const`）切片或字符串字面量

**症状：** 向接收 `u8[:]` 并对其进行修改的辅助函数传入字符串字面量 `"banana"` 或 `const` 变量时，触发可写性错误。

<!-- compile-fail -->
```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 { s[0] = 'B'; }
}

fn main() {
    capitalize_first("banana"); // error: cannot write through read-only string literal
}
```

**原因：** 字符串字面量位于只读静态内存中（`const u8[:]`）。虽然当接收 `u8[:]` 的辅助函数从未写入 `s` 时，Goose 允许它接受字符串字面量，但任何通过 `s` 进行的写操作（或将 `s` 存入可写存储槽位）都需要可写的底层存储。

**修复：** 在原地修改之前先具象化一个可变缓冲区（例如 `str("banana")`），并将只读参数标记为 `const u8[:]`，以便在函数签名中明确表达意图：

```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 { s[0] = 'B'; }
}

fn main() {
    var word = str("banana");
    capitalize_first(word);
    print(word);
}
```

```text
Banana
```

---

## 6. 无检查函数（`checkfree fn`）保留了越界检查

**症状：** 某个 `checkfree fn` 编译失败，因为越界检查消除（BCE）无法证明从数组中读取的索引（或未受约束的参数）一定在边界内。

<!-- compile-fail -->
```goose
checkfree fn sum_indexed(vals: const i64[:], idxs: const u32[:]) -> i64 {
    var total = 0;
    for k in idxs {
        total += vals[k]; // error: BCE does not track arbitrary values stored in `idxs`
    }
    return total;
}

fn main() {
    let vals: i64[3] = [10, 20, 30];
    let idxs: u32[2] = [0, 2];
    print(sum_indexed(vals, idxs));
}
```

**原因：** BCE 只跟踪整型变量和数组长度，不会跟踪存储在另一个数组内部的任意整数。

**修复：** 在 `idxs` 中存储经过校验的索引类型（`u32 in vals`），将校验前移到构造阶段；或者对由长度推导出的索引使用 `assert` 前置条件：

```goose
var vals: i64[>..] = [10, 20, 30];

checkfree fn sum_indexed(idxs: const (u32 in vals)[:]) -> i64 {
    var total = 0;
    for k in idxs {
        total += vals[k]; // guaranteed in bounds by `u32 in vals`!
    }
    return total;
}

fn main() {
    var idxs: (u32 in vals)[>..] = [];
    idxs.push(0 as (u32 in vals));
    idxs.push(2 as (u32 in vals));
    print(sum_indexed(idxs));
}
```

```text
40
```

---

## 7. 隐式复制非固定大小的值或将非扁平结构体入队

**症状 A（缺少 `copy`）：** 按值赋值或传递一个已有的 `T[>..]` 时，必须显式调用 `copy(x)`，从而确保 $O(n)$ 复制永远不会悄无声息地发生。

**症状 B（非扁平线程消息）：** 向 `qput` 或 `thread_spawn` 传递包含切片（`const u8[:]`）或引用（`Node&`）的结构体会编译失败，因为工作线程之间不共享任何内存。

<!-- compile-fail -->
```goose
struct BadJob { label: const u8[:] }

fn main() {
    qput(BadJob { label: "task" }); // error: BadJob is not flat (contains a slice)
}
```

**修复：** 将切片字段替换为拥有所有权的变长数组（`u8[]`），使整条消息能作为一整块扁平字节块跨越队列传递：

```goose
struct GoodJob { label: u8[] }

fn main() {
    let j = GoodJob { label: "task" };
    print("flat job ready: ", j.label);
}
```

```text
flat job ready: task
```

---

## 8. 常见编译器警告（及快速修复）

| 警告 | 原因 | 快速修复 |
|---|---|---|
| `redundant 'as T': an X converts to T implicitly here` | 在 Goose 本就会隐式拓宽的位置使用了拓宽类型转换（如 `u8 as u16`、`i64 as f64`） | 移除多余的类型转换，或改写为 `var acc: u16 = a;` |
| `redundant &: elements of this type bind by reference without it` | 遍历非固定大小的元素类型（变长模式枚举、`u8[][]` 等）时写了 `for &n in xs` | 改写为 `for n in xs`——非固定大小的元素会自动按引用绑定 |
| `unused variable` / `unused binding` | 局部变量或模式匹配载荷绑定从未被读取 | 添加前缀或替换为 `_`（例如 `let _, ok = ...;`） |
| `reusable` / `reusable[]` 弃用警告 | 旧版内置对象池修饰符 | 迁移至 `import slot_pool;`（`slot_pool<T>`）或 `import slice_pool;`（`slice_pool<T>`） |
