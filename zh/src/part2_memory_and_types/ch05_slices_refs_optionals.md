# 第 5 章：切片、引用与可选引用

在第一部分中，你已经看到 Goose 如何将值内联存储在数据栈上，从而使内存在增长过程中永不移动。现在进入**第二部分**，我们将探讨代码如何安全地*指向*这些内存——无需原始指针算术、无需满屏的 `*` 和 `->` 符号、无需担心空指针解引用，也无需生命周期标注。

Goose 提供了三种核心间接访问类型，每一种都是固定大小、零开销的值：

- **`T[:]`** —— **切片**（指向任意数组中一段连续元素的视图）。
- **`T&`** —— **引用**（单个非空机器地址，在表达式中完全透明）。
- **`T?`** —— **可选引用**（可为空的引用，使用前必须进行类型收窄，且零额外空间开销）。

读完本章后，你将能够：

- 使用半开区间与末端相对区间（`^k`），基于 `T[:]` 切片编写通用的数组处理函数。
- 使用 `.=` 和 `.==` 绑定、重绑定以及比较透明引用 `T&`，避免意外拷贝。
- 使用 `if`、`while`、`guard` 和 `assert` 清晰地返回并收窄可空引用 `T?`。
- 使用 `const T&` 和 `const T[:]` 控制只读与可写访问权限。

---

## 5.1 切片（`T[:]`）：通用的零拷贝视图

切片 `T[:]` 是一个固定大小的二元组：一个指向首元素的指针，加上一个 `i64` 类型的元素数量。由于切片不拷贝任何元素，`T[:]` 是 Goose 中所有就地读取或修改数组元素的函数的通用参数类型。

```text
grow: i64[>..]  on a data stack:
  index:   0    1    2    3    4
         +----+----+----+----+----+
         | 10 | 20 | 30 | 40 | 50 |
         +----+----+----+----+----+
               ^              ^
               |              |
  s = grow[1..4]:  { ptr -> elem 1, len = 3 }
  tail = grow[^2..]:              { ptr -> elem 3, len = 2 }
```

### 切片语法与隐式转换

- `a[x..y]` 表示从索引 `x` 到 `y`（不含 `y`）的元素视图。
- 省略下界表示从 `0` 开始（`a[..y]`），省略上界表示直到 `a.len`（`a[x..]`），两端都省略（`a[..]`）则表示整个数组。
- `^k` 表示从末尾倒数（即 `len - k`）：`a[^2..]` 表示最后两个元素，`a[..^1]` 则去掉最后一个元素。
- **自动隐式转换：** 只要*任意*种类的数组（`T[k]`、`T[]`、`T[..k]`、`T[..]`、`T[>..]`、`T[>..<]`）流向类型为 `T[:]` 的目标位置（函数参数、带类型标注的变量、结构体字段或返回值），它都会自动隐式转换为完整切片——无需显式写出 `&` 或 `[..]`。
- **增长稳定性：** 在对 `T[>..]` 调用 `.push(...)` 之前获取的切片，在增长之后依然完全有效；它的 `.len` 仍保持创建时的长度。

```goose
fn sum(xs: const i64[:]) -> i64 {
    var total = 0;
    for x in xs { total += x; }
    return total;
}

fn main() {
    let fixed: i64[4] = [1, 2, 3, 4];
    var grow: i64[>..] = [10, 20, 30, 40, 50];

    let mid = grow[1..4];            // [20, 30, 40]
    let last_two = grow[^2..];       // [40, 50]
    let trim_ends = grow[1..^1];     // [20, 30, 40]

    // `sum` accepts every array kind and any slice without copying:
    print("fixed=", sum(fixed), " grow=", sum(grow), " mid=", sum(mid),
          " last_two=", sum(last_two), " trim=", trim_ends);

    // Growing `grow` does not invalidate `mid` or change its length:
    grow.push(60);
    print("mid after push=", mid, " grow.len=", grow.len);
}
```

```
fixed=10 grow=150 mid=90 last_two=90 trim=[20, 30, 40]
mid after push=[20, 30, 40] grow.len=6
```

与数组一样，元素为固定大小的切片同时支持索引访问（`s[i]`，会针对 `s.len` 进行越界检查）和迭代遍历，而元素为变长类型的切片（例如 `u8[][:]`）仅支持迭代遍历。

---

## 5.2 透明引用（`T&`）、`.=` 与 `.==`

引用 `T&` 是一个 8 字节的机器地址，并提供三项保证：**永不为空**、**不支持指针算术或非安全强制转换**，以及**不会悬垂**。

### 透明性

在 Goose 中，引用是**透明的**：只要你在普通表达式中使用引用，它的行为就等同于它所指向的值本身。语言中没有 `*` 解引用运算符，也没有 `->` 箭头运算符：

- `r + 1` 读取目标值并加 `1`。
- `r.field` 访问目标值的字段。
- `r = 99` 和 `r += 5` 会直接**穿透写入**目标值。
- `r1 == r2` 比较的是**目标值**是否相等。
- `print(r)` 打印的是目标值。

正因为 `=` 和 `==` 始终作用于*目标值*，当你需要操作**引用本身**时，Goose 提供了两个简洁且毫无歧义的运算符：

| 运算符 | 含义 | 示例 |
|---|---|---|
| `=` | 拷贝或赋值**目标值** | `r = 11;`（修改 `r` 所指向的元素） |
| `.=` | 将**引用绑定或重绑定**到目标 | `var r .= xs[0];` 随后 `r .= xs[2];` |
| `==` / `!=` | 比较**目标值**是否相等 | `r == xs[2]`（值相同时为 `true`） |
| `.==` / `.!=` | 比较**引用同一性**（是否指向同一地址） | `r .== xs[2]`（仅当指向同一元素时为 `true`） |

```goose
fn main() {
    var xs: i64[>..] = [10, 20, 10];
    var r .= xs[0];                  // `r` refers to xs[0]
    var c = xs[0];                   // `c` is a fixed-size copy of 10

    r = 11;                          // writes xs[0]
    c = 99;                          // writes only the local copy `c`
    print("after write: xs=", xs, " c=", c);

    // Value equality vs. identity equality:
    xs[2] = 11;
    print("r == xs[2]: ", r == xs[2], "  r .== xs[2]: ", r .== xs[2]);

    // Rebind `r` to point at xs[2], then mutate through it:
    r .= xs[2];
    r += 100;
    print("after rebind: xs=", xs, " r .== xs[2]: ", r .== xs[2]);
}
```

```
after write: xs=[11, 20, 10] c=99
r == xs[2]: true  r .== xs[2]: false
after rebind: xs=[11, 20, 111] r .== xs[2]: true
```

### 绑定、隐式 `&` 与退化规则

以下两条简明规则让引用代码保持干练、免于冗余噪音：

1. **当目标类型已经是 `T&` 时无需多余的 `&`：** 在调用 `fn inc(n: i64&)`、初始化带标注的 `let r: i64& = count;`，或填充结构体字面量中的引用字段时，直接传入左值而不必加 `&`（例如 `inc(count)`）。写 `inc(&count)` 虽然也能工作，但 Goose 会发出 `&` 多余的警告。
2. **类型推导变量会将固定大小引用退化为拷贝：** 由于引用是透明的，对引用 `r: i64&` 写 `let d = r;` 会创建一份 `i64` 的*拷贝*。若要通过类型推导绑定一个新引用，应使用 `.=`（`let a .= r;`）或显式 `&`（`let a = &n;`）。非固定大小的左值（如 `u8[>..]`、`Order`）绝不会隐式拷贝，因此 `let alias = xs;` 始终按引用绑定。

```goose
fn inc(n: i64&) { n += 1; }

struct CounterRef { target: i64& }

fn main() {
    var count = 0;
    inc(count);                      // binds by reference without `&`
    let ref_a: i64& = count;         // annotated reference
    let ref_b .= count;              // inferred reference via `.=`
    let ref_c = &count;              // inferred reference via `&`
    let snapshot = ref_a;            // fixed-size reference decays to a copy!

    let holder = CounterRef { target: count };
    ref_a += 10;
    ref_b += 100;
    ref_c += 1000;
    holder.target += 10_000;

    print("count=", count, " snapshot=", snapshot);
}
```

```
count=11111 snapshot=1
```

> **注意：** 对临时表达式取 `&`（例如 `&(a + b)`）会导致编译错误，且语言中不存在引用的引用（`&r` 只会得到其中存储的引用）。另请记住：`let r .= xs[0];` 仅禁止用 `.=` *重绑定* `r`，除非 `r` 带有 `const`，否则 `r = 5;` 仍然可以修改 `xs[0]`。

---

## 5.3 可选引用（`T?`）与流敏感收窄

既然 `T&` 永不为空，那么该如何表示“可能存在某个元素”（例如查找结果、字典查询结果，或树节点的左子节点）呢？

答案是 **`T?`**——**可选引用**。在底层实现上，`T?` 本质上仍是一个 8 字节引用，仅用地址 `0` 表示 `null`，因此可空性带来**零额外字节开销**。写作 `T&?` 与 `T?` 表示完全相同的类型；另外，结构体字面量中省略的任何可选引用字段都会自动默认为 `null`。

### 使用前必须收窄

与 `T&` 不同，可选引用 `T?` **不是透明的**。你不能直接通过 `T?` 读取字段、执行算术运算或进行赋值——这样做总是会引发编译错误：

<!-- compile-fail -->
```goose
fn main() {
    var x = 10;
    let opt: i64? = x;
    print(opt + 1);                  // error: optional must be narrowed before use
}
```

要使用可选引用，你需要通过以下四种内置方式之一对其进行**类型收窄（流敏感收窄）**。在收窄后的作用域内，编译器会将该变量视为普通的非空引用 `T&`：

1. **`if opt { ... }`** —— 在 `then` 分支内完成收窄（对于 `if opt == null { ... } else { ... }`，则在 `else` 分支内完成收窄）。
2. **`guard opt else { return ...; }`** —— 在所在代码块的整个剩余部分内都保持收窄状态！
3. **`while cur { ... cur .= cur.next; }`** —— 在循环体内完成收窄，且每次迭代前都会重新检查。
4. **`assert(opt);`** —— 若为空则中止程序，在所在代码块的剩余部分内保持收窄状态。

```goose
fn find_over(xs: i64[:], threshold: i64) -> i64? {
    for &x in xs {
        if x > threshold { return x; }
    }
    return null;
}

fn boost_first_over(xs: i64[:], threshold: i64, bonus: i64) -> bool {
    let hit = find_over(xs, threshold);
    guard hit else { return false; } // narrowed to `i64&` from here down!
    hit += bonus;                    // mutates the found element directly in `xs`
    return true;
}

struct Node { val: i64, next: Node? }

fn main() {
    var nums: i64[>..] = [3, 12, 7, 25];
    let ok1 = boost_first_over(nums, 10, 100);
    let ok2 = boost_first_over(nums, 500, 100);
    print("ok1=", ok1, " ok2=", ok2, " nums=", nums);

    // Walking a linked chain with `while cur`:
    var n3 = Node { val: 30 };       // `next` defaults to `null`
    var n2 = Node { val: 20, next: n3 };
    var n1 = Node { val: 10, next: n2 };

    var cur: Node? = n1;
    var sum = 0;
    while cur {
        sum += cur.val;              // narrowed to `Node&` inside the loop body
        cur .= cur.next;
    }
    print("chain sum=", sum);
}
```

```
ok1=true ok2=false nums=[3, 112, 7, 25]
chain sum=60
```

注意 `boost_first_over` 如何通过 `hit` 直接就地修改 `nums[1]`，全程既没有拷贝元素，也不必计算索引——而且因为 `find_over` 返回的是可空引用 `i64?`，也就不再需要用 `-1` 这样的哨兵魔数来与真实数据混淆。

---

## 5.4 可写性与 `const`

在第 2 章中，你已经了解到 `let` 锁定的是绑定本身，而 `const` 锁定的是内容。对于引用和切片，`const` 控制的是**通过该间接引用进行写入的权限**：

- `const T&` —— 只读引用：你可以读取 `T`，但绝不能修改它。
- `const T[:]` —— 只读切片：你可以读取元素，但绝不能覆写它们。

### 哪些值属于只读？

编译器会为每一个引用和切片静态追踪其可写性：

- **只读：** 字符串字面量（`"hello"` 的类型为 `const u8[:]`）、对 `const` 变量取 `&x` 或 `x[..]`、按值绑定的 `for` 或 `match` 绑定、临时值的视图、`bytes_of(...)`，以及从 `const` 存储槽位中读出的任何内容。
- **可写：** 其他所有情形——包括对 `var` 变量和普通 `let` 变量取 `&x` 和 `x[..]`。

添加 `const` 始终是隐式允许的（你可以将可写的 `u8[:]` 传给 `const u8[:]` 参数）；而去掉 `const` 则**绝不允许**。

### 参数对常量性保持泛型；存储槽位必须显式声明

Goose 在函数处理 `T[:]` 和 `T&` 参数上有一个精妙且极其便利的特性：

- 写作 **`u8[:]`**（不带 `const`）的参数*同时*接受可写与只读实参！编译器会针对每个调用点实参的实际可写性检查函数体：如果函数仅做读取，传入字符串字面量就能编译通过；如果函数执行了写入，传入字符串字面量则会在该调用点报错。
- 写作 **`const u8[:]`** 的参数在函数体内部无论调用方传入什么都是只读的——既作为文档说明，也由编译器强制保证函数不会修改输入。
- **存储槽位**（结构体字段、数组元素、全局变量以及带类型标注的变量）若要保存只读引用或字符串字面量，必须显式声明 `const`。

```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 && s[0] >= 'a' && s[0] <= 'z' {
        s[0] -= 32;
    }
}

struct Tag { label: const u8[:] }    // `const` slot: can hold string literals OR mutable views

fn main() {
    var word: u8[>..] = [];
    word.append("goose");
    capitalize_first(word);          // writable argument: succeeds

    let t1 = Tag { label: "static-tag" };
    let t2 = Tag { label: word };    // writable coerces to `const u8[:]` implicitly
    let chosen = if word.len > 3 { t1.label } else { word[..] }; // read-only (one branch is const)

    print("word=", word, " t1=", t1.label, " t2=", t2.label, " chosen=", chosen);
}
```

```
word=Goose t1=static-tag t2=Goose chosen=static-tag
```

如果你尝试把只读的字符串字面量传给 `capitalize_first`，Goose 会在编译期捕获这次非法写入：

<!-- compile-fail -->
```goose
fn capitalize_first(s: u8[:]) {
    if s.len > 0 && s[0] >= 'a' && s[0] <= 'z' {
        s[0] -= 32;
    }
}

fn main() {
    capitalize_first("immutable");   // error: cannot write through read-only slice
}
```

> **语法说明：** `const` 是一个前缀修饰符，限定基础类型上的*第一层*引用或切片（`const u8[:]` 表示字节的只读切片）。若要将 `const` 应用于包裹切片变量的*外层*引用，请加括号：`const (u8[:])&` 表示指向一个可写切片变量的只读引用。另请注意，`const` 是**浅层的**：从 `const Box&` 中读取一个非 `const` 的引用字段，得到的仍是该字段自身的可写性。

---

## 5.5 综合实战：带反向链表的就地日志索引

现在我们把切片、透明引用（`.=`、`.==`）、可选引用收窄（`guard`、`if`、`while`）以及 `const` 视图结合到一个实用案例中：对多行日志缓冲区建立索引，且**不拷贝哪怕一个字节的文本**。

每条 `Entry` 都直接存储指向原始日志文本的 `const u8[:]` 视图、一个可变的 `priority: i32`，以及一个可选引用 `prev: Entry?`，用来反向链接同一服务的上一条日志条目。由于 `entries` 是只增数组 `Entry[>..]`，这些 `Entry?` 链接在每一次 `push` 之后都会保持有效！

```goose
import std;

struct Entry {
    id: i32,
    service: const u8[:],
    msg: const u8[:],
    priority: i32,
    prev: Entry?,
}

fn find_latest(entries: Entry[:], svc: const u8[:]) -> Entry? {
    // Scan backward to find the most recent entry for `svc`.
    var i = entries.len - 1;
    while i >= 0 {
        if entries[i].service == svc { return entries[i]; }
        i--;
    }
    return null;
}

fn escalate(entries: Entry[:], svc: const u8[:], bump: i32) -> bool {
    let latest = find_latest(entries, svc);
    guard latest else { return false; }

    // Walk all entries for this service through the `prev` chain and boost priority:
    var cur: Entry? = latest;
    while cur {
        cur.priority += bump;
        cur .= cur.prev;
    }
    return true;
}

let RAW_LOG = """
    auth:login ok
    db:pool opened
    auth:token refreshed
    http:200 /health
    auth:login failed
    """;

fn main() {
    var entries: Entry[>..] = [];

    each_split(RAW_LOG, '\n') {
        guard it.len > 0;
        let colon = find(it, ":");
        guard colon > 0;
        let svc = it[..colon];
        let msg = it[colon + 1..];
        let prev = find_latest(entries, svc);
        entries.push(Entry {
            id: entries.len as i32,
            service: svc,
            msg: msg,
            priority: 1,
            prev: prev,
        });
    };

    let changed = escalate(entries, "auth", 5);
    let missing = escalate(entries, "cache", 5);
    print("escalated auth=", changed, " cache=", missing);

    // Inspect the linked chain for "auth" from newest to oldest:
    let head = find_latest(entries, "auth");
    assert(head);                    // narrowed to `Entry&` for the rest of `main`
    print("head index=", entries.index_of(head),
          " is last element? ", head .== entries[^1..][0]);

    var cur: Entry? = head;
    while cur {
        print("  #", cur.id, " [p=", cur.priority, "] ", cur.service, ": ", cur.msg);
        cur .= cur.prev;
    }
}
```

```
escalated auth=true cache=false
head index=4 is last element? true
  #4 [p=6] auth: login failed
  #2 [p=6] auth: token refreshed
  #0 [p=6] auth: login ok
```

看看这段代码在**零堆分配、零字符串拷贝、零生命周期标注**的前提下实现了什么：
- 每个 `service` 和 `msg` 字段都是直接切入 `RAW_LOG` 的 `const u8[:]` 视图。
- 每个 `prev` 字段都是强类型的 `Entry?`，直接指向 `entries` 中更早的元素，并在后续每一次 `entries.push(...)` 中保持有效。
- `escalate` 通过收窄后的可选引用就地修改全部三条 `"auth"` 日志条目，而 `entries.index_of(head)` 直接根据引用反推出 `head` 的精确索引（`4`）！

---

## 练习

1. **体验 `const` 与可写切片的区别：** 在第 5.5 节的综合实战示例中，将 `struct Entry` 的 `msg` 字段从 `const u8[:]` 改为 `u8[:]`。点击 **▶ Run** 并阅读完整的编译器报错，了解为何不能将 `RAW_LOG` 存入非 `const` 存储槽位。
2. **添加查找并计数的辅助函数：** 编写 `fn count_chain(start: Entry?) -> i64`，使用 `while cur` 沿着 `cur .= cur.prev` 遍历链表并返回链表长度（输入 `null` 时返回 `0`）。分别对 `"auth"`、`"db"` 和 `"cache"` 调用它。
3. **对比 `.=` 与 `=`：** 编写一个简短程序，声明两个局部变量 `var a = 1; var b = 2;` 以及 `var r .= a;`。在同一个程序中展示 `r = b;` 与 `r .= b;` 的差异，在每一步之后打印 `a`、`b`、`r` 以及 `r .== b`。

---

## 本章要点

- 切片 `T[:]` 是适用于所有数组种类的通用零拷贝视图，支持半开区间 `a[x..y]` 和末端相对区间 `a[^k..]`。
- 引用 `T&` 是非空、透明的 8 字节地址：用 `=` / `==` 操作目标值，用 `.=` / `.==` 进行引用绑定与同一性比较。
- 可选引用 `T?` 是零开销的可空引用（`null` 即地址 `0`），使用前必须通过 `if`、`while`、`guard` 或 `assert` 进行类型收窄。
- 可写性由 `const` 控制：接受 `T[:]` 的函数会按调用点自适应只读或可写实参，而 `const T[:]` 用于明确约束只读访问；此外，存储槽位必须声明 `const` 才能保存只读数据。

到目前为止，引用和切片一直“自然而然地正常工作”——连一行生命周期标注都不用写。在**第 6 章：无标注的生命周期**中，我们将揭开底层机制，深入讲解**根、候选根、收缩规则与平衡调用**，让你彻底理解编译器究竟在检查什么，并能在几秒内修复任何生命周期或收缩相关的诊断报错。
