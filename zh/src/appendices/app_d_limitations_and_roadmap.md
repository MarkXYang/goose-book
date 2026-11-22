# 附录 D：语言现状、局限与延伸阅读

Goose 是一门年轻且处于积极开发中的系统编程语言，其编译器、语言规范、标准库、基准测试以及 31 个示例程序全部集中在一个透明的仓库中。本附录记录了当前实现的精确范围、v1 版本中刻意省去的特性、正在推进的设计方向，以及下一步的阅读资料。

---

## 1. 编译器与平台状态

本书中每一个可运行及原生构建的示例均在以下环境下测试通过：

- **编译器：**`goose v2026.4`（全程序 C++ 编译器，约 3 万行代码，采用 Apache-2.0 许可证）
- **平台：**Linux、macOS 和 Windows（64 位小端序 x86-64 与 ARM64）
- **后端：**
  - 内置 **TinyCC（libtcc）** 进程内 JIT（`goose file.goose`），无需额外工具链即可即时运行。
  - 原生 **C99** 代码生成（`goose build` / `goose -o out.c`），支持 **clang**、**GCC** 和 **MSVC**（在 x86-64 上使用 clang 时支持 `simd fn` 的 AVX2/AVX-512 多版本分发）。
- **内置包：**`gfx`（SDL3 GPU + 编译期 GLSL → SPIR-V/MSL/HLSL `embed_shader`）、`audio`（SDL3 PCM 混音器 + 离线渲染器）、`ui`（Nuklear 控件、字体与 HUD）以及 `physics`（Box3D 刚体模拟）。
- **编辑器支持：**内置 `goose lsp` 语言服务器协议（LSP）服务和 `goose format` 格式化工具，并提供官方 VS Code 扩展（`vscode/goose-language.vsix`）以及 Neovim、Helix 和 Emacs 的配置。

---

## 2. v1 刻意省去的特性与当前局限

了解 Goose 当前*不包含*哪些内容，有助于避免浪费时间去寻找那些刻意排除在范围之外或仍在路线图上的特性：

| 领域 | 当前状态 / 局限 | 当前实用替代方案 |
|---|---|---|
| **可调整大小类型的移动语义** | 非固定大小的局部变量没有隐式移动语义；重新赋值或返回两个不同的局部变量需要使用 `copy(x)`。 | 采用目标处直接构造（`let x = f()`、`v.push(f())`），并在每个函数中只返回一个具名局部变量。 |
| **普通结构体中的多个可调整大小字段** | 普通 `struct` 最多只能包含一个可调整大小字段（且必须作为最后一个字段）。 | 使用根结构体（`root struct`）将多个独立存储的可调整大小数组打包到同一个所有权生命周期下。 |
| **嵌套拥有关系的 `root struct` 字段** | 目前 `root struct` 的所有者必须是局部变量或全局声明（不能被另一个 `root struct` 所拥有）。 | 将同级的 `root struct` / `slot_pool` / `slice_pool` 变量并列声明，并通过引用传递。 |
| **跨线程队列的相对引用** | 包含相对引用的值不是扁平的，不能直接跨越 `qput`/`qget` 传递。 | 使用 `to_bytes(a)` 将其序列化为扁平的 `u8[]` 消息，在工作线程中再通过 `from_bytes<A>` 重新加载。 |
| **带标签的 `break` / `continue`** | v1 中未包含。 | 将内层循环提取为嵌套辅助函数（或使用 `block { ... }` 配合提前 `break` / `return`）。 |
| **模块 / 命名空间可见性控制** | v1 中没有 `pub`/`private` 访问修饰符；命名空间用于清晰地组织名称，但不强制执行可见性限制。 | 将内部辅助项放在子命名空间中（例如 `detail::helper`），或嵌套在公开函数内部。 |
| **网络、HTTP 与子进程** | 不属于 v1 的 `stdlib/os.goose`。 | 通过 `extern fn`、`extern "lib" { ... }` 或原生包 `.build` 伴随文件（见第 15 章）简洁地绑定 C 库。 |
| **已弃用的 `reusable` / `reusable[]`** | 目前仅为兼容旧代码而保留，计划移除。 | 改用标准库中的 `slot_pool<T>`（槽位池，用于独立槽位）和 `slice_pool<T>`（切片池，用于连续片段）。 |
| **大端序宿主机** | 序列化（`to_bytes`/`from_bytes`）与 C 代码生成目前仅面向 64 位小端序宿主机。 | 与大端序外部传输格式交换数据时，使用 `binary` 模块（`read_*be` / `write_*be`）。 |
| **越界检查消除（BCE）局限** | 普通 `u64` 变量（非索引类型）、任意数组元素内容以及 `while` 退出条件中的析取（逻辑或）不会被追踪。 | 计数器保持使用 `i64`，已验证的索引以 `u32 in arr` 形式存储，并在入口处添加 `assert`。 |

---

## 3. 活跃研究与路线图（`docs/design/`）

Goose 的设计笔记记录了下一阶段语言与工具链能力的演进方向，同时始终坚守其零 GC、无需生命周期标注的基石：

- **`par for` / 结构化子线程：**分叉-合并（fork–join）风格的数据并行机制，能够安全地借用父数据栈的只读视图，无需通过队列复制庞大的输入数据。
- **协程：**遵循栈纪律的生成器与状态机，无需在堆上分配 Future 对象。
- **无析构函数的所有权：**实现非固定大小值的零拷贝转移，同时不引入 RAII 析构胶水代码或借用检查器标注。
- **`goose dap`：**调试适配器协议（DAP）服务器，用于在 VS Code、Neovim 等编辑器中进行源码级单步调试。
- **WebAssembly 目标平台：**将 Goose 编译为紧凑、性能可预测的 Wasm 模块。

---

## 4. 延伸阅读与一手资料

所有权威文档均托管在 Goose 官方仓库（`github.com/aardappel/goose`）中：

### 核心语言文档
- **教程（`docs/tutorial.md`）：**以示例驱动的简明导览，介绍 Goose 最具特色的理念。
- **语言参考手册（`docs/reference.md`）：**按主题编排的完整手册，每一项语法结构都附带可编译示例。
- **语言规范（`docs/goose_spec.md`）：**关于类型、生命周期、求值顺序、C 语言表示以及开放议题（`Appendix B`）的形式化规则。
- **标准库（`docs/stdlib.md`）：**涵盖全部 14 个标准库模块（`std`、`slot_pool`、`slice_pool`、`dictionary`、`vec`、`mat`、`quat`、`math`、`os`、`ffi`、`binary`、`base64`、`csv`、`json`、`regex`、`exception`）以及 `gfx`、`audio`、`ui` 和 `physics` 的完整 API 文档。

### 工具链、内部实现与基准测试
- **命令行（`docs/command_line.md`）与包管理（`docs/packages.md`）：**完整的 CLI 参数、`.build` 伴随文件、C 互操作头文件以及包的发布流程。
- **编辑器配置（`docs/editors.md`）：**在 VS Code、Neovim、Helix 和 Emacs 中配置 `goose lsp`。
- **编译器实现（`docs/implementation.md`）：**逐趟（pass-by-pass）解析编译器内部结构、BCE 抽象域、C 代码生成、运行时以及第 9 节（*编写高性能 Goose 代码*）。
- **基准测试（`bench/summary.md`、`bench/results.md`、`bench/design.md`）：**涵盖全部 16 项基准测试与三种输入规模，与惯用 C++、专家级 C++ 及安全 Rust 的完整对比结果。

### 推荐延伸阅读的示例程序（`samples/`）
如果你想在第六部分综合实战项目之外，进一步研读更大规模的惯用 Goose 程序，建议阅读仓库中的以下四个示例：
1. **`samples/18_json.goose`** —— 完整的 JSON 解析器，支持 `\uXXXX` UTF-8 解码与美化输出。
2. **`samples/26_file_tree.goose`** —— 结合 `slot_pool`（槽位池）与 `slice_pool`（切片池），并使用 4 字节 `in pool` 链接构建的目录树。
3. **`samples/30_mini_minecraft.goose`** —— 完整可玩的体素游戏，包含程序化地形、区块网格生成、光照、合成、昼夜循环以及受验证的原子存档——且零外部资源文件。
4. **`samples/31_mini_doom.goose`** —— 可玩的 Freedoom E1M1 引擎，展示了带检查的 WAD 解析（`binary`）、采用调用者自有暂存区的递归 BSP 地面裁剪、战斗系统以及 `audio` 音效。
