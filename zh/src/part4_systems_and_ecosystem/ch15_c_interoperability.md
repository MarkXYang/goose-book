# 第 15 章：与 C 语言互操作

没有任何一门系统编程语言能够孤立存在。无论你是需要经过实战检验的压缩库、操作系统 API、GPU 驱动程序，还是想将高速 Goose 内核嵌入到现有的 C、C++ 或 Python 应用程序中，跨越 C 边界都必须快速且可预测。

由于 Goose 直接编译为 C，调用 C 代码不需要任何沉重的 FFI 桥接层——每一次调用都会编译为一次**直接的 C 函数调用**。Goose 支持**双向** C 互操作：

1. **`extern fn`** —— 绑定静态 C 符号（系统库、`libm`，或列在 `.build` 附属配置文件中的 C 源文件）。
2. **`extern "lib" { ... }`** —— 在程序启动时按名称动态加载共享库，**无需任何 C 工具链**。
3. **`export fn`** —— 将 Goose 程序编译为独立的 C 模块或共享库（`.so` / `.dylib` / `.dll`），并自动生成 `.h` 头文件。

学完本章后，你将能够：

- 声明 `extern fn` 绑定，并了解哪些 Goose 类型可以直接跨越 C ABI。
- 使用 `.build` 附属配置文件清晰地打包你自己的 C 源文件与头文件。
- 借助 `extern "lib" { ... }`、`pad` 和 `ffi` 模块动态绑定共享库。
- 使用 `export fn`、`--header`、`--shared`、`goose_init` 和 `goose_exit` 将 Goose 函数导出给 C 宿主程序调用。

---

## 15.1 使用 `extern fn` 调用 C 函数

`extern fn` 是一个顶层、具有完整类型签名、非泛型且没有函数体的函数声明。默认情况下，它会绑定到同名的 C 符号；添加 `= "c_symbol"` 即可绑定到不同的 C 符号名：

```goose
import std;

// Bind directly to C99 <math.h> symbols (available in both `goose run` and `goose build`):
extern fn cube_root(x: f64) -> f64 = "cbrt";
extern fn hypot(x: f64, y: f64) -> f64;
extern fn scale_pow2(x: f64, exp: i32) -> f64 = "ldexp";

fn main() {
    var root_text: u8[>..] = [];
    format_flt(root_text, cube_root(27.0), 3);
    print("cbrt(27)=", root_text, " hypot(3,4)=", hypot(3.0, 4.0),
          " ldexp(1.5, 4)=", scale_pow2(1.5, 4));
}
```

```
cbrt(27)=3.000 hypot(3,4)=5.0 ldexp(1.5, 4)=24.0
```

### 哪些类型可以跨越边界？

Goose 拒绝静默地错误编组复杂类型——只有具备简单、明确 C 内存布局的类型才允许出现在 `extern fn` 声明中：

| Goose 类型 | C 表示形式 |
|---|---|
| `i8..i64`、`u8..u64`、`f32`、`f64`、`bool` | 对应的 C 定宽整数、`float`、`double`、`bool` |
| 扁平固定大小 `struct`、`T[k]`、`T[..k]`、固定模式 `enum` | 按值传递的紧凑（packed）C 结构体 |
| `T&` / `const T&`（适用于扁平固定大小 `T`） | `T *` / `const T *` 指针 |
| `T[:]` / `const T[:]`（适用于扁平固定大小 `T`） | `struct { T *data; int64_t len; }` |
| `T[>..]&`（特别是 `u8[>..]&`） | `gs_rref` 构建器句柄（在 C 中通过 `gs_bld_append(out, ptr, len)` 追加数据） |
| 返回类型 | 单个标量、单个扁平固定大小值，或无返回值（void） |

具有 Goose 专属内存布局的类型——如 `varint`、可选引用 `T?`、按值传递的变长类型（`u8[]`）或包含引用的结构体——会在声明处被直接拒绝：

<!-- compile-fail -->
```goose
extern fn bad_c_fn(s: u8[]) -> i32;  // error: variable-size type cannot cross extern fn by value

fn main() {
    print(bad_c_fn("hi"));
}
```

> **安全契约：** 将只读切片或引用参数标记为 `const`，这样 Goose 才允许你将字符串字面量和 `const` 数据传入 C。在 `extern fn` 返回之后，C 代码**绝对不能**继续保留指向借用的 Goose 切片或引用的指针。

---

## 15.2 使用 `.build` 附属文件打包自定义 C 代码

当你的 Goose 程序包含自己的 C 文件时，只需在根文件 `foo.goose` 同目录下放置一个名为 `foo.build` 的**附属配置文件（sidecar file）**：

```text
my_app/
  app.goose
  app.build
  impl.c
  include/
    adapters.h
```

`.build` 文件采用简洁的类 Goose 语法（`//` 注释、`key = value;`、字符串列表），并且每个 `c_*` 键都支持 `_linux`、`_macos` 和 `_windows` 平台后缀：

```text
// app.build
c_sources = ["impl.c"];
c_include = ["include"];
c_headers = ["include/adapters.h"];     // spliced into generated C for inline adapters
c_defines = ["FAST_CRC=1"];
c_flags_cc = ["-O3"];                   // gcc / clang flags (c_flags_msvc for MSVC)
c_libs = ["m"];
c_libs_windows = ["user32"];
```

`goose build app.goose` 和 `goose run app.goose` 都会自动读取 `app.build`：`goose build` 会将 `impl.c` 编译并链接进你的可执行文件，而 `goose run` 则会将 `impl.c` 一次性构建为一个缓存的共享库（位于 `~/.goose` 下），并在进程内由 TinyCC 运行时加载。

### 针对 Goose 切片与构建器编写 C 代码

在通过 `c_headers`（或 `--include shim.h`）引入的头文件中，C 代码可以直接访问 Goose 切片（带有 `.data` 和 `.len` 字段的 `sl_u8` / `sl_i32`），通过指针写入 Goose 结构体（Goose 会为用户类型名和字段名加上 `_g` 后缀），并使用 `gs_bld_append` 向 Goose 的 `u8[>..]&` 只增数组构建器追加字节：

```c
/* include/adapters.h */
#include <string.h>

static uint32_t crc32_bytes(sl_u8 s) {
    uint32_t crc = 0xFFFFFFFFu;
    for (int64_t i = 0; i < s.len; i++) {
        crc ^= s.data[i];
        for (int k = 0; k < 8; k++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

static void c_version(gs_rref out) {
    const char *v = "C adapter 1.0";
    gs_bld_append(out, v, strlen(v));
}
```

在 Goose 一侧，你只需声明对应的函数签名，就能像调用普通 Goose 函数一样调用它们：

<!-- fragment -->
```goose
extern fn crc32_bytes(s: const u8[:]) -> u32;
extern fn c_version(out: u8[>..]&);
```

---

## 15.3 零工具链动态 FFI：`extern "lib" { ... }`

如果你希望调用系统中已安装——或随可执行文件一同分发的——共享库，并且**完全不想编译任何 C 包装代码**，该怎么做呢？

只需编写一个 **`extern "lib" { ... }`** 代码块。在程序启动时，Goose 会动态打开该共享库，并解析你的程序实际调用的每一个符号（未调用的声明会被忽略；如果块中没有任何符号被调用，甚至连共享库都不会被打开！）：

<!-- check-only -->
```goose
import ffi;

struct SDLRect { x: i32, y: i32, w: i32, h: i32 }

extern "SDL2" {
    fn SDL_Init(flags: u32) -> i32;
    fn SDL_Quit();
    fn SDL_GetError() -> u64;                         // returns `const char *` as opaque u64
    fn SDL_CreateWindow(title: const u8&, x: i32, y: i32, w: i32, h: i32, flags: u32) -> u64;
    fn delay(ms: u32) = "SDL_Delay";                  // rename symbol with `= "..."`
}

fn main() {
    if SDL_Init(0x20) < 0 {
        abort(str("SDL error: ", ffi::cstr(SDL_GetError())));
    }
    let title = "Goose Window\0";                     // explicit NUL terminator for C
    let win = SDL_CreateWindow(title[0], 100, 100, 640, 480, 0);
    if win != 0 { delay(100); }
    SDL_Quit();
}
```

### 绑定原生 C 库的核心规则

在不使用 C 适配层、直接绑定 C 共享库时，请牢记以下五条规则：

1. **库名解析与 `.build` 覆盖：** `"SDL2"` 在 Linux 上解析为 `libSDL2.so`，在 macOS 上解析为 `libSDL2.dylib`，在 Windows 上解析为 `SDL2.dll`。包含 `/` 的路径（如 `"bin/steam_api64"`）会相对于 `resource_dir()` 加载。如果某个平台使用带版本号的 soname，应在 `.build` 附属文件中进行映射，而不是硬编码在 Goose 代码里：
   ```text
   dylibs_linux = ["SDL2=libSDL2-2.0.so.0"];
   ```
2. **精确的 C 类型对应：** `int` 对应 `i32`，`unsigned` 对应 `u32`，`size_t` 对应 `u64`，而 C 的 `enum`（包括 `SDL_bool`）对应 **`i32`** 而非 `bool`。应避免使用 C 的 `long`（在 Windows 上为 32 位，在 Linux/macOS 上为 64 位）。
3. **不透明句柄与 C 持有的内存：** 使用 **`u64`** 表示不透明的 C 指针（如 `SDL_Window *`、`FILE *`、`const char *`）。若要将 C 持有的字符串或字节缓冲区安全地读取回 Goose 内存，请导入 `ffi` 并调用 **`ffi::cstr(ptr)`** 或 **`ffi::bytes(ptr, n)`**。
4. **以 NUL 结尾的字符串与 `(ptr, len)` 参数对：** 对于 `const char *`，在字符串末尾追加 `\0` 并将 `title[0]` 传递给 `const u8&` 参数。对于期望接收 `(ptr, count)` 的 C 函数，将 `xs[0]` 和 `xs.len` 作为两个参数传入即可。
5. **使用 `pad` 处理结构体对齐：** 由于 Goose 结构体默认采用紧凑布局（packed），在 C ABI 插入对齐填充的位置，你需要显式添加 `pad n`——并且任何包含 `pad` 的结构体都必须**按引用传递**（`const MyStruct&`），绝不能按值传递（以免填充字节扰乱寄存器传参约定）。

> **何时仍需要极简的 C 适配层：** C 回调函数（向 C 传入函数指针）和 C 可变参数函数（`printf` 风格的 `...`）无法直接在 `extern "lib"` 块中绑定；对于这类接口，可以在 `c_sources` / `c_headers` 中用寥寥几行 C 代码封装一下。

---

## 15.4 从 C 调用 Goose：`export fn`、`--header` 与 `--shared`

`export fn` 是 `extern fn` 的镜像：它会为一个顶层、非泛型的 Goose 函数生成可被 C 调用的包装器，使 C、C++、Rust 或 Python（`ctypes`）宿主程序都能直接调用 Goose 代码！

- 编写 `export fn name(...)` 以 Goose 原名导出，或编写 `export "c_name" fn name(...)` 指定自定义的 C 符号名。
- 参数与返回值遵循与 `extern fn` 相同的 C 兼容类型规则，**外加**一项便捷的扩展：导出函数可以返回 **`const u8[:]`**，C 侧会将其接收为 `const uint8_t *`，并附带一个额外的 `int64_t *out_len` 输出参数（请记住：Goose 字节切片**并不**以 NUL 结尾！）。
- 如果你的 Goose 代码中定义了 `fn main()`，它会在 `goose_init(argc, argv)` 内部执行一次——这使 `main` 成为了库最自然的初始化钩子！

```goose
var call_count: i32 = 0;

export fn greeting() -> const u8[:] {
    call_count += 1;
    return "Hello from Goose!";
}

export "goose_add" fn add(a: i32, b: i32) -> i32 {
    call_count += 1;
    return a + b;
}

export fn total_calls() -> i32 { call_count }

fn main() {
    // Runs once when C calls `goose_init`, or directly when run as a program:
    print(greeting(), " sum=", add(20, 22), " calls=", total_calls());
}
```

```
Hello from Goose! sum=42 calls=2
```

### 构建并从 C 宿主程序调用

你可以通过**两种**方式将 Goose 嵌入到 C 项目中：

**方式 A —— 共享库（`.so` / `.dylib` / `.dll`）：**
```bash
goose build --shared --header mylib.h mylib.goose
```

**方式 B —— 编译进你的 C 项目的独立 C 文件：**
```bash
goose --standalone --header mylib.h -o mylib.c mylib.goose
cc -O2 mylib.c host.c -o app -lm -pthread
```

以下是配套的 C 宿主程序（`host.c`）：

```c
#include "mylib.h"
#include <stdio.h>

int main(int argc, char **argv) {
    goose_init(argc, argv);          /* initializes runtime, globals, and Goose main() */

    int64_t len = 0;
    const uint8_t *msg = greeting(&len);
    fwrite(msg, 1, (size_t)len, stdout);   /* slices are not NUL-terminated! */
    putchar('\n');

    printf("20 + 22 = %d (calls=%d)\n", goose_add(20, 22), total_calls());

    goose_exit();                    /* releases all Goose stacks cleanly */
    return 0;
}
```

> **宿主线程与热重载规则：** 请在调用 `goose_init` 的同一线程上调用导出的函数。调用 `goose_exit()` 会释放所有数据栈和运行时结构，因此宿主程序甚至可以在同一进程中热重载重新编译的 Goose 共享库，并再次调用 `goose_init`（在调用 `goose_exit` 之前，请确保所有 Goose 工作线程均已结束）。

---

## 15.5 实战示例：双向原生几何计算内核

下面是一个简洁、自包含的 Goose 库：它在内部**调用 C**（通过 `extern fn` 调用 `libm`），同时向外**导出**一套整洁、可供 C 调用的几何 API（`export fn`），包含扁平固定大小结构体和切片处理函数——它既可以直接用 `goose` 运行以快速测试，也随时可以通过 `goose build --shared --header geo.h` 打包为共享库分发！

```goose
import std;

extern fn hypot(x: f64, y: f64) -> f64;
extern fn atan2(y: f64, x: f64) -> f64;

struct Vec2 { x: f64, y: f64 }
struct Bounds { min_x: f64, min_y: f64, max_x: f64, max_y: f64 }

var last_label: u8[..48] = "uninitialized";

export "geo_dist" fn distance(a: Vec2, b: Vec2) -> f64 {
    hypot(b.x - a.x, b.y - a.y)
}

export "geo_angle_deg" fn angle_degrees(a: Vec2, b: Vec2) -> f64 {
    atan2(b.y - a.y, b.x - a.x) * 180.0 / 3.141592653589793
}

export "geo_bounds" fn compute_bounds(pts: const Vec2[:], out: Bounds&) -> bool {
    guard pts.len > 0 else { return false; }
    var lo_x = pts[0].x;
    var lo_y = pts[0].y;
    var hi_x = lo_x;
    var hi_y = lo_y;
    for p in pts[1..] {
        if p.x < lo_x { lo_x = p.x; }
        if p.y < lo_y { lo_y = p.y; }
        if p.x > hi_x { hi_x = p.x; }
        if p.y > hi_y { hi_y = p.y; }
    }
    out = Bounds { min_x: lo_x, min_y: lo_y, max_x: hi_x, max_y: hi_y };
    last_label.clear();
    format(last_label, "bounds(", pts.len, " pts)");
    return true;
}

export "geo_status" fn status_text() -> const u8[:] { last_label }

fn main() {
    let pts: Vec2[3] = [
        Vec2 { 0.0, 0.0 },
        Vec2 { 3.0, 4.0 },
        Vec2 { -2.0, 1.0 },
    ];
    var b = default<Bounds>();
    let ok = compute_bounds(pts, b);

    var deg: u8[>..] = [];
    format_flt(deg, angle_degrees(pts[0], Vec2 { 1.0, 1.0 }), 1);

    print("dist=", distance(pts[0], pts[1]), " deg=", deg);
    print("ok=", ok, " bounds=", b, " status=", status_text());
}
```

```
dist=5.0 deg=45.0
ok=true bounds=Bounds { -2.0, 0.0, 3.0, 4.0 } status=bounds(3 pts)
```

请注意 `status_text()` 是如何将 `last_label`（一个全局 `u8[..48]` 定容数组）作为 `const u8[:]` 返回的：正因为 `last_label` 是全局变量，在 `status_text()` 返回给 C 宿主之后，它的地址依然保持有效，直到下一次调用将其更新为止！

---

## 练习

1. **生成真实的 C 头文件与共享库：** 将 15.5 节的代码保存为 `geo.goose` 并运行：
   ```bash
   goose build --shared --header geo.h geo.goose
   ```
   打开生成的 `geo.h`，查看为 `geo_dist`、`geo_bounds` 和 `geo_status` 生成的精确 C 函数原型（注意 `geo_status` 上多出来的 `int64_t *` 长度参数！）。
2. **测试 `extern fn` 的 `const` 强制检查：** 声明 `extern fn mutate_buf(s: u8[:]);` 并尝试调用 `mutate_buf("literal");`。验证 Goose 会在编译期捕获这一 `const` 违规，除非将该外部参数声明为 `const u8[:]`。
3. **直接调用 C 数学函数：** 在 15.1 节的示例中添加 `extern fn floor(x: f64) -> f64;` 和 `extern fn fmod(x: f64, y: f64) -> f64;`，并比较 `fmod(-7.0, 3.0)` 与 Goose 整数运算 `-7 % 3` 的结果。

---

## 本章要点

- **`extern fn`** 可直接绑定 C 符号，支持标量、扁平紧凑固定大小结构体、`T&` 指针、`T[:]`（`{ data, len }`）切片以及 `u8[>..]&` 构建器（`gs_bld_append`）。
- 将程序自带的 C 源文件、头文件搜索路径、内联 `c_headers` 以及各平台链接库标志放在 `foo.goose` 同目录下的 **`foo.build`** 附属文件中。
- **`extern "lib" { ... }`** 在程序启动时以零 C 工具链方式动态加载共享库；使用 `u64` 表示不透明指针，使用 `ffi::cstr` / `ffi::bytes` 将 C 内存拷贝到 Goose，并使用 `pad` 处理 C 结构体对齐。
- **`export fn`** 配合 `--header` / `--shared`，通过 `goose_init(argc, argv)` 和 `goose_exit()` 将 Goose 库暴露给 C 宿主程序。

接下来，在**第 16 章：程序结构、包与工具链**中，我们将退后一步纵览全局，了解多文件 Goose 程序、命名空间、编译期嵌入资源、Git 包以及完整的 `goose` 命令行工具链是如何协同工作的。
