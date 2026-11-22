# 第 4 章：数组家族与字符串

在大多数编程语言中，你只会得到一种动态数组（`std::vector<T>`、`Vec<T>`、`[]T`）和一种字符串类型（`std::string`、`String`、`string`），而且它们每一个都存储着指向独立堆分配的指针。如果你把字符串放进结构体，再把结构体放进数组，最终得到的便是一棵散落在堆各处的指针树。

Goose 采取了更简洁的做法：**没有单一的数组类型，也没有专门的字符串类型。** 相反，Goose 为你提供了一套统一的**数组家族**，家族成员之间唯一的区别在于其大小行为——而每个字符串都只是 `u8` 字节的数组（或切片）。选择数组类型，*就是*选择精确的内存布局。

学完本章后，你将能够：

- 为任意数据布局从 Goose 的数组家族（`T[k]`、`T[]`、`T[varint]`、`T[..k]`、`T[..]`、`T[>..]`、`T[>..<]`）中挑选合适的数组类型。
- 熟练使用内置数组操作（`.push`、`.append`、`.pop`、`.resize`、`.clear`、`.move_from`、结构化 `==`），并理解放置规则。
- 以零指针方式内联打包变长字符串与嵌套数组，并游刃有余地使用**顺序**（sequential）数组。
- 使用 `u8` 数组和 `std` 构建、切片、解析、查找、UTF-8 迭代以及自定义 `format` 文本。

---

## 4.1 数组家族一览

Goose 中所有非可调整大小的数组都以 `[元数据][元素...]` 的形式直接内联存储在承载它的容器内部——无论是局部变量、结构体字段，还是另一个数组的元素——**没有任何指向外部缓冲区的指针**。两种可调整大小的数组则将元素存放在数据栈（第 3 章）顶部，仅在所属栈帧中保存其长度。

| 类型 | 大小类别 | 行为 | 存储元数据 |
|---|---|---|---|
| `T[k]` | 固定大小 | 精确包含 `k` 个元素（`k` 为编译期常量） | 无 |
| `T[]` | 变长 | 构造时确定大小，此后永久冻结 | `u32` 长度 |
| `T[u8]`、`T[u16]`、`T[u32]`、`T[u64]` | 变长 | 构造后冻结，可自选长度整数位宽 | 对应整数类型的长度 |
| `T[varint]` | 变长 | 构造后冻结，紧凑的变宽长度编码 | 最多 127 个元素占 1 字节，最多 16,383 个元素占 2 字节 |
| `T[..k]` | 固定大小 | 内联容量 `k`；在 `0..=k` 范围内增长和缩容 | 能容纳 `k` 的最小无符号整型长度（最大 255 用 `u8`，最大 65,535 用 `u16`，依此类推） |
| `T[..]` | 变长 | 构造时确定运行时容量；在该容量内增长和缩容 | `u32` 容量 + `u32` 长度 |
| `T[>..]` | 可调整大小 | **只增数组**（默认主力）；扩容后引用依然有效 | 长度（存于所属栈帧） |
| `T[>..<]` | 可调整大小 | **增缩数组**（栈、队列、堆）；可从任意位置缩容 | 长度（存于所属栈帧） |

以下是它们并排使用的示例：

```goose
fn main() {
    var fixed: i32[4] = [1, 2, 3, 4];
    let zeros: f64[8] = [0.0; 8];           // fill literal: 8 copies of 0.0
    let frozen: i64[] = [7, 8, 9];          // variable class, frozen length
    let tiny: u8[varint] = "hi";            // 1-byte varint length + 2 bytes

    var small: u8[..8] = [];                // fixed-size! capacity 8 lives inline
    small.push('h');
    small.push('i');

    var bounded: i64[..] = [..16];          // runtime capacity 16, length 0
    bounded.push(42);

    var grow: i64[>..] = [];                // grow-only workhorse
    for i in 5 { grow.push(i * i); }

    var stack: i64[>..<] = [];              // grow-shrink stack
    stack.push(10);
    stack.push(20);
    let top = stack.pop();

    print(fixed, " zeros=", zeros.len, " frozen=", frozen, " tiny=", tiny);
    print("small=", small, " (", small.len, "/", small.cap, ") bounded.cap=", bounded.cap);
    print("grow=", grow, " stack=", stack, " top=", top);
}
```

```
[1, 2, 3, 4] zeros=8 frozen=[7, 8, 9] tiny=hi
small=hi (2/8) bounded.cap=16
grow=[0, 1, 4, 9, 16] stack=[10] top=20
```

### 应该选择哪一种？

拿不准主意时，可以参考下面这套快速经验法则：

- **`T[>..]`** —— 你的**默认**动态数组：适用于构建器、集合、节点池和临时缓冲区。尽管名为“只增数组”，只要当前没有任何存活的引用指向其内部，你依然可以对它调用 `.clear()`、`.pop()` 或 `.resize()`（见第 6 章）。
- **`T[>..<]`** —— 需要像栈、队列或二叉堆那样自由压入和弹出元素时的首选。其权衡在于，指向它内部的引用只能存活于局部变量中，不能存储在结构体或其他数组里。
- **`T[..k]`** —— 当需要把小型可增长列表或短字符串**内联放进结构体**（例如 16 字节的用户名或最多 8 个邻居 ID），同时希望结构体保持固定大小并支持随机索引时使用。
- **`T[]` 或 `T[varint]`** —— 用于嵌入记录或数据池中的**已定型、大小只读的数据**（例如字符串数组 `u8[][>..]`，或紧凑记录中的 `u8[varint]`）。
- **`T[k]`** —— 用于固定几何结构或查找表（如 `f32[16]` 矩阵、`bool[256]` 字节表）。

> **内存模型：** 只有 `T[>..]` 和 `T[>..<]` 属于**可调整大小**类别，受“每个数据栈最多一个可调整大小对象”的规则约束。`T[..k]` 是**固定大小**的——因此你可以在一个结构体里放二十个 `u8[..16]` 字段，也可以在单个数据栈上存放包含一百万个该类型元素的数组！

---

## 4.2 字面量、操作与放置规则

### 数组字面量与类型推断

- 没有目标类型时，`[1, 2, 3]` 默认为定长数组 `i64[3]`（如果其元素为变长类型，则为变长数组 `T[]`）。
- `[v; n]` 将 `v`（仅求值一次）重复 `n` 次，其中 `n` 是编译期常量（字面量、具名常量或定长全局变量的 `.len`）。
- `[..cap]` 构造一个容量为 `cap` 的空运行时定容数组 `T[..]`。
- 不带类型标注的 `var out = [];` 会创建一个**只增数组 `T[>..]`**，其元素类型 `T` 将从你首次执行 `push`、`append`、`format` 或整体赋值时自动推断！

### 内置操作

所有数组操作都支持通过 UFCS 使用方法语法（`xs.push(v)` 等价于 `push(xs, v)`）：

| 操作 | 适用类型 | 含义 |
|---|---|---|
| `.len` | 所有数组和切片 | 当前元素个数（`i64`） |
| `.cap` | `T[..k]`、`T[..]` | 固定内联容量（`i64`）；推入超过 `.cap` 将中止程序 |
| `.push(v) -> T&` | 定容数组、`[>..]`、`[>..<]` | 在末尾原地构造 `v`；返回指向它的引用 |
| `.append(src)` | 定容数组、`[>..]`、`[>..<]` | 追加数组、切片或字面量中的所有元素 |
| `.pop() -> T` | 定容数组、`[>..<]`，以及无引用的 `[>..]` | 移除并返回最后一个元素（为空时中止程序） |
| `.pop_n(n)` | 同 `.pop()` | 丢弃最后 `n` 个元素 |
| `.resize(n, v)` / `.resize(n)` | 同 `.pop()` | 用 `v` 的副本（或默认 `T`）扩容，或缩容至 `n` |
| `.clear()` | 同 `.pop()` | 将长度设为 `0` |
| `.index_of(r) -> i64` | 固定大小元素的数组 | 将引用 `r` 转换为其元素下标 |
| `.move_from(src)` | 固定大小元素的切片 | 等长切片之间的重叠安全复制（`memmove` 语义） |

Goose 中的扩容**绝不会暴露未初始化内存**：`.push`、`.append` 和 `.resize` 始终写入有效的元素值。

相等性比较（`==` 和 `!=`）按结构比较数组——先比长度，再逐元素比较——甚至可以跨**不同种类的数组**（只要元素类型相同）直接比较！

```goose
fn main() {
    var xs = [];                         // inferred as i64[>..] from the first push
    xs.push(10);
    xs.append([20, 30, 40]);
    let last .= xs.push(50);
    print("index=", xs.index_of(last), " eq_fixed=", xs == [10, 20, 30, 40, 50]);

    // Overlap-safe copy within the same array via `.move_from`:
    xs[1..4].move_from(xs[0..3]);        // shift elements [10, 20, 30] right by 1
    print("shifted=", xs);

    var st: i64[>..<] = [1, 2, 3, 4];
    st.pop_n(2);
    st.resize(5, 9);
    print("stack=", st);
}
```

```
index=4 eq_fixed=true
shifted=[10, 10, 20, 30, 50]
stack=[1, 2, 9, 9, 9]
```

### 放置规则：什么可以包含什么？

第 3 章介绍的大小类别精确决定了类型之间如何嵌套：

1. **固定大小元素**（`i32`、`Point`、`u8[..16]`、`T&`）可以放进**任意**数组种类和结构体字段中。
2. **变长元素**（`u8[]`、`u8[varint]`、`Order`、`Shape..`）可以放进 `T[]` 和 `T[>..]`（使其成为**顺序**数组），也可以出现在结构体的任意字段位置——但**绝不能**放进 `T[>..<]`、`T[..k]` 或 `T[..]`（因为缩容或复用固定存储槽位绝不能改变元素的字节布局）。
3. **可调整大小的值**（`T[>..]`、`T[>..<]`）可以作为局部变量、全局变量、参数、返回值、普通结构体的**最后一个字段**（使该结构体也成为可调整大小类型），或根结构体（`root struct`）的任意字段——但**绝不能**作为数组的元素。

---

## 4.3 嵌套内联数据与顺序数组

现在我们来看看为什么 Goose 的 `varint` 和 `T[varint]` 类型能够产生如此惊人紧凑的内存布局。

`varint` 是一种变长整数**存储类型**，可用于结构体字段和数组元素（根据数值大小占用 1 到 10 字节；读取时得到 `i64`，仅在构造时设定）。将其与 `T[varint]` 结合，嵌套记录便能以零指针直接打包为紧凑的原始字节：

```text
struct Item  { sku: u8[varint], qty: varint, cents: varint }
struct Order { id: varint, customer: u8[varint], items: Item[varint] }

Order { id: 1001, customer: "alice", items: [
    Item { sku: "SKU-441", qty: 2, cents: 1999 },
    Item { sku: "SKU-7",   qty: 1, cents: 500  },
]}

Inline byte layout inside `Order[>..]` (29 bytes total, 0 pointers):
+------+----+-------+----+----+---------+---+------+----+-------+---+------+
| 1001 | 05 | alice | 02 | 07 | SKU-441 | 2 | 1999 | 05 | SKU-7 | 1 | 500  |
+------+----+-------+----+----+---------+---+------+----+-------+---+------+
  2 B   1 B    5 B   1 B  1 B     7 B    1 B  2 B   1 B    5 B   1 B  2 B
```

每一个字段——包括客户姓名、商品条目列表，以及每条商品的 SKU 字符串——都紧挨着排列在一条缓存行内。

### 权衡：顺序数组

由于每个 `Order`（或 `u8[][>..]` 中的每个 `u8[]` 字符串）可能具有**不同的字节长度**，编译器无法通过 `i * element_size` 来计算第 `i` 个元素的地址。这样的数组称为**顺序**（sequential）数组：

- 你**可以**用 `for x in arr` 按顺序遍历它（每次迭代按引用绑定元素，不发生复制）。
- 你**不能**用 `arr[i]` 下标索引它，也不能调用 `.pop()`。

试图对顺序数组进行下标索引会在编译期立刻被拦截：

<!-- compile-fail -->
```goose
fn main() {
    var words: u8[][>..] = [];
    words.push("alpha");
    words.push("beta");
    print(words[0]);                 // error: variable-size elements cannot be indexed
}
```

### 两全其美：维护视图侧边索引

如果你既想要紧凑的内联存储，*又*需要随机访问或排序呢？只需把字符串打包存放在一个扁平的 `u8[][>..]` 中，再构建一个指向它的轻量级切片索引（`u8[:][>..]`）。对切片进行排序只会重排视图，**连一个文本字节都不会移动**：

```goose
import std;

fn main() {
    var words: u8[][>..] = [];
    words.push("delta");
    words.push("alpha");
    words.push("charlie");
    words.push("bravo");

    var index: u8[:][>..] = [];
    for w in words {
        index.push(w[..]);           // zero-copy view into `words`
    }
    sort(index) { a, b => compare(a, b) < 0 };

    print("sorted views: ", index);
    print("original:     ", words);
    print("2nd sorted:   ", index[1]);
}
```

```
sorted views: ["alpha", "bravo", "charlie", "delta"]
original:     ["delta", "alpha", "charlie", "bravo"]
2nd sorted:   bravo
```

因为 `words` 在其数据栈上绝不会移动，`index` 中的每个切片都会自动保持有效——而且编译器无需任何生命周期标注即可证明这一点。（在第 12 章中，你会看到 4 字节相对引用如何让索引变得更小！）

---

## 4.4 字符串只是 `u8` 数组

Goose 中没有特殊的 `String` 类。每一种文本角色都直接对应一种 `u8` 数组：

| 角色 | Goose 类型 | 说明 |
|---|---|---|
| 字符串字面量 / 借用文本视图 | `const u8[:]`（或 `u8[:]`） | 零拷贝切片；字面量位于只读静态数据区 |
| 字符串构建器 | `u8[>..]` | 指针递增式 `.append`、`.push`、`format(s, ...)` |
| 已定型的内联字符串 | `u8[]` 或 `u8[varint]` | 长度不可变，直接内联打包在数组/结构体中 |
| 固定容量短字符串 | `u8[..16]`（任意 `k`） | 固定大小！内联存于结构体中且不会使其变成顺序布局 |

### 原始多行字符串

标准的 `"..."` 字面量支持转义序列（`\n`、`\t`、`\r`、`\0`、`\\`、`\"`、`\'`、`\xNN`）。对于 SQL、GLSL 着色器、帮助文档或 Windows 路径，Goose 提供了由 `"""..."""` 定界的**原始字符串**——不会解析任何转义字符，而且对于多行原始字符串，每行内容都会自动剥离与结尾 `"""` 相同的缩进前缀：

```goose
fn main() {
    let usage = """
        usage: tool [-v] file
          -v   verbose output
        """;
    let path = """C:\data\new "file" \n""";
    print(usage);
    print(path);
}
```

```
usage: tool [-v] file
  -v   verbose output
C:\data\new "file" \n
```

### 渲染文本与自定义 `format` 重载

有三个内置函数可将任意值转换为文本，它们接受任意数量的参数，且参数之间不插入任何分隔符：

- `print(a, b, ...)` —— 在一次原子写入中将内容加换行符输出到标准输出。
- `format(out, a, b, ...)` —— 将渲染后的文本追加到任意可增长的 `u8` 数组（`u8[>..]` 或 `u8[..k]`）。
- `str(a, b, ...)` —— 返回一个新的 `u8[>..]`，并在其目标处直接构造。

> **注意：** 像 `'a'` 这样的字符字面量是**整数字节常量**（`97`），而不是单字符字符串！因此 `str('a')` 会生成 `"97"`，而 `buf.push('a')` 才会追加实际的字节 `'a'`。

要自定义你自己的结构体或枚举在各处的渲染方式（包括 `print`、`str` 和 `format`，即使嵌套在数组内部也会生效！），只需定义一个 `format(out: u8[>..]&, v: YourType)` 重载：

```goose
struct Person { name: u8[..16], age: i32 }

fn format(out: u8[>..]&, p: Person) {
    format(out, p.name, " (", p.age, ")");
}

fn main() {
    var team: Person[>..] = [];
    team.push(Person { name: "ada", age: 36 });
    var second = Person { name: "", age: 45 };
    format(second.name, "grace");    // `u8[..16]` works as a bounded builder too!
    team.push(second);

    print(team);
    print("first as str: ", str(team[0]), " (char 'a' is ", str('a'), ")");
}
```

```
[ada (36), grace (45)]
first as str: ada (36) (char 'a' is 97)
```

### 日常文本操作与 UTF-8（`import std;`）

导入 `std` 会带来丰富的零拷贝切片辅助函数——`trim`、`find`、`starts_with`、`ends_with`、`split`、`each_split`、`words`、`join`、`parse_int`、`parse_flt`、`format_int`、`format_flt`、`format_replaced`、`to_upper`、`to_lower`，以及 UTF-8 码点迭代（`each_utf8` / `push_utf8`）：

```goose
import std;

fn main() {
    // Zero-copy slicing and parsing:
    let line = "  host = localhost:8080  ";
    let body = trim(line);
    let eq = find(body, "=");
    let key = trim(body[..eq]);
    let val = trim(body[eq + 1..]);
    print(key, " => ", val);

    // Zero-copy split & join:
    let parts = split("10,25,bad,42", ',');
    var sum = 0;
    for p in parts {
        let n, ok = parse_int(p);
        if ok { sum += n; }
    }
    var joined: u8[>..] = [];
    join(joined, parts, " | ");
    print("sum=", sum, " joined=", joined);

    // UTF-8 bytes vs. Unicode code points:
    let greek = "αβγ";
    var codepoints = 0;
    var shifted: u8[>..] = [];
    each_utf8(greek) {
        codepoints++;
        push_utf8(shifted, it + 1);
    };
    print(greek, ": ", greek.len, " bytes, ", codepoints, " code points -> ", shifted);
}
```

```
host => localhost:8080
sum=77 joined=10 | 25 | bad | 42
αβγ: 6 bytes, 3 code points -> βγδ
```

请注意，`split` 返回的是 `u8[:][>..]`——一个指向原始文本的视图数组，零字符复制；而 `each_split` 更进一步，对每个分段直接调用代码块，连切片数组都无需分配。

---

## 4.5 实战示例：零分配内联订单簿

让我们把数组家族、`varint`、原始字符串、自定义 `format`、顺序迭代以及零拷贝视图整合在一起，编写一个完整的内联订单簿：

```goose
import std;
import dictionary;

struct Item  { sku: u8[varint], qty: varint, cents: varint }
struct Order { id: varint, customer: u8[varint], items: Item[varint] }
struct Row   { name: u8[:], total: i64 }

struct Money { cents: i64 }
fn format(out: u8[>..]&, m: Money) {
    format(out, "$", m.cents / 100, ".");
    format_int(out, m.cents % 100, 10, 2, '0');
}

let FEED = """
    1001,alice,SKU-441:2:1999;SKU-7:1:500
    1002,bob,SKU-12:10:250
    1003,alice,SKU-7:3:500;SKU-900:1:12999
    1004,carol,SKU-12:2:250;SKU-13:1:275
    """;

fn parse_items(field: u8[:]) -> Item[>..] {
    var items: Item[>..] = [];       // constructed directly inside each Order's `items` field
    each_split(field, ';') {
        let p = split(it, ':');
        assert(p.len == 3);
        let qty, ok1 = parse_int(p[1]);
        let cents, ok2 = parse_int(p[2]);
        assert(ok1 && ok2);
        items.push(Item { sku: p[0], qty: qty, cents: cents });
    };
    return items;
}

fn order_total(o: Order&) -> i64 {
    var total = 0;
    for item in o.items {
        total += item.qty * item.cents;
    }
    return total;
}

fn main() {
    var book: Order[>..] = [];
    each_split(FEED, '\n') {
        guard it.len > 0;
        let f = split(it, ',');
        assert(f.len == 3);
        let id, ok = parse_int(f[0]);
        assert(ok);
        book.push(Order { id: id, customer: f[1], items: parse_items(f[2]) });
    };

    var grand = 0;
    var item_count = 0;
    for o in book {
        grand += order_total(o);
        item_count += o.items.len;
    }
    print(book.len, " orders, ", item_count, " line items, ",
          bytes_of(book).len, " packed bytes, total ", Money { grand });

    // Group totals per customer using zero-copy slices directly into `book`:
    var by_cust = dictionary<u8[:], i64> {};
    for o in book {
        by_cust.update(o.customer[..], 0) { it += order_total(o); };
    }

    var rows: Row[>..] = [];
    by_cust.each() { name, total => rows.push(Row { name: name, total: total }); };
    sort(rows) { a, b => a.total > b.total };

    for r in rows {
        print("  ", r.name, ": ", Money { r.total });
    }
}
```

```
4 orders, 7 line items, 105 packed bytes, total $222.72
  alice: $189.97
  bob: $25.00
  carol: $7.75
```

不妨停下来品味一下这个数字：**4 份完整订单、7 条商品明细、4 个客户字符串以及 7 个 SKU 字符串，全部紧密打包在 105 个连续字节中，零次堆分配、零个指针**；同时，`by_cust` 直接通过指向 `book` 内部的零拷贝切片完成了按客户汇总！

---

## 练习

1. **比较 `u8[]` 与 `u8[varint]`：** 在第 4.5 节的实战示例中，将 `u8[varint]` 改为 `u8[]`，将 `Item[varint]` 改为 `Item[]`，并将 `varint` 字段改为 `i64`。点击 **▶ Run**，将新的 `bytes_of(book).len` 与 `105` 字节进行对比。
2. **定容短字符串溢出：** 创建一个 `var tag: u8[..4] = [];` 并调用 `format(tag, "goose");`。在演练场中运行它，观察定容数组超出容量时的精确运行时中止信息，然后将容量扩大为 `u8[..8]`。
3. **自定义十六进制字节转储：** 编写一个小型结构体 `struct ByteDump { data: const u8[:] }`，并为其定义 `format(out: u8[>..]&, d: ByteDump)` 重载，利用 `format_int(out, b, 16, 2, '0')` 将任意切片打印为空格分隔的十六进制字节序列（例如，`"hi"` → `68 69`）。

---

## 本章要点

- Goose 的数组家族（`T[k]`、`T[]`/`T[varint]`、`T[..k]`/`T[..]`、`T[>..]`、`T[>..<]`）让你无需堆分配器就能精确选择内联内存布局和可变性规则。
- 变长元素的数组（`u8[][>..]`、`Order[>..]`）以零指针方式打包嵌套数据；它们是**顺序**的（可迭代、不可下标索引），并可通过零拷贝切片的侧边数组实现索引或排序。
- 字符串就是 `u8` 数组（`const u8[:]` 字面量/视图、`u8[>..]` 构建器、`u8[]`/`u8[varint]` 已定型字符串、`u8[..k]` 内联短字符串）。
- `print`、`str` 和 `format` 会按结构渲染任意值，并自动采用自定义的 `fn format(out: u8[>..]&, v: T)` 重载。

至此，**第一部分：基础篇**告一段落。在**第二部分：引用、类型与安全内存**中，我们将以**第 5 章：切片、引用与可选引用**开篇，揭示 Goose 如何使引用保持透明、空值安全且绝无悬垂可能。
