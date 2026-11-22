# 附录 A：快速参考

本附录将 Goose 的关键字、运算符、类型语法、内置函数、CLI 参数以及核心术语汇集于此，便于日常查阅。

---

## 1. 关键字

### 保留关键字

`import` · `namespace` · `struct` · `enum` · `type` · `fn` · `thread_fn` · `export` · `simd` · `recursive` · `let` · `var` · `extern` · `const` · `pad` · `reusable` *（已弃用）* · `if` · `else` · `while` · `for` · `in` · `loop` · `block` · `guard` · `defer` · `match` · `return` · `break` · `continue` · `as` · `true` · `false` · `null` · `self` · `bool` · `varint` · `i8` · `i16` · `i32` · `i64` · `u8` · `u16` · `u32` · `u64` · `f32` · `f64`

### 上下文关键字

除指定的语法位置外，在其余任何位置均视为普通标识符：

| 上下文关键字 | 识别位置 |
|---|---|
| `from` | 位于 `return` 返回值之后：`return err from load;` |
| `upto` | 上界类型：`u32 upto edges` |
| `inline` | 位于 `fn` 之前（不可与 `extern`、`export`、`simd`、`recursive`、`thread_fn` 组合） |
| `checkfree` | 位于 `fn`、`simd`、`inline`、`recursive` 或 `thread_fn` 之前 |
| `root` | 位于 `struct` 之前：`root struct Pool { ... }` |
| `fields`、`variants`、`types` | 位于 `const for` 头部：`const for <N, A> in fields<T>` |
| `has_default`、`same` | 位于 `const if` 条件中：`const if same<A, B>` |

### 函数修饰符顺序

组合使用多个函数修饰符时，必须按以下固定顺序书写：

```text
export | extern  →  checkfree  →  simd  →  inline  →  recursive  →  fn | thread_fn
```

---

## 2. 运算符优先级

结合最紧密的位于最上方（与 Rust 的优先级一致）：

| 优先级 | 类别 | 运算符 |
|---|---|---|
| 1（最高） | 后缀 | `f(…)`、`a[i]`、`a[lo..hi]`、`.field`、`x as T`、`x as! T` |
| 2 | 一元 | `-`、`!`、`~`、`&` |
| 3 | 乘除 | `*`、`/`、`%` *（欧几里得取模 `%`）* |
| 4 | 加减 | `+`、`-` |
| 5 | 移位 | `<<`、`>>` |
| 6 | 按位与 | `&` |
| 7 | 按位异或 | `^` |
| 8 | 按位或 | `\|` |
| 9 | 比较 | `<`、`<=`、`>`、`>=` |
| 10 | 相等性与同一性 | `==`、`!=`、`.==`、`.!=` |
| 11 | 逻辑与 | `&&` |
| 12（最低） | 逻辑或 | `\|\|` |

- **赋值（为语句，而非表达式）：** `=`、`.=`、`+=`、`-=`、`*=`、`/=`、`%=`、`&=`、`|=`、`^=`、`<<=`、`>>=`、`++`、`--`。
- **区间（`a..b`）：** 允许用于切片（`xs[a..b]`）、`for` 头部（`for i in a..b`）和 `match` 分支（`0..10 => ...`）。
- **算术位宽：** 运算按操作数声明的位宽执行（无符号数回绕；有符号数在 `--debug` 模式下检查溢出，在发布模式下回绕）。
- **类型转换：** `as` 在 `--debug` 模式下检查范围；`as!` 显式回绕/截断且不做检查。

---

## 3. 大小类别与数组家族矩阵

### 大小类别

| 大小类别 | 示例 | 存储位置 | 是否可复制？ | 是否可随机索引？ |
|---|---|---|---|---|
| **固定大小（Fixed）** | 标量、固定大小结构体、`T[k]`、`T[..k]`、固定模式 `Enum`、`T&`、`T[:]`、`T&<uN>` | 原生调用栈（或内联于聚合类型中） | 按值隐式复制 *（持有自相对引用的结构体除外）* | 是 |
| **变长（Variable）** | `T[]`、`T[uN]`、`T[varint]`、`T[..]`、变长模式 `Enum..`、变长结构体 | 数据栈（或内联于容器内部） | 显式 `copy(x)` *（若不含自相对引用）* | `T[..]` 与固定大小元素的 `T[]` 支持；变长元素数组仅支持顺序访问 |
| **可调整大小（Resizable）** | `T[>..]`、`T[>..<]`、含可调整大小尾部的结构体、根结构体（`root struct`） | 专用数据栈 | 显式 `copy(x)` *（若不含自相对引用）* | 固定大小元素支持；变长元素仅支持顺序访问 |

### 数组类型

| 语法 | 名称 | 增长 / 收缩 | 容量 / 长度存储 | 说明 |
|---|---|---|---|---|
| `T[k]` | 定长数组 | 均不支持 | 编译期常量 `k` | 内联紧凑布局 `T[k]` |
| `T[..k]` | 静态定容数组 | 支持 `push`/`pop`，上限为 `k` | `u8`/`u16`/`u32`/`u64` 长度 + 内联 `k` 个存储槽位 | 属于固定大小类别！非常适合短键（如 `u8[..32]`） |
| `T[..]` | 运行时定容数组 | 支持 `push`/`pop`，上限为运行时容量 | 数据栈上的 `u32 cap` + `u32 len` + `cap` 个存储槽位 | 容量在创建时确定 |
| `T[]` / `T[uN]` / `T[varint]` | 变长数组 | 不支持（构造后冻结） | 内联 `u32` / `uN` / ULEB128 长度 + 元素 | 适用于内联字符串（`u8[varint]`）与扁平队列负载（`u8[]`） |
| `T[>..]` | 只增数组 | `push`/`append`；仅在无存活视图时可收缩 | 头部 + 数据栈（`push` 时永不移动） | 默认动态容器；引用在 `push` 后依然有效 |
| `T[>..<]` | 增缩数组 | 可自由 `push`/`pop`/`clear`/`resize` | 头部 + 数据栈 | 内部引用仅允许存放在局部变量中（严禁存入字段或数组） |
| `T[:]` / `const T[:]` | 切片 | 读写元素（不可调整大小） | `{ data*, len }`（16 字节） | 指向任意可索引数组的非拥有视图 |

---

## 4. 引用、可选引用、相对引用与索引类型

| 语法 | 含义 | 大小 | 核心规则 |
|---|---|---|---|
| `T&` / `const T&` | 非空引用 | 8 B *（指向可调整大小类型时为 16 B 胖引用）* | 在值上下文中透明；`.=` 绑定/重新绑定，`=` 写入目标；`.==`/`.!=` 比较引用同一性 |
| `T?` / `const T?` | 可选引用 | 8 B（`null` = 0） | 在 `if r`、`while r`、`guard r else`、`assert(r)` 内部类型收窄为 `T&` |
| `T&<u8\|u16\|u32>` | 自相对引用 | 1 / 2 / 4 B | 相对链接自身地址的偏移量；不可复制；可直接通过 `to_bytes`/`from_bytes` 序列化；在字面量中使用 `self` |
| `T&<u8\|u16\|u32>?` | 可选自相对引用 | 1 / 2 / 4 B | 偏移量 `0` 表示 `null`；非空状态下写入零偏移量会被拒绝 |
| `T&<uN in pool>` | 池相对引用 | 1 / 2 / 4 B | 相对全局或根池基址的偏移量；可自由复制；支持可选修饰 `?` |
| `uN in arr` | 索引类型 | 1 / 2 / 4 / 8 B | 经证明满足 `0 <= v < arr.len`；可消除 `arr[v]` 的越界检查 |
| `uN upto arr` | 上界类型 | 1 / 2 / 4 / 8 B | 经证明满足 `0 <= v <= arr.len`；可消除 `arr[lo..hi]` 的切片边界检查 |

---

## 5. 内置函数与数组成员

### 语言内置函数

| 内置函数 | 签名 / 行为 |
|---|---|
| `print(a, b, …)` | 将所有参数加 `\n` 格式化后一次性原子写入标准输出 |
| `str(a, b, …) -> u8[>..]` | 将参数格式化并在目标处直接构造新的字符串 |
| `format(out, a, b, …)` | 将格式化后的参数追加到可增长的 `u8` 数组 `out` 中 |
| `assert(cond)` | 若条件为假或 `null` 则中止程序；触发可选引用类型收窄，并向 BCE 提供静态事实 |
| `abort(msg)` / `exit(code)` | 立即终止程序（永不返回；`abort` 可被 `thread_spawn_contained` 捕获） |
| `copy(x) -> T` | 对已存储的值执行显式 $O(\text{size})$ 复制 |
| `default<T>() -> T` | 生成零初始化并应用字段默认值的固定大小 `T` |
| `to_bytes(a)` / `to_bytes(a, out)` | 生成带帧结构的字节镜像（`ULEB128(byte_len)` + 元素区域） |
| `bytes_of(a) -> const u8[:]` | 返回数组元素区域的零拷贝只读视图 |
| `from_bytes<A>(b) -> A, bool` | 验证不受信任的带帧字节流并构造拥有所有权的数组 `A` |
| `embed_file(path) -> const u8[:]` | 在编译期将文件字节内容嵌入程序 |
| `embed_shader(stage, glsl)` | 在编译期将 GLSL（`"vert"`/`"frag"`/`"comp"`）编译为 SPIR-V/MSL/HLSL |
| `src_file()` / `src_line()` | 编译期文件名与行号（用作参数默认值时捕获调用点位置） |
| `hardware_threads() -> i64` | 建议的工作线程数，受限于 CPU 核心数与栈虚拟地址空间预算 |
| `thread_spawn(f, args…) -> i64` | 启动一个隔离的 `thread_fn` 工作线程 |
| `thread_spawn_contained(f, args…) -> i64` | 启动故障隔离的工作线程，其 `abort` 会被 `thread_wait` 捕获 |
| `thread_wait(id)` / `thread_wait(id, msg) -> bool` | 等待工作线程结束；若故障隔离线程中止则返回 `false` 并将消息写入 `msg` |
| `qput(v) -> bool` | 将扁平值 `v` 入队（若达到 `qlimit` 上限则立即返回 `false`） |
| `qget<T>()` / `qget<T>(ms)` / `qpoll<T>()` | 阻塞式、带超时（`T, bool`）或非阻塞（`T, bool`）出队 |
| `qlimit<T>(n)` | 设置类型 `T` 队列的最大缓冲消息数 |

### 数组成员

`.len` · `.cap` · `.push(v)` *（返回引用）* · `.append(xs)` · `.pop()` · `.pop_n(n)` · `.resize(n)` / `.resize(n, fill)` · `.clear()` · `.index_of(ref)` · `.range_index(slice)` · `.check_length(n)` · `.move_from(other)`

---

## 6. CLI 速查表

| 命令 | 用途 |
|---|---|
| `goose file.goose [-- args]` | 通过 TinyCC JIT 进程内编译并运行（即时反馈，未优化） |
| `goose build file.goose [-o out]` | 调用系统 C 编译器（`-O2`）编译优化后的原生可执行文件 |
| `goose build --debug file.goose` | 启用有符号溢出检查与类型转换范围检查的原生构建 |
| `goose build --shared --header f.h f.goose` | 构建可供 C 调用的共享库并生成 C 头文件 |
| `goose check file.goose` | 执行类型检查、优化与 BCE 而不运行程序 |
| `goose check --stats --bce-lines f.goose` | 输出越界检查消除总数及逐行统计信息 |
| `goose check --stacks --specs f.goose` | 输出静态数据栈数量与单态化特化清单 |
| `goose -o out.c [--standalone] f.goose` | 输出生成的 C99 源码（`--standalone` 会内嵌完整运行时） |
| `goose format [-w] file.goose` | 格式化 Goose 源码（`-w` 原地写回） |
| `goose fetch file.goose` | 将锁定版本的 Git 包依赖下载至 `GOOSE_HOME` |
| `goose lsp` | 通过 stdio 运行语言服务器协议（LSP）服务 |
| `goose toolchain` | 检测系统 C 编译器（`clang`、`gcc`、`cl`）与内置包 |

---

## 7. 精简术语表

- **平衡调用（Balanced call）：** 永远不会使数组在返回时比进入时更短的调用，从而允许调用前已有的视图继续存活。
- **候选根（Candidate）：** 其存储空间可能存放着从容器中加载出的引用目标的变量。
- **分支函数（Case function）：** 按单个枚举变体（如 `Shape.Circle`、`Shape.Rect&`）键控的一组函数重载，通过跳转表进行分发。
- **数据栈（Data stack）：** 一段带有碰撞指针（bump pointer）的虚拟地址空间保留区（默认 2 GB）；所有非固定大小的值都存储在数据栈上。
- **精确根（Exact root）：** 能够证明精确存储同一性的根（`index_of`、相对引用以及栈顶缓存所必需），与之相对的是**非精确根（Inexact root）**（仅提供生命周期上界）。
- **扁平类型（Flat）：** 在任意嵌套深度都不含引用、切片或相对引用的类型（可用于线程队列与 C FFI）。
- **函数值（Function value）：** 作为泛型参数 `F` 传递的编译期函数或尾随代码块 `{ it > 0 }`；永不进行堆分配且不可逃逸。
- **引用持有者（Holder）：** 包含引用或切片的结构体或数组。
- **类型收窄（Narrowing）：** 在 `if`、`while`、`guard` 或 `assert` 之后，将 `T?` 流敏感地收窄为 `T&`。
- **顺序数组（Sequential array）：** 元素为变长类型的数组（如 `T..[>..]`、`u8[][>..]`），支持遍历和相对引用，但不支持随机索引。
- **透明引用（Transparent reference）：** 在所有值表达式（字段读取、算术运算、`=`）中都表现得如同所指向的目标 `T` 一样的 `T&`；只有 `.=` 与 `.==`/`.!=` 操作引用本身。
