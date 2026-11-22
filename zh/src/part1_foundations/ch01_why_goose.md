# 第 1 章：为什么选择 Goose？

每一种系统编程语言都在动态内存管理上押下了自己的赌注。C 和 C++ 将 `malloc` 与 `free`（或 `new` 与 `delete`）交到你手中——理论上很快，但在实践中既脆弱，又会在内存分配碎片化、析构函数遍历深层对象图时悄然变得昂贵。Go、Java 和 C# 引入了垃圾回收器——既安全又便捷，但代价是停顿压力、追踪开销和更高的内存占用。Rust 在保持内存安全的同时消除了垃圾回收器，但它依然依赖通用堆分配器，并且每当数据结构变得相互关联时，都会要求你在所有权转移、独占性规则和生命周期标注之间小心翼翼地周旋。

Goose 押下了一个截然不同的赌注：**如果根本没有堆，会怎样？**

在 Goose 中，没有 `malloc`，没有垃圾回收器，没有引用计数，没有析构函数，也没有生命周期标注——然而这门语言依然是**内存安全**的（没有越界读取、没有悬垂引用、没有类型混淆，也没有 `unsafe` 逃生舱口）。更令人惊讶的是，移除堆并不会拖慢程序；在 Goose 的基准测试套件中，它通常会令程序比 C++ 和安全 Rust 更快、内存占用更小。

读完本章后，你将能够：

- 解释 Goose 数据栈内存模型的核心思想，以及它为何能消除堆开销。
- 安装或验证 `goose` 编译器，并检查其工具链。
- 编写、运行、类型检查、格式化以及原生编译你的第一个 Goose 程序。
- 识别让 Goose 与众不同的标志性特性——跨扩容稳定的引用、内联嵌套数据以及作用域退出时的自动清理。

---

## 1.1 堆的隐性税

考虑编程中最寻常不过的一项任务：构建一个包含五个字符串的列表，从 `"word0"` 到 `"word4"`。

```goose
fn main() {
    var words: u8[][>..] = [];
    for i in 5 {
        words.push(str("word", i));
    }
    print(words);
}
```

```
["word0", "word1", "word2", "word3", "word4"]
```

从表面上看，这就像任何现代语言中的普通循环。然而在底层，它的内存布局与 C++ 或 Rust 截然不同。

在 C++（`std::vector<std::string>`）或 Rust（`Vec<String>`）中，字符串列表通常是一张由堆分配和指针交织而成的网：一块堆缓冲区存放向量中的字符串头数组（每个元素包含指针、长度和容量），再加上超出短字符串优化（SSO）范围的每个字符串各自的独立堆分配。当向量增长超过当前容量时，它会分配一块更大的缓冲区，移动每一个字符串头，然后释放旧缓冲区——这会使指向其中的指针全部失效。当向量离开作用域时，析构函数必须逐一遍历每个元素，先释放每个字符串的缓冲区，再释放外层缓冲区。

而在 Goose 中，`words` 是**一整块连续的字节**，没有任何指针：

```text
C++ / Rust (Vec<String> — pointer indirection & heap allocations):

  stack frame          heap (vector buffer)               heap (strings)
  +----------+        +--------------------+             +-------------+
  | ptr  ----|------->| ptr | len | cap    |------------>| w o r d 0   |
  | len = 5  |        +--------------------+             +-------------+
  | cap = 8  |        | ptr | len | cap    |-------+     +-------------+
  +----------+        +--------------------+       +---->| w o r d 1   |
                      | ...                |             +-------------+

Goose (u8[][>..] — one flat, contiguous block on a data stack):

  frame header         data stack (bump-allocated, zero pointers)
  +----------+        +------+---------+------+---------+------+---------+
  | len = 5  |        | 05   | w o r d | 05   | w o r d | 05   | w o r d | ...
  | base ----|------->|      | 0       |      | 1       |      | 2       |
  +----------+        +------+---------+------+---------+------+---------+
                                                                         ^
                                                                   bump pointer
```

留意这个变量在生命周期每个阶段所发生的事情：

1. **构造：** 压入每个字符串时，只需在递增指针（bump pointer）处直接写入其长度和字节内容——无需查询分配器，无需对空闲链表加锁，无需检查容量，也不存在先构造临时字符串再拷贝进数组的过程。
2. **读取：** 遍历 `words` 会沿着 CPU 缓存行线性流式访问，完全没有指针追踪（pointer chasing）。
3. **销毁：** 当 `main` 返回（或任何外围 `{ ... }` 代码块结束）时，释放 `words` 只需要**一次存储指令**，将数据栈的递增指针回退即可。无论 `words` 中存放了五个字符串还是五百万个字符串，清理耗时都是相同的常数时间：一条指令。

> **内存模型：** Goose 将内存划分为三个区域：原生调用栈（用于存放固定大小的值和头部元数据）、静态只读数据区（用于存放字符串字面量和编译期常量），以及 **N 个数据栈**——大块虚拟地址空间预留区域，其数量 `N` 由编译器在编译期静态计算得出。操作系统仅在程序实际触及虚拟内存页时才会提交物理页。

### Goose 的四大支柱

Goose 中的每一项重大设计抉择，都源自以下四个环环相扣的特性：

1. **用数据栈取代堆。** 在任意时刻，任何一个数据栈上至多只有一个可调整大小的值处于活跃状态，并且它始终位于栈顶。扩容就是一次纯粹的指针递增；内存永不搬迁。
2. **作用域退出是唯一的释放方式。** 每一个动态值都由某个变量（局部变量或全局变量）拥有。当该变量的作用域结束时，其对应的数据栈在 O(1) 时间内回退。
3. **嵌套数据保持内联。** 字符串、字符串数组，或包含变长字段的结构体，都会被存储为一段连续的扁平字节序列——而非由指针串联而成的对象图。
4. **零标注的安全引用。** 由于数据栈上的内存永不移动，指向正在增长的数组内部的引用在该数组的整个生命周期内始终有效。编译器会自动推断是哪个变量充当每个引用的*根*，并拒绝任何可能比其所有者存活更久的引用——既无需 `'a` 生命周期语法，也无需借用检查器的独占性规则。

> **为什么这在实践中很重要：** 在 Goose 的 16 项基准测试套件中（测量完整进程耗时，包含启动与清理阶段），上述布局与分配特性使 Goose 的运行速度大致达到**惯用写法 C++ 的 3.5 倍**、**专家手工调优 C++ 的 1.25 倍**，以及**最优安全 Rust 的 1.13–1.19 倍**，同时峰值内存占用分别降低了 **1.95 倍、1.32 倍和 1.25 倍**。我们将在第 19 章详细剖析其中的优势与短板。

---

## 1.2 安装与验证 Goose

Goose 以单个可执行文件 `goose` 的形式发布，其中打包了编译器、进程内 JIT 运行器（TinyCC）、原生构建驱动、包获取器、代码格式化工具以及语言服务器协议（LSP）服务器。

### 安装预编译发行版

从 [Goose 发布页面](https://github.com/aardappel/goose/releases)下载适用于 Linux（x64）、macOS（Apple Silicon 或 Intel）或 Windows（x64）的压缩包，解压后将其目录添加到你的 `PATH` 中。在 macOS 上，你也可以通过 Homebrew 安装：

```bash
brew install goose-lang
```

在 Linux 上，进程内运行程序需要系统的 C 标准库头文件（Debian/Ubuntu 上为 `libc6-dev`，Fedora/RHEL 上为 `glibc-devel`）。使用 `goose build` 构建原生可执行文件时，还需要一台 C 编译器（Linux/macOS 上为 `gcc` 或 `clang`；Windows 上为 Visual Studio 2022+ 或 `clang`）。

### 从源码构建

如果你更倾向于自行构建 Goose，则需要 CMake 3.27+、支持 C++20 的编译器以及 Git：

```bash
git clone https://github.com/aardappel/goose
cd goose
cmake -B build -DCMAKE_BUILD_TYPE=Release -DGOOSE_BUILTIN_LIBS=all
cmake --build build --config Release
```

`-DGOOSE_BUILTIN_LIBS=all` 选项会将图形（`gfx`）、音频（`audio`）、界面组件（`ui`）和刚体物理（`physics`）等库直接编译进 `goose` 二进制文件中。

### 检查工具链

运行 `goose version` 和 `goose toolchain`，确认所有组件都已就绪：

```bash
goose version
goose toolchain
```

你应该会看到类似如下的输出：

```text
goose v2026.4-64-ga5ab89f3 (runtime 41e336e5d1ee)
C compiler:   gcc 15.2.0, found on PATH
              /usr/bin/cc
C++ compiler: /usr/bin/g++
style:        cc
cmake:        /usr/bin/cmake
git:          /usr/bin/git
built in:     physics, gfx, audio, ui
GOOSE_HOME:   /home/you/.goose
```

`GOOSE_HOME`（默认为 `~/.goose`）是 Goose 用来缓存已编译运行时目标文件、拉取的依赖包以及构建产物的目录。

---

## 1.3 你的第一个 Goose 程序

创建一个名为 `hello.goose` 的文件：

```goose
fn main() {
    print("hello, goose");
}
```

```
hello, goose
```

无需单独的构建步骤，直接将文件传给 `goose` 即可立即运行：

```bash
goose hello.goose
```

在这短短三行代码中，已有几点值得留意：

- `fn main()` 是根文件的入口点。它不接收参数（命令行参数位于 `os` 模块中），也没有返回值。
- `print(...)` 是内建函数，而非 `printf` 风格的包装器。它接受任意数量、任意类型的参数，将每个参数转换为文本，在末尾追加换行符，并通过单次原子 `write` 调用输出整行内容（因此并发线程的输出绝不会互相交错）。

Goose 中的每一个值——标量、布尔值、数组、结构体、枚举——开箱即支持自我打印：

```goose
struct Point { x: f64, y: f64 }

fn main() {
    let answer = 42;
    let ratio = 0.75;
    let nums: i64[3] = [10, 20, 30];
    let origin = Point { x: 1.5, y: -2.0 };
    print("answer=", answer, " ratio=", ratio, " ok=", answer > 0);
    print("nums=", nums, " origin=", origin);
}
```

```
answer=42 ratio=0.75 ok=true
nums=[10, 20, 30] origin=Point { 1.5, -2.0 }
```

仅仅为了查看数据，你不需要格式说明符，不需要 `#[derive(Debug)]`，也不需要手写 `operator<<` 样板代码。（在第 4 章中，你将学到如何在需要定制输出时定义自定义 `format` 重载。）

---

## 1.4 Goose 日常开发循环

在阅读本书的过程中，以下四条命令将构成你的日常工作流：

| 命令 | 作用 | 适用场景 |
|---|---|---|
| `goose file.goose` *（或 `goose run file.goose`）* | 编译为 C 并通过 TinyCC 在进程内立即执行 | 学习与原型验证期间的快速编辑-运行迭代 |
| `goose build file.goose` | 通过本机 C 编译器（`-O2` / `/O2`）在源文件旁编译出原生可执行文件 | 基准测试、分发二进制文件以及使用线程的程序 |
| `goose check file.goose` | 在不运行的情况下完成解析、类型检查、生命周期检查和边界检查 | 快速验证，或配合 `--stacks`、`--bce-lines` 进行检查 |
| `goose format file.goose` | 按 Goose 规范格式重写源文件 | 保持代码整洁一致（在 CI 中可配合 `--check` 使用） |

### 进程内运行与原生构建

Goose 会将每个程序编译为单个 C 文件。当你运行 `goose hello.goose` 时，Goose 会将该 C 代码直接在内存中交给内嵌的 TinyCC 后端——不会调用链接器，也不会向磁盘写入可执行文件。

当你运行 `goose build hello.goose` 时，Goose 会调用本机的优化 C 编译器（`gcc`、`clang` 或 MSVC）生成高性能的原生二进制文件（`./hello` 或 `hello.exe`）：

```bash
goose build hello.goose && ./hello
```

请牢记 `goose run` 与 `goose build` 之间的两点差异：

1. **优化：** TinyCC 不会对生成的 C 代码进行优化。进行性能测试前，务必使用 `goose build`。
2. **线程：** TinyCC 无法为工作线程的数据栈分配线程局部存储（TLS），因此使用线程（`thread_fn`）的程序必须通过 `goose build` 构建。

> **编译器提示：** 为 `goose run`、`goose build` 或 `goose check` 添加 `--debug` 参数，即可开启整数溢出、窄化 `as` 转换和枚举标签的运行时检查，每项检查都会报告精确的源码行号。为 `goose check` 添加 `--stacks` 参数，可以查看编译器为程序分配了多少个数据栈。

### 编辑器支持

由于编译器本身内置了语言服务器协议（LSP）实现，运行 `goose lsp` 即可为任何支持 LSP 的编辑器提供实时诊断、悬停类型信息、跳转到定义、自动补全、重命名、内联提示以及保存时自动格式化功能：

- **VS Code：** 从 Goose 仓库或发行压缩包安装附带的扩展（`code --install-extension vscode/goose-language.vsix`）。
- **Neovim、Helix、Emacs：** 将编辑器内置的 LSP 客户端（`vim.lsp.config`、`languages.toml` 或 `eglot`）指向 `goose lsp`。Goose 仓库中的 `docs/editors.md` 提供了完整的配置片段。

---

## 1.5 初窥 Goose 的独到之处

在进入第 2 章的语法与规则细节之前，让我们先运行一段精炼的程序，感受一下你将在 Goose 中反复见到的三大核心理念：

1. **跨数组扩容依然有效的引用**（不存在重分配导致的失效）。
2. **内联嵌套记录**（字符串与子数组紧密打包，零指针）。
3. **确定性的作用域退出清理**（临时内存通过一次存储指令即可回收）。

```goose
struct Item  { sku: u8[varint], qty: varint, cents: varint }
struct Order { id: varint, customer: u8[varint], items: Item[varint] }

struct Node { id: i32, weight: f32 }

fn main() {
    // 1. References survive growth on a grow-only array (`T[>..]`).
    var nodes: Node[>..] = [];
    let first .= nodes.push(Node { id: 1, weight: 0.5 });
    for i: i32 in 2..1001 {
        nodes.push(Node { id: i, weight: 0.0 });
    }
    first.weight = 99.5;
    print("nodes=", nodes.len, " first=", nodes[0]);

    // 2. Nested variable-size records pack inline with zero pointers.
    var book: Order[>..] = [];
    book.push(Order {
        id: 1001,
        customer: "alice",
        items: [
            Item { sku: "SKU-441", qty: 2, cents: 1999 },
            Item { sku: "SKU-7", qty: 1, cents: 500 },
        ],
    });
    print("orders=", book.len, " packed bytes=", bytes_of(book).len);

    // 3. Scratch data lives only until the closing brace.
    {
        var scratch: u8[>..] = [];
        for i in 10000 {
            scratch.push((i % 256) as! u8);
        }
        print("scratch bytes=", scratch.len);
    } // `scratch` is freed here by a single bump-pointer store.
}
```

```
nodes=1000 first=Node { 1, 99.5 }
orders=1 packed bytes=29
scratch bytes=10000
```

停下来仔细端详这三行输出：

- **`nodes=1000 first=Node { 1, 99.5 }`：** `let first .= nodes.push(...)` 绑定了一个指向第 0 号元素的直接强类型引用。此后继续压入 999 个元素从未移动过第 0 号元素，因此随后修改 `first.weight` 完全安全，且经过静态检查。（在 C++ 中，若未提前 `reserve`，继续 `push` 会悄无声息地让 `first` 成为悬垂引用；在安全 Rust 中，借用检查器则会禁止跨 `push` 持有 `first`。）
- **`orders=1 packed bytes=29`：** 一份完整的订单——包含一个 ID、客户名 `"alice"`，以及一个嵌套的两项商品明细列表（每项又自带 SKU 字符串）——总共只占用 **29 个连续字节和 0 次堆分配**。与之等价的 C++ `std::string` + `std::vector<Item>` 大约占用 160 字节并伴随多次堆分配；Rust 的 `String` + `Vec<Item>` 则分散在四次分配中，大约占用 153 字节。
- **`scratch bytes=10000`：** 在 `{ ... }` 代码块内，`scratch` 通过递增栈指针不断增长。到达右花括号 `}` 时，整块 10,000 字节的缓冲区会在一条指令内瞬间释放。

> **注意：** 如果 `[>..]`、`.=`、`varint` 或 `as!` 这些符号此刻看起来还有些陌生，不必担心。第 2 章到第 6 章会循序渐进地为你逐一拆解。

---

## 1.6 Goose 放弃了什么（坦诚的预告）

没有任何内存模型是没有代价的。在第一天就理解 Goose 的取舍，可以避免日后的困惑与挫败：

- **你需要显式思考所有权作用域。** 大多数数据归属于构造它们的函数，这几乎不需要任何额外心智负担。而当对象具有相互重叠、非嵌套的生命周期时（例如以任意顺序删除的图节点，或编辑器中打开的多个文档），你会将它们放入标准库提供的 `slot_pool<T>`（槽位池）或 `slice_pool<T>`（切片池）中（第 8 章）。
- **递归函数不能跨递归调用持有可增长的局部变量。** 临时缓冲区必须在递归调用之前结束生命周期，而持久化容器则需要由递归外部拥有并作为参数传入（第 11 章）。
- **紧凑的变长数组只能顺序访问。** 将 `Order` 紧密打包进 29 字节，意味着不同元素的字节大小各不相同，因此你可以按顺序遍历 `book`，但无法在不额外维护一份引用索引的前提下以 O(1) 时间直接跳到 `book[42]`（第 4 章和第 12 章）。
- **封闭世界编译。** Goose 一次性编译整个程序——没有逃逸闭包、没有函数指针，也没有枚举标签之外的动态派发（第 7 章和第 9 章）。正是这种封闭世界假设，才让静态数据栈分配、生命周期推断以及零开销高阶函数成为可能。

上述每一项约束都为你换来了实实在在的回报：可预测的延迟、紧凑且缓存友好的内存布局、零分配器开销，以及告别标注疲劳的内存安全。

---

## 练习

1. **查看编译器的数据栈分配：** 将 1.5 节的完整示例保存为 `taste.goose`，然后运行：
   ```bash
   goose check --stacks taste.goose
   ```
   观察 `main` 需要多少个数据栈（`nodes`、`book`、`scratch`，外加一个用于打印的临时缓冲区）。接着，在第一个 `scratch` 代码块结束*之后*，添加第二个 `{ var scratch2: u8[>..] = []; ... }` 代码块，并再次运行 `goose check --stacks taste.goose`。注意数据栈总数并没有增加——顺序执行的作用域会自动复用数据栈。
2. **对比进程内运行与原生构建：** 先运行 `goose taste.goose`，再使用 `goose build taste.goose && ./taste` 进行原生构建并运行。尝试运行 `goose build --dry-run taste.goose`，查看 Goose 在底层生成的确切 C 编译器命令。
3. **探索 `print` 与格式化：** 在 `taste.goose` 中向 `book` 添加第二个包含三项商品的 `Order`，用 `print(book)` 直接打印 `book`，并观察 `bytes_of(book).len` 的变化。运行 `goose format taste.goose`，体验 Goose 的规范代码格式。

---

## 本章要点

- Goose 是一门基于**静态分配的数据栈**而非堆分配器或垃圾回收器的内存安全系统编程语言。
- 数据栈上的内存通过递增指针增长，永不搬迁，并在其所属作用域退出时以 O(1) 时间释放。
- 嵌套的字符串、结构体和数组会以**内联**方式打包为连续字节且不含指针，大幅削减分配开销与缓存未命中。
- 单个可执行文件 `goose` 集成了即时进程内运行（`goose run`）、优化原生编译（`goose build`）、静态验证（`goose check`）、代码格式化（`goose format`）以及编辑器集成（`goose lsp`）。

在**第 2 章**中，我们将明快地走一遍 Goose 的语法、精确位宽标量、面向表达式的控制流、`guard`、`defer` 以及多返回值，让你迅速掌握日常编程所需的全部基础构件。
