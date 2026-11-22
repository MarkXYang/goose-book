# 附录 B：面向 C++ 与 Rust 程序员的 Goose 指南

如果你已经熟悉 C++ 或 Rust，那么你已经掌握了 Goose 90% 的表层语法——你最大的风险反而在于沿用那些原本用于绕开堆分配器或借用检查器的编程习惯，而 Goose 根本不存在这些机制。本附录为你提供一份直观的心智模型对照指南，并重点指出第一天就值得更新的五个习惯。

---

## 1. 直接对照表

| 概念 | 惯用 C++ | 惯用 Rust | 惯用 Goose |
|---|---|---|---|
| 动态数组 | `std::vector<T>` | `Vec<T>` | `T[>..]`（只增数组，地址稳定）或 `T[>..<]`（LIFO 栈） |
| 拥有所有权的字符串 | `std::string` | `String` | `u8[>..]`（构建器）或内联 `u8[varint]` / `u8[]` / `u8[..k]` |
| 借用字符串 / 切片 | `std::string_view` / `std::span<T>` | `&str` / `&[T]` / `&mut [T]` | `const u8[:]` / `const T[:]` / `T[:]` |
| 标签联合体 / 和类型 | `std::variant<A, B>` 或虚基类 | `enum E { A, B }` | 固定模式 `E`（可索引，按联合体大小对齐）或变长模式 `E..`（精确变体大小！） |
| 可空指针 / 引用 | `T*`（无检查） | `Option<&T>` / `Option<&mut T>` | `T?` / `const T?`（通过 `if`/`while`/`guard`/`assert` 实现流敏感收窄） |
| 生命周期标注 | 无（依赖程序员自律） | `'a` + `&` / `&mut` 独占性 | 无（通过单态化根溯源自动推断；允许别名） |
| 资源清理 | RAII 析构函数（`~T()`） | `Drop` trait | 离开作用域时以 $O(1)$ 释放内存；非内存资源通过 `defer` 清理 |
| 错误传播 | 异常或 `std::expected` | `Result<T, E>` + `?` | 尾部 `bool`（局部）、`T?`（查找）或 `return E from f`（深层跨帧返回） |
| 图 / AST / 区域分配器 | `std::deque` / 自定义 arena + 指针 | `Vec<Node>` + `u32` 索引 / `slotmap` | `T[>..]` + `T&<u32>?` 相对链接，或 `slot_pool<T>` + `u32 in pool` |
| 消除越界检查 | 无检查 `operator[]`（未定义行为） | 迭代器组合子 / `unsafe get_unchecked` | 越界检查消除（BCE）+ 索引类型（`u32 in a`）+ 编译器验证的 `checkfree fn` |
| 泛型与 Lambda | 模板 + Lambda | Trait + `impl Fn` | 无类型/泛型参数（`fn f<T, F>`）+ 编译期尾随代码块 `{ it }` |
| 并发 | `std::thread` + `std::mutex` / 原子操作 | `std::thread` + `Arc<Mutex<T>>` / 通道 | 无共享并发 `thread_fn` + 通过类型化队列（`qput`/`qget`）传递扁平消息 |
| 序列化 | Protobuf / FlatBuffers / 自定义 | `serde` / `rkyv` | 直接在活动数组上执行 `bytes_of` / `to_bytes` / 受验证的 `from_bytes<A>` |

---

## 2. 五大习惯转变（附可运行 Goose 示例）

### 转变 1：在扩容过程中保留带类型的引用（无需 `reserve`，无需索引变通）

在 C++ 中，向 `std::vector` 压入元素会使所有指针和迭代器失效，除非你提前 `reserve` 一个硬性上界。在安全 Rust 中，压入 `Vec` 的同时持有指向其内部的 `&mut` 引用是被禁止的，这迫使你退而使用原始整数索引。

在 Goose 中，`T[>..]` **永不重定位**，因此 `.push(...)` 返回的活动引用在整个作用域内始终有效：

```goose
struct Task { id: i32, priority: i32 }

fn main() {
    var tasks: Task[>..] = [];
    let first .= tasks.push(Task { id: 1, priority: 10 });
    for i: i32 in 2..5000 {
        tasks.push(Task { id: i, priority: 1 });
    }
    first.priority = 99; // Still valid 4,999 pushes later!
    print("tasks=", tasks.len, " first.priority=", tasks[0].priority);
}
```

```text
tasks=4999 first.priority=99
```

### 转变 2：优先使用变长模式 ADT（`E..`），而非指针间接多态

在 C++ 需要分配 `std::vector<std::unique_ptr<Expr>>`、而 Rust 会将每个 `enum Expr` 填充至最大变体大小的场景中，Goose 可以通过 `E..[>..]` 将异构变体——包括内联字符串——紧凑地打包进同一个缓冲区：

```goose
enum LogEntry {
    Heartbeat { tick: u32 },
    Metric { tick: u32, value: f64 },
    Alert { tick: u32, msg: u8[varint] },
}

fn main() {
    var log: LogEntry..[>..] = [];
    log.push(LogEntry.Heartbeat { tick: 1 });
    log.push(LogEntry.Metric { tick: 2, value: 98.6 });
    log.push(LogEntry.Alert { tick: 3, msg: "disk 95%" });

    var alerts = 0;
    for entry in log {
        match entry {
            Alert &a => {
                alerts += 1;
                print("alert@", a.tick, ": ", a.msg);
            },
            _ => {},
        }
    }
    print("total bytes=", bytes_of(log).len, " alerts=", alerts);
}
```

```text
alert@3: disk 95%
total bytes=32 alerts=1
```

### 转变 3：用 `return … from` 替代 `Result`/`?` 链与异常

你不必把每个内部辅助函数的返回类型都包装成 `Result<T, Err>`，也无需为异常栈展开表付出代价。让内部辅助函数直接返回干净的值类型（`i64`、`Node&`），在出错时直接跨帧返回至入口函数即可：

```goose
import std;

fn parse_port(text: const u8[:]) -> i64, u8[>..] {
    fn read_num(s: const u8[:]) -> i64 {
        let v, ok = parse_int(s);
        guard ok else { return 0, str("not an integer: '", s, "'") from parse_port; }
        return v;
    }

    let p = read_num(text);
    guard p >= 1 && p <= 65535 else {
        return 0, str("port out of range: ", p) from parse_port;
    }
    return p, str("");
}

fn main() {
    let p1, e1 = parse_port("8080");
    let p2, e2 = parse_port("99999");
    print("ok: ", p1, " (err='", e1, "')");
    print("bad: ", p2, " (err='", e2, "')");
}
```

```text
ok: 8080 (err='')
bad: 0 (err='port out of range: 99999')
```

### 转变 4：用 `slot_pool` + 索引类型处理非嵌套生命周期

当 C++ 使用 `new`/`delete` 或 Rust 求助于 `slotmap`/`slab` + `usize` 时，使用 Goose 的 `slot_pool<T>`（槽位池）即可实现 $O(1)$ 存储槽位复用；当你希望在热循环中彻底消除越界检查且不使用 `unsafe` 时，搭配 `u32 in pool.items` 即可。

### 转变 5：用数据栈配合 `defer` 替代 RAII / `Drop`

在 Goose 中，你永远不需要为释放内存编写析构函数——离开作用域时，单次写入即可重置其数据栈。对于外部资源（文件、GPU 上下文、UI 上下文、音频设备、C 句柄），只需在获取资源后紧接着写下 `defer cleanup();`，清理代码就会在每条退出路径（包括 `return … from`）上严格按 LIFO 逆序执行。

---

## 3. 需要留意的肌肉记忆陷阱

1. **`=` 通过引用写入目标值；`.=` 绑定或重新绑定引用本身。**
   在 C++ 中，引用无法重新绑定；在 Rust 中，`*r = val` 与 `r = &other` 借助 `*` 明确区分了两者。而在 Goose 中，引用是透明引用，因此 `cur = next` 会将 `next` 的*值*复制到 `cur` 指向的目标中——要移动引用本身，请使用 `cur .= next`！
2. **`let` 并不等同于 `const`！**
   在 Rust 中，除非写成 `let mut x`，否则 `let x = vec![1, 2]` 会使 `x` 深度不可变。而在 Goose 中，`let xs = [1, 2]` 仅禁止对 `xs` 进行整体重新赋值（`xs = ...`），但仍允许修改其中的元素（`xs[0] = 9`）。需要只读内容时，请使用 `const`（`const xs = ...` 或 `const u8[:]`）。
3. **窄整数不会自动提升为 `int`！**
   在 C/C++ 中，`u8 + u8` 会自动提升为 `int`。而在 Goose 中，`u8 + u8` 仍保持为 `u8`，并按模 256 回绕。在对字节求和之前，请显式加宽类型（`var sum: u16 = a; sum += b;`）。
4. **非固定大小的值不存在隐式移动。**
   新生成的结果会通过目标处直接构造就地创建（`let xs = make()`），零拷贝开销。将已有的非固定大小变量赋值给另一个变量时，必须显式写出 `copy(xs)`，从而让每一次 $O(n)$ 拷贝都在源码中清晰可见。
5. **没有 `unsafe` 块——请改用 `checkfree` + 索引类型。**
   在 Rust 程序员不得不使用 `unsafe { slice.get_unchecked(i) }` 来消除热图或矩阵运算中越界检查的地方，Goose 提供了由编译器校验的 `u32 in arr` 索引类型和无检查函数（`checkfree fn`），让你在保持 100% 内存安全的同时获得同样零检查的汇编代码。
