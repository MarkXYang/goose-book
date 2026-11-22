# 第 16 章：程序结构、包与工具链

小型示例可以轻松地放在单个 `.goose` 文件中。但真实的应用程序往往横跨多个模块、引入可复用的库、打包字体或着色器、在 CI 中执行格式检查，并与编辑器和外部构建系统集成。

Goose 刻意将项目结构保持在最简形态：**从单个根文件出发的全程序编译、无中心注册表的扁平化固定版本 Git/路径包，以及单一的 `goose` 可执行文件**——它一站式处理运行、构建、检查、拉取依赖、格式化以及 LSP 编辑器支持。

读完本章后，你将能够：

- 使用 `import`、命名空间、文件级命名空间别名和全局变量初始化规则来组织多文件 Goose 程序。
- 使用 `embed_file` 嵌入编译期静态资源，并使用 `resource_dir()`（来自 `os`）以可移植的方式定位运行期资源。
- 基于扁平的根文件依赖模型，使用和编写 Goose 包——无论是纯 Goose 包、C 语言底层包，还是 CMake 驱动的包。
- 在日常开发和 CI 中熟练运用完整的 `goose` 命令行工具（`--stacks`、`--bce-lines`、`--stats`、`--diagnostics json`、`--standalone`、`goose format`、`goose lsp`）。

---

## 16.1 文件、`import` 与全程序编译

每条 `goose` 命令都接收一个**根文件（root file）**——即包含 `fn main()`（或用于库导出的 `export fn`）并声明程序所用包的入口文件。编译器会传递性地追踪所有 `import` 语句，并将整个程序作为一个整体单元进行编译。

- `import a.b.c;` 会将 `a/b/c.goose` 加载**一次**（重复导入无害）。Goose 按以下顺序查找：
  1. 相对于导入文件所属包的根目录（对于你自己的程序，即根文件所在目录）。
  2. 根文件中声明的某个包内的模块（例如 `import mylib.extra;`）。
  3. 标准库目录（`stdlib/`，可通过 `--stdlib` 或 `GOOSE_STDLIB` 覆盖）。
- `import .helper;`（前导点号）相对于*导入文件自身*所在目录进行解析。
- **导入不会创建命名空间**——它们只是将该文件的声明纳入本次编译。
- **可运行模块可直接作为库使用：**被*导入*文件中的所有 `fn main()` 都会被静默忽略！给一个可复用文件编写 `fn run_demo()`，再配上一个小巧的 `fn main() { run_demo(); }`，你既可以独立运行它，也可以干净地从其他程序中导入它。

---

## 16.2 命名空间、别名与查找规则

在文件顶部（声明之前）加上 `namespace geo;`，即可将该文件中的所有顶层声明置于命名空间 `geo` 中。多个文件可以共同贡献到同一个命名空间中，单个声明也可以显式限定自己的命名空间（例如在带有命名空间的文件中编写 `fn geo::norm1(...)` 或 `fn ::main()`）。

名称查找遵循三条清晰的规则：

1. **解析顺序：**词法作用域（局部变量、类型参数、嵌套函数）→ 当前命名空间 → 全局命名空间与内置项。
2. **UFCS 与 `format` 跟随类型：**对 `geo::Pt` 调用 `p.norm1()` 时，无论当前处于*哪个*命名空间，都会自动在命名空间 `geo` 中查找；`print(p)` / `str(p)` 同样会自动找到 `geo::format`！
3. **重载集绝不跨命名空间合并：**如果命名空间 `geo` 声明了自己的 `hash`，那么在 `geo` 内部调用 `hash(x)` 时*只会*搜索 `geo` 的重载集——若要访问全局的 `std` 重载，请显式写作 `::hash(x)`。

当某个包的命名空间名称较长时，可以在文件顶部添加一个**文件级别名（file-local alias）**：`namespace g = goose_geometry;`。别名绝不会泄漏给导入该文件的其他文件。

```goose
import std;
namespace geo;

struct Pt { x: i64, y: i64 }

enum Kind { Origin, Offset { dx: i64, dy: i64 } }

fn norm1(p: Pt) -> i64 { abs(p.x) + abs(p.y) }  // `abs` found in global `std`

fn format(out: u8[>..]&, p: Pt) {
    format(out, "(", p.x, ",", p.y, ")");
}

// `::main` places the entry point in the global namespace despite `namespace geo;`:
fn ::main() {
    let p = geo::Pt { x: -3, y: 4 };
    let k: geo::Kind = geo::Kind.Offset { dx: 1, dy: 2 };
    // Both `print(p)` and `p.norm1()` find `geo::format` and `geo::norm1` automatically!
    print("pt=", p, " norm1=", p.norm1(), " kind=", k);
}
```

```
pt=(-3,4) norm1=7 kind=Offset { 1, 2 }
```

请注意，`::`（命名空间限定）与 `.`（变体/字段/UFCS 访问）是不同的词法记号，因此 `geo::Kind.Offset { ... }` 可以毫无歧义地解析。Goose 的命名空间不支持嵌套，也没有可见性修饰符——它们的存在纯粹是为了清晰的命名组织和整洁的 C 符号隔离。

---

## 16.3 全局变量、初始化顺序与嵌入资源

### 全局变量与全程序 Arena

全局变量可以是任意类型——标量、结构体、可增长数组或根结构体（`root struct`）池。它们在 **`main` 运行之前**完成初始化：先初始化被导入文件中的全局变量，再按各文件内的声明顺序依次初始化，且初始化表达式可以调用函数。

- 仅给出类型的 `var` 全局变量会从默认值开始初始化（例如 `var log: u8[>..];` 是一个空的只增数组）。
- 具有编译期初始化表达式的固定大小扁平类型 `const` 全局变量会存放在**静态只读内存**中，供所有线程共享。
- 全局可增长数组在程序整个生命周期内占据某个数据栈的底部——使其成为零样板代码的全程序 Arena（并且支持 `in pool` 相对引用和索引类型！）。
- 在某个全局变量的初始化完成之前引用它，会在编译期被直接捕获报错。

```goose
let TAGS = ["alpha", "beta", "gamma"];           // static read-only data
let LAST_IDX = TAGS.len - 1;                     // named constant derived from global .len
var counts: i64[TAGS.len] = [0; TAGS.len];
var audit: u8[>..];                              // type-only `var` global starts empty

fn main() {
    counts[LAST_IDX] += 3;
    format(audit, "last=", TAGS[LAST_IDX], " count=", counts[LAST_IDX]);
    print(audit, " all=", counts);
}
```

```
last=gamma count=3 all=[0, 0, 3]
```

### 编译期资源与运行期资源

Goose 清晰地区分了烘焙进可执行文件的资源与随可执行文件一同分发的资源：

- **在编译期烘焙进可执行文件（`embed_file`、`embed_shader`）：**

  <!-- fragment -->
  ```goose
  let FONT_BYTES = embed_file("assets/font.ttf");    // const u8[:] in static data
  ```
  `embed_file("path")` 在**编译期**相对于调用它的源文件进行解析，并将文件字节直接嵌入静态数据区（按路径去重）。`embed_shader(...)` 类似地在编译期为 `gfx` 编译 GLSL 着色器。这种方式非常适合自包含的单文件二进制部署和包分发！
- **随可执行文件一同分发（来自 `os` 的 `resource_dir()`）：**
  对于大型外部资源文件（如游戏 WAD 文件或纹理），请使用 `import os;` 中的 `str(resource_dir(), "data/map.wad")`：在 `goose run` 下，`resource_dir()` 指向根源文件所在目录；在 `goose build` 下，它解析为编译后可执行文件自身所在的目录（或在 macOS `.app` 应用包内指向 `Contents/Resources/`）。

---

## 16.4 扁平固定版本包

Goose 的包模型旨在通过一条严格规则杜绝依赖地狱：

> **只有根文件才能声明包来源。**

在你的根文件（`app.goose`）中，使用 `import name from "source";` 将每个包声明一次：

```text
import mylib from "joe/mylib@v2";                             // GitHub shorthand at tag v2
import mylib from "github.com/joe/mylib@3f2c1a9";             // pinned commit (reproducible)
import mylib from "https://git.example.com/x/mylib.git@main"; // full Git URL at branch
import mylib from "../mylib";                                 // local directory (edited live!)
```

- 之后在项目的任意位置，都可以通过 `import mylib.extra;` 导入 `mylib` 的子模块。
- **官方内置/推荐包（blessed packages）**（`gfx`、`audio`、`ui`、`physics`）由编译器直接按名称识别——只需写 `import gfx;` 而无需 `from "..."` 来源（而且如果你的编译器使用 `-DGOOSE_BUILTIN_LIBS=all` 构建，它们就已经直接编译在 `goose` 内部了！）。
- **传递依赖要求保持扁平：**如果 `mylib` 依赖 `vec_extras`，`mylib.goose` 会写下 `import vec_extras from "github.com/x/vec_extras@v2";` 作为*依赖要求*。如果你的根文件遗漏了 `vec_extras`，编译器会直接告诉你应当添加的确切声明行；如果两个库对同一包的来源仓库意见不一致，编译器会报错。每个程序对每个包**至多只包含一个版本**，全部锁定在一个清晰可见的位置。

### 包布局与编写

Goose 包本质上就是一个普通的 Git 仓库（或本地目录），无需任何注册表登记：

```text
mylib/
  mylib.goose        # Entry file (`namespace mylib;`)
  mylib.build        # Optional sidecar for C sources, c_headers, or CMake target
  extra.goose        # Additional module (`import mylib.extra;`)
  src/, include/     # Optional C code
  examples/, test/   # Runnable root programs demonstrating/testing the package
```

基于现有 CMake C 库封装的包，在 `mylib.build` 中只需寥寥数行配置：

```text
// mylib.build
cmake_target = "mylib";
cmake_options = ["MYLIB_SIMD=ON"];
exclude = ["tools", "third_party/demo"];
// Optional: mark non-thread-safe global C libraries so workers cannot call them:
// main_thread_only = true;
```

### 拉取依赖、离线构建与缓存

```bash
goose fetch app.goose               # download all declared packages without compiling
goose fetch --update app.goose      # re-fetch packages pinned to a tag or branch
goose build --offline app.goose     # guarantee zero network access
```

Goose 拉取或编译的所有内容都位于 `~/.goose` 目录下（可通过 `GOOSE_HOME` 覆盖）：`pkg/`（Git 检出目录）、`build/pkg/`（按工具链区分的原生构建产物）、`build/runtime/`（缓存的运行时目标文件）、`build/prog/`（各程序生成的 C 代码与目标文件）以及 `config`（例如 `cc = "clang";`）。删除 `~/.goose` 始终是安全的——其中的所有内容都会按需重新生成。

---

## 16.5 掌握 `goose` 命令行与工具链

单个可执行文件即可覆盖完整的开发与 CI 生命周期：

| 命令 / 选项 | 用途 |
|---|---|
| `goose app.goose -- arg1 arg2` | 快速 TinyCC 进程内运行；`--` 之后的参数传递给 `args()`（来自 `os`） |
| `goose build -o bin/app app.goose` | 原生优化构建（`-O2` / `/O2`）；添加 `--debug` 以启用 `-O0 -g` 及溢出/范围检查 |
| `goose build --dry-run app.goose` | 打印确切的 C 编译器与链接器命令，但不实际执行 |
| `goose check --stacks app.goose` | 报告每个程序/工作线程的确切数据栈数量及 `hardware_threads()` 上限 |
| `goose check --bce-lines app.goose` | 逐行报告已消除与仍保留的越界检查（第 18 章） |
| `goose check --stats app.goose` | 打印声明、特化、内联与越界检查消除（BCE）汇总统计信息 |
| `goose check --diagnostics json app.goose` | 输出机器可读的 JSON 诊断信息，便于 CI 或自定义工具集成 |
| `goose run --stack-reserve 16G app.goose` | 增大每个数据栈的虚拟内存预留空间（默认为 `2G`） |
| `goose run -DGS_STACK_STATS=1 app.goose` | 程序退出时在 stderr 上打印实际运行期数据栈高水位使用量 |
| `goose format src/` / `goose format --check .` | 原地格式化所有 `.goose` 文件，或在 CI 中检测到未格式化文件时以退出码 `1` 失败 |
| `goose toolchain` / `goose version` | 查看检测到的 C 编译器、内置包、`GOOSE_HOME` 以及运行时哈希 |
| `goose completion bash\|zsh\|fish\|powershell` | 生成 Shell Tab 自动补全脚本 |
| `goose lsp` | 启动内置 Language Server Protocol 服务器，支持 VS Code、Neovim、Helix 或 Emacs |

### 将生成的 C 代码集成到外部构建系统

如果你想使用 Make、CMake、Ninja 或 Bazel 自行驱动 C 编译器，可以直接输出 C 源码：

```bash
# Single self-contained C file (easiest for external builds):
goose --standalone -o app.c app.goose
cc -O2 app.c -o app -lm -pthread

# Or share one compiled runtime object across many programs:
goose --emit-runtime goose_runtime.c
cc -O2 -c goose_runtime.c
goose -o app.c app.goose
cc -O2 app.c goose_runtime.o -o app -lm -pthread
```

对于使用内置原生包（如 `gfx`）的程序，`goose --gfx-link cc`（或 `msvc`）会打印出供链接器使用的响应文件路径（`@$(goose --gfx-link cc)`）。

---

## 练习

1. **检查编译器统计信息与运行期栈统计：**将第二部分或第三部分中的任意完整示例保存为 `demo.goose`，然后运行：
   ```bash
   goose check --stats --stacks demo.goose
   goose run -DGS_STACK_STATS=1 demo.goose
   ```
   对比 `--stacks` 给出的静态数据栈数量与 `GS_STACK_STATS=1` 在 stderr 上报告的实际运行期使用情况。
2. **测试命名空间 UFCS 与显式 `::`：**扩展 16.2 节的示例，在 `namespace geo;` 内新增 `struct Box { lo: Pt, hi: Pt }` 和 `fn width(b: Box) -> i64`，然后在 `fn ::main()` 中验证 `b.width()` 无需 `geo::` 前缀即可解析，而构造 `geo::Box { ... }` 时需要带上命名空间前缀。
3. **创建一个双文件本地包：**在一个临时目录中创建 `greet/greet.goose`（`namespace greet; fn hello(name: const u8[:]) -> u8[>..] { str("hi, ", name) }`）和 `main.goose`（`import greet from "./greet"; fn main() { print(greet::hello("goose")); }`），然后运行 `goose main.goose`。

---

## 本章要点

- 程序从单个**根文件**出发进行全程序编译；`import` 只会加载每个文件一次，且被导入文件中的 `fn main()` 会被自动忽略。
- **命名空间**（`namespace foo;`、`foo::name`、`::global`、文件级别名）能整洁地组织名称，同时让 UFCS 和 `format` 自动跟随值的类型解析。
- 编译期静态资源使用 **`embed_file`** / **`embed_shader`**，随程序分发的运行期文件使用 **`resource_dir()`**（来自 `os`）。
- **包采用扁平结构并在根文件中锁定版本**（`import lib from "owner/repo@ref"` 或 `"../local"`），统一缓存于 `~/.goose`，并可通过可选的 `.build` 伴生文件集成 C 源码或 CMake 目标。
- 单一的 `goose` 命令行工具涵盖 JIT 运行、原生构建、C 代码输出、代码格式化、Shell 补全、诊断选项（`--stacks`、`--bce-lines`、`--stats`、`--diagnostics json`）以及 `goose lsp`。

在**第 17 章：标准库导览**中，我们将全面巡礼 `stdlib/` 下随编译器附带的全部 20 个模块——它们全部使用清晰易读的 Goose 编写——让你在进入第五部分的性能工程之前，对触手可及的工具箱了然于胸！
