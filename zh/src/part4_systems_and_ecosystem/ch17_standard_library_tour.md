# 第 17 章：标准库导览

Goose 在 `stdlib/` 下附带了 **20 个标准库模块**，几乎全部采用清晰易读的 Goose 代码编写。每当你想知道在 Goose 中如何以地道的方式组织数据结构或解析器时，打开 `stdlib/dictionary.goose`、`stdlib/json.goose` 或 `stdlib/regex.goose` 都是最好的参考资料。

每个模块都遵循一致的约定：

- 原地读取或修改元素 → `T[:]`
- 扩容或缩容数组 → `A&`（对数组种类泛型）
- 返回全新集合 → 在调用方的目标处直接构造的 `T[>..]`
- “可能存在的元素” → `T?`；位置索引 → `i64`（不存在时为 `-1`）；双结果标量 → 尾随 `bool`
- 文本输出追加到 `u8[>..]&` 构建器中；`str(...)` 构建新文本

| 全局命名空间模块 | 独立命名空间模块（`ns::name`） |
|---|---|
| `std`、`dictionary`、`slot_pool`、`slice_pool`、`vec`、`mat`、`quat`、`math`、`os` | `exception`、`ffi`、`binary`、`base64`、`csv`、`json`、`regex`、`gfx`、`audio`、`physics`、`ui` |

学完本章后，你将能够：

- 立即上手使用 `std` 算法、位运算、`rng`、`ByteSet` 以及 `dictionary<K, V>`。
- 使用 `vec`、`mat`、`quat` 和 `math` 进行 2D/3D 线性代数运算，且不会混淆逐元素乘法 `*` 与矩阵/四元数乘法 `mul`。
- 借助 `binary`、`base64`、`csv`、`json` 和 `regex` 解析、转换并输出外部数据。
- 了解何时引入 `os` 以及四个多媒体与仿真包（`gfx`、`audio`、`physics`、`ui`）。

---

## 17.1 核心工具（`std`）与哈希表（`dictionary`）

### `std` 精华功能

除了第 4 章介绍的字符串辅助函数（`trim`、`split`、`parse_int`、`each_utf8`）之外，`std` 还提供：

- **标量与位运算：** `min`、`max`、`clamp`、`abs`、`sign`、`lerp`、`div_euclid`、`rem`、`isqrt`、`gcd`、`lcm`、`is_pow2`、`next_pow2`、`ilog2`、`popcount`、`clz`、`ctz`、`rotl`、`rotr`、`byte_swap`、`swap`。
- **哈希与伪随机数生成器（PRNG）：** `hash`（支持整数、浮点数、`bool`、`u8[:]`）、`hash_combine`，以及 `rng`（基于 splitmix64：`rand_u64`、`rand_range`、`rand_flt`、`rand_gaussian`、`shuffle`）。
- **查找、排序与堆：** `find`、`position`、`contains`、`any`、`all`、`count`、`lower_bound`、`binary_search`、`sort`、`stable_sort`、`reverse`、`dedup`、`retain`、`filter`、`map`、`fold`、`heap_push`、`heap_pop`。
- **SIMD 字节扫描：** `ByteSet`（`byte_set(",\"\n")`）配合 `find_any` 与 `find_pair`（在 x86-64 上通过 SSE2 每次扫描 16 字节）。

### `dictionary<K, V>`

`dictionary<K, V>` 是一个采用 31 位哈希标签与后移删除（backward-shift deletion）的开放寻址哈希表。`K` 和 `V` 都必须是固定大小类型（`const u8[:]` 切片或 `u8[..k]` 定容小字符串非常适合作为零拷贝键；`dictionary<K, bool>` 可用作集合）。对于自定义结构体键，只需利用 `hash_combine` 定义 `fn hash(k: MyKey) -> u64` 即可。

> **注意：** `dictionary` 内部的存储槽位数组是增缩数组（`[>..<]`），因此在调用 `d.insert`、`d.update`、`d.remove` 或 `d.clear` 之前，务必先用完 `d.get(k)` 返回的引用——或者直接使用 `d.update(k, default_val) { it += 1; }` 在单次调用中完成查找与修改。

```goose
import std;
import dictionary;

struct GridKey { x: i32, y: i32 }
fn hash(k: GridKey) -> u64 { hash_combine(hash(k.x), hash(k.y)) }

fn main() {
    // Bit ops & integer math:
    print("popcount=", popcount(0xF0F0), " ilog2(100)=", ilog2(100),
          " isqrt(50)=", isqrt(50), " gcd=", gcd(-12, 18));

    // Deterministic RNG + shuffle:
    var r = rng { 42 };
    var deck: i64[6] = [1, 2, 3, 4, 5, 6];
    deck.shuffle(r);
    print("shuffled=", deck, " roll=", r.rand_range(1, 6));

    // SIMD-accelerated ByteSet search:
    let delims = byte_set(",;:");
    let sample = "alpha;beta,gamma";
    print("first delim at=", find_any(sample, delims));

    // Dictionary with custom struct key:
    var cells = dictionary<GridKey, i32> {};
    cells.insert(GridKey { 1, 2 }, 10);
    cells.update(GridKey { 1, 2 }, 0) { it += 5; };
    let v = cells.get(GridKey { 1, 2 });
    if v { print("cell(1,2)=", v); }
}
```

```
popcount=8 ilog2(100)=6 isqrt(50)=7 gcd=6
shuffled=[5, 4, 1, 3, 6, 2] roll=1
first delim at=5
cell(1,2)=15
```

---

## 17.2 数学与线性代数（`vec`、`mat`、`quat`、`math`）

Goose 内置了受 GLSL/GLM 启发的 2D 与 3D 数学模块——全部对 `checkfree` 友好（零残留越界检查！）：

- **`vec`：** `vec2<T>`、`vec3<T>`、`vec4<T>`，以及别名 `float2/3/4`、`double2/3/4`、`int2/3/4`、`dot`、`cross`、`length`、`normalize`、`distance`、`lerp`、`xy`、`xyz`。
- **`mat`：** 列主序的 `float2x2`、`float3x3`、`float4x4`（以及 `double*`）、`translate`、`rotate`、`scale`、`perspective`、`ortho`、`look_at`、`transpose`、`inverse`、`transform_point`、`transform_vector`。
- **`quat`：** `floatq` / `doubleq`、`angle_axis`、`rotate`、`slerp`、`from_euler`、`mat4_cast`、`compose`。
- **`math`：** 完整的 `f32`/`f64` libm 函数（`sqrt`、`sin`、`cos`、`atan2`、`pow`、`exp`、`log`、`floor`、`ceil`、`mul_add`）以及 `PI` 和 `radians`。

> **注意：** 由于 `*` 是 Goose 内置的针对结构体和定长数组的**逐元素**运算符，`m1 * m2` 会按分量逐项相乘（类似 GLSL 中的 `matrixCompMult`）。进行矩阵乘法和四元数哈密顿积运算时，请始终使用 **`mul(m1, m2)`**！

```goose
import std;
import vec;
import mat;
import quat;
import math;

fn main() {
    let p = float3 { 1.0, 0.0, 0.0 };
    let q = angle_axis(radians(90.0 as f32), float3 { 0.0, 0.0, 1.0 });
    let rotated = rotate(q, p);

    // Compose translate + rotate into a 4x4 matrix and transform `p`:
    let model = mul(translate(float3 { 10.0, 0.0, 0.0 }), mat4_cast(q));
    let world = transform_point(model, p);

    var out: u8[>..] = [];
    format(out, "rot=("); format_flt(out, rotated.x, 2); format(out, ",");
    format_flt(out, rotated.y, 2); format(out, ") world=(");
    format_flt(out, world.x, 2); format(out, ","); format_flt(out, world.y, 2);
    format(out, ")");
    print(out);
}
```

```
rot=(0.00,1.00) world=(10.00,1.00)
```

---

## 17.3 外部数据格式与正则表达式（`binary`、`base64`、`csv`、`json`、`regex`）

五个专注且带有独立命名空间的模块以零堆分配的方式处理日常二进制和文本格式：

- **`binary`：** 带边界检查的小端与大端读写（`read_u16le`、`read_u32be`、`write_u64le`、`reader`、`subreader`），适用于 PNG、WAV 或 Doom WAD 等外部文件格式（第 13 章就曾用 `binary` 处理我们的 `"GSX1"` 文件头）。
- **`base64`：** 遵循 RFC 4648 的 `base64::encode(out, src)` 与 `base64::decode(out, src) -> bool`，以自动向量化的 `simd fn` 实现。
- **`csv`：** 遵循 RFC 4180 的零拷贝 `csv::each_record(text) { fields => ... }`（每条记录复用同一个切片缓冲区！）以及 `csv::format_field(out, s)`。
- **`json`：** 单模块提供三种风格：
  1. **流式写入器**（`json::format_key`、`format_string`、`format_number`、`format_int`、`format_bool`）。
  2. **文档树**（`json::parse(text, doc, decoded)` 推入自相对的 `json::Node`，配合 `member` / `each` / `as_*`）。
  3. **零分配流式读取器**（`json::reader(text)` + `each_element` / `each_member` / `read_int` / `skip` / `finish`）。
- **`regex`：** 基于 DFA 的线性时间正则表达式（`regex::compile`、`find`、`matches`、`each_captures`、`format_replaced`、`regex::group`），使用 SIMD `ByteSet` 预过滤器快速跳过不匹配的区域。

```goose
import std;
import base64;
import csv;
import json;
import regex;

fn main() {
    // 1. Base64 round-trip:
    var b64: u8[>..] = [];
    base64::encode(b64, "goose!");
    var raw: u8[>..] = [];
    let b64_ok = base64::decode(raw, b64);
    print("b64: ", b64, " -> ", raw, " (ok=", b64_ok, ")");

    // 2. Zero-copy RFC 4180 CSV with quoted comma:
    let table = """
        item,price
        "bolt, 6mm",1.50
        nut,0.25
        """;
    var sum = 0.0;
    let csv_ok = csv::each_record(table) { cols =>
        let p, ok = parse_flt(cols[1]);
        if ok { sum += p; }
    };
    print("csv ok=", csv_ok, " sum=", sum);

    // 3. Streaming JSON reader (zero tree nodes built!):
    var jr = json::reader("""[{"id":1,"ok":true},{"id":2,"ok":false}]""");
    var ok_ids: i64[>..] = [];
    json::each_element(jr) {
        var id = 0;
        var flag = false;
        json::each_member(jr) { key =>
            if key == "id"      { id = json::read_int(jr); }
            else if key == "ok" { flag = json::read_bool(jr); }
            else                { json::skip(jr); }
        };
        if flag { ok_ids.push(id); }
    };
    assert(json::finish(jr));
    print("json ok_ids=", ok_ids);

    // 4. DFA Regex captures & template replacement:
    let re = regex::compile("""(\w+)=(\d+)""");
    var replaced: u8[>..] = [];
    regex::format_replaced(replaced, re, "x=10 y=25") { c =>
        str(regex::group(c, 1), ":", regex::group(c, 2))
    };
    print("regex: ", replaced);
}
```

```
b64: Z29vc2Uh -> goose! (ok=true)
csv ok=true sum=1.75
json ok_ids=[1]
regex: x:10 y:25
```

> **提示：** 请在搜索循环外将每个 `Regex` **只编译一次**（`regex::compile` 会预先构建正向与反向 DFA，使每次搜索每字节只需一次查表）。

---

## 17.4 系统服务（`os`）与交互式包（`gfx`、`audio`、`physics`、`ui`）

### `os`

`import os;` 提供可移植的操作系统服务（位于全局命名空间）：

- **命令行参数：** `args()`、`arg_count()`、`arg(i, out)`
- **文件与目录：** `read_file`、`write_file`、`append_file`、`write_file_atomic`、`rename_file`、`delete_file`、`file_exists`、`list_dir`、`resource_dir`
- **时间、熵源、内存与标准错误输出：** `clock()`（高精度秒数）、`sleep_ms`、`random_seed()`、`trim_memory()`、`write_stderr`

### 四个多媒体与仿真包

Goose 还随附四个官方原生包（已预编译进发行版二进制文件及 `-DGOOSE_BUILTIN_LIBS=all` 构建中）：

- **`gfx`（SDL3 GPU API）：** 跨 Vulkan、Metal 与 Direct3D 12 的现代跨平台 2D/3D GPU 渲染与计算着色器，支持编译期 `embed_shader`（GLSL → SPIR-V/MSL/HLSL）、键盘/鼠标/手柄输入，以及无头离屏渲染（`gfx::open_headless` / `GOOSE_GFX_HEADLESS=1`）。
- **`audio`（SDL3 PCM 混音器）：** 程序化单声道/立体声播放，无论有无图形窗口均可使用。
- **`physics`（Box3D）：** 多线程 3D 刚体物理引擎，支持面向实例化渲染的批量变换获取。
- **`ui`（基于 `gfx` 的 Nuklear 与字体烘焙）：** 清晰的 TTF 文本渲染、非交互式游戏 HUD（`ui::label`、`ui::draw_text`），以及完整的即时模式窗口、按钮、滑块和拾色器。

我们将在**第 22 章（综合实战 3）**中使用这些包构建一个完整的交互式应用程序！

---

## 17.5 综合实例：CSV 到 JSON 遥测流水线

让我们将 `csv`、`regex`、`base64`、`dictionary` 和 `json` 串联起来，编写一个贴近实战的零堆分配 ETL 脚本：读取 CSV 行，用已编译的 DFA 正则表达式提取 `env=...` 标签，对令牌进行 Base64 编码，统计各环境的记录数量，并格式化输出正确转义的 JSON 报告：

```goose
import std;
import csv;
import regex;
import base64;
import json;

fn main() {
    let input_csv = """
        svc,meta,token
        "api,v2","env=prod region=us",tok-1
        worker,"env=staging region=eu",tok-2
        auth,"env=prod region=eu",tok-3
        """;

    let env_re = regex::compile("""env=(\w+)""");

    var out: u8[>..] = [];
    out.push('[');
    var first = true;
    var row_num = 0;

    let ok = csv::each_record(input_csv) { cols =>
        row_num++;
        guard row_num > 1 && cols.len == 3;          // skip header row

        var env_val: u8[..16] = "unknown";
        regex::each_captures(env_re, cols[1]) { c =>
            env_val.clear();
            format(env_val, regex::group(c, 1));
        };

        var encoded_tok: u8[>..] = [];
        base64::encode(encoded_tok, cols[2]);

        if !first { out.push(','); }
        first = false;
        out.push('{');
        json::format_key(out, "svc");   json::format_string(out, cols[0]); out.push(',');
        json::format_key(out, "env");   json::format_string(out, env_val); out.push(',');
        json::format_key(out, "token"); json::format_string(out, encoded_tok);
        out.push('}');
    };
    out.push(']');
    assert(ok);

    // Parse back with `json::parse` to prove our generated JSON is 100% valid!
    var doc: json::Node[>..] = [];
    var decoded: u8[>..] = [];
    let root, err = json::parse(out, doc, decoded);
    guard root else { abort(err); }

    print("generated: ", out);
    var prod_count = 0;
    json::each(root) { item =>
        let e, has_e = json::as_string(json::member(item, "env"));
        if has_e && e == "prod" { prod_count++; }
    };
    print("parsed cleanly; prod services=", prod_count);
}
```

```
generated: [{"svc":"api,v2","env":"prod","token":"dG9rLTE="},{"svc":"worker","env":"staging","token":"dG9rLTI="},{"svc":"auth","env":"prod","token":"dG9rLTM="}]
parsed cleanly; prod services=2
```

注意这五个标准库模块组合起来是多么游刃有余：`csv::each_record` 产生零拷贝切片，`regex::each_captures` 用 DFA 提取捕获组，`base64::encode` 用 SIMD 编码，`json::format_*` 干净地转义并写入同一个 `u8[>..]` 构建器，最后 `json::parse` 将输出校验并解析为一棵自相对的 `json::Node[>..]` 树！

---

## 练习

1. **测试 `json::parse` 的错误报告：** 在 17.5 节的示例中故意破坏生成的 JSON（例如省略末尾的 `]`），并查看 `json::parse` 返回的精确字节偏移诊断信息（不要中止程序——改为打印 `err`）。
2. **使用 `std` 中的二叉堆：** 导入 `std`，使用 `heap_push(h, x)` 将 `[40, 10, 30, 50, 20]` 压入 `var h: i64[>..<] = [];`，再用 `heap_pop(h)` 弹出全部五个元素，验证最大堆（或自定义比较器的最小堆）的出堆顺序。
3. **探索 `std/regex` 的捕获切片：** 编写一段代码，对 `"a=1 b=2 c=3"` 调用 `regex::each_captures`，并使用 `line[c.slots[2]..c.slots[3]]` 将每个键存入 `dictionary<const u8[:], i64>`（回顾为何 `regex::group(c, 1)` 以 `c` 为根，而通过 `c.slots` 对 `line` 切片可以活得比代码块更久！）。

---

## 本章要点

- Goose 的 **20 个标准库模块**均用易读的 Goose 代码写成，统一遵循切片（`T[:]`）、构建器（`u8[>..]&`）、可选引用（`T?`）与尾随 `bool` 约定。
- **`std`** 与 **`dictionary`** 涵盖位运算、PRNG、SIMD `ByteSet` 查找、排序/堆以及开放寻址哈希表。
- **`vec`**、**`mat`**、**`quat`** 和 **`math`** 提供 `checkfree` 的 2D/3D 线性代数支持（矩阵与四元数乘法请使用 `mul(a, b)`；`*` 为逐元素乘法）。
- **`binary`**、**`base64`**、**`csv`**、**`json`** 和 **`regex`** 以零堆分配方式处理外部数据格式，而 **`os`**、**`gfx`**、**`audio`**、**`physics`** 和 **`ui`** 提供可移植的操作系统服务与交互式多媒体能力。

至此，**第 IV 部分：并发、互操作与生态**全部结束！在**第 V 部分：性能工程**中，我们将从**第 18 章：索引类型、越界检查消除与 `checkfree`** 开始，学习如何在热点循环和图遍历中证明零越界检查残留！
