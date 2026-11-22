# 第 21 章：综合项目 2 —— 多线程图像处理流水线

图像与信号处理流水线正是系统编程中两大截然不同领域的交汇处：**紧凑且可向量化的内层循环**（哪怕只多出一次错位的越界检查或指针重载，都会毁掉 SIMD 吞吐量）与**多核并行**（共享可变缓冲区极易引入隐蔽的数据竞争与锁争用）。

Goose 以互不干扰的方式同时解决了这两大难题：`checkfree simd fn` 确保内层计算核完全消除越界检查并做好 AVX2/AVX-512 向量化准备，而 `thread_fn` 与基于扁平变长结构体的类型化队列则能以**零共享可变状态**将任务分发到各个 CPU 核心。

在这第二个综合项目中，我们将构建一个两阶段的**并行灰度图像处理流水线**（先进行 3×3 类高斯方框模糊，再进行 3×3 Sobel 边缘检测），完整具备有界队列背压控制、确定性的串行与并行逐像素校验、受控故障隔离以及二进制 PGM（`P5`）图像导出功能。

### 架构概览

```text
                      ┌─── BandJob (y0, rows, mode, haloed u8[]) ───┐
                      │         qlimit<BandJob>(workers * 2)        │
                      ▼                                             ▼
┌──────────────┐   qput()   ┌───────────────────────┐        ┌───────────────────────┐
│  Main Thread │ ─────────► │ Worker 0 (thread_fn)  │  ...   │ Worker K (thread_fn)  │
│  • Split     │            │ checkfree simd fn:    │        │ checkfree simd fn:    │
│    into bands│ ◄───────── │  blur3x3 / sobel3x3   │        │  blur3x3 / sobel3x3   │
│  • Reassemble│  qget()    └───────────────────────┘        └───────────────────────┘
│  • Verify    │                      ▲                                    ▲
│  • Save PGM  │                      └─── BandResult (y0, rows, u8[]) ────┘
└──────────────┘
```

### 本项目综合运用的概念

| 特性 | 章节 | 在本项目中的作用 |
|---|---|---|
| 行切片越界检查消除（BCE）与无检查函数（`checkfree fn`） | 第 18 章 | 编译器证明 3×3 模糊核与 Sobel 核中均不存在任何残留的越界检查。 |
| `simd fn` 与 `inline fn` | 第 19 章 | 在 clang 下为热路径卷积循环生成 AVX2/AVX-512 多版本代码，且没有任何提前退出分支。 |
| 扁平变长结构体（`u8[]`） | 第 14 章 | 每个 `BandJob` 和 `BandResult` 都携带一整段连续的像素条带，仅通过一次 `memcpy` 即可跨队列传输。 |
| `qlimit<T>` 背压控制 | 第 14 章 | 限制排队中的条带数量以保持内存占用有界，同时排空已完成的结果以防止死锁。 |
| `thread_spawn_contained` | 第 14 章 | 隔离非法或不可信的滤镜工作线程，并干净地捕获其中止诊断信息。 |
| `os` 命令行参数与文件 I/O | 第 16–17 章 | 通过可选的 `--out <file.pgm>` 参数导出标准二进制 PGM（`P5`）图像。 |

---

## 1. 无检查 SIMD 行级算子（模糊与 Sobel）

3×3 方框模糊和 3×3 Sobel 边缘检测器都需要根据三个连续行切片（`top`、`mid` 和 `bot`）组成的 3×3 邻域来计算每一个输出像素 `dst[x]`。

将这三个行切片直接传入 `checkfree simd fn`，可以一举获得三大优势：
1. **零越界检查：** 只需一条 `assert(top.len == w && mid.len == w && bot.len == w && dst.len == w)`，Goose 就能在 `for x in 1..w - 1` 循环内静态证明每一次 `top[x - 1]`、`mid[x]` 和 `bot[x + 1]` 访问都是安全的。
2. **对后端友好的向量化：** clang 和 MSVC 都能干净利落地向量化一维行切片模板计算（还记得第 19 章中 `blur` 的经验吗：在 MSVC 上，行切片版本仅耗时 298 ms，而扁平二维索引版本耗时高达 952 ms！）。
3. **精确的整数拓宽：** 通过 `inline fn` 将像素求和拓宽为 `i32`，既避免了 8 位整数回绕，又保持 32 位宽度以获得最高的 SIMD 通道密度（每个 AVX2 寄存器可容纳 8 个通道，每个 AVX-512 寄存器可容纳 16 个通道）。

让我们先看一个**独立完整、可直接 JIT 运行的预览程序**，它在一个合成的 48×16 几何测试图（包含一个高亮矩形、一个圆形以及一条对角斜条）上运行这两个 `checkfree simd fn` 计算核，让你现在就能直接运行并看到用 ASCII 字符渲染出的检测边缘：

```goose
const W = 48;
const H = 16;

checkfree inline fn abs_i32(v: i32) -> i32 {
    return if v < 0 { -v } else { v };
}

checkfree inline fn clamp_u8(v: i32) -> u8 {
    if v < 0 { return 0; }
    if v > 255 { return 255; }
    return v as u8;
}

checkfree inline fn u8_i32(b: u8) -> i32 {
    return b;
}

// 3x3 weighted blur kernel:
//   [1 2 1]
//   [2 4 2] / 16
//   [1 2 1]
checkfree simd fn blur3x3_row(
    top: const u8[:],
    mid: const u8[:],
    bot: const u8[:],
    dst: u8[:],
) {
    let w = dst.len;
    assert(w >= 2);
    assert(top.len == w && mid.len == w && bot.len == w);

    dst[0] = mid[0];
    for x in 1..w - 1 {
        let s: i32 =
              u8_i32(top[x - 1]) + 2 * u8_i32(top[x]) + u8_i32(top[x + 1])
            + 2 * u8_i32(mid[x - 1]) + 4 * u8_i32(mid[x]) + 2 * u8_i32(mid[x + 1])
            + u8_i32(bot[x - 1]) + 2 * u8_i32(bot[x]) + u8_i32(bot[x + 1]);
        dst[x] = ((s + 8) >> 4) as u8;
    }
    dst[w - 1] = mid[w - 1];
}

// 3x3 Sobel edge magnitude: |Gx| + |Gy| clamped to 0..255.
checkfree simd fn sobel3x3_row(
    top: const u8[:],
    mid: const u8[:],
    bot: const u8[:],
    dst: u8[:],
) {
    let w = dst.len;
    assert(w >= 2);
    assert(top.len == w && mid.len == w && bot.len == w);

    dst[0] = 0;
    for x in 1..w - 1 {
        let tl = u8_i32(top[x - 1]);
        let tc = u8_i32(top[x]);
        let tr = u8_i32(top[x + 1]);
        let ml = u8_i32(mid[x - 1]);
        let mr = u8_i32(mid[x + 1]);
        let bl = u8_i32(bot[x - 1]);
        let bc = u8_i32(bot[x]);
        let br = u8_i32(bot[x + 1]);

        let gx = -tl + tr - 2 * ml + 2 * mr - bl + br;
        let gy = -tl - 2 * tc - tr + bl + 2 * bc + br;
        dst[x] = clamp_u8(abs_i32(gx) + abs_i32(gy));
    }
    dst[w - 1] = 0;
}

fn make_scene(w: i64, h: i64) -> u8[>..] {
    var img: u8[>..] = [];
    img.resize(w * h, 20);
    for y in h {
        for x in w {
            // Left filled rectangle
            if x >= 4 && x <= 16 && y >= 3 && y <= 12 {
                img[y * w + x] = 220;
            }
            // Right filled disk (aspect-ratio adjusted for text cells)
            let dx = x - 34;
            let dy = (y - 8) * 2;
            if dx * dx + dy * dy <= 72 {
                img[y * w + x] = 240;
            }
        }
    }
    return img;
}

fn apply_stage(src: const u8[:], dst: u8[:], w: i64, h: i64, sobel: bool) {
    assert(src.len == w * h && dst.len == w * h);
    for y in h {
        let ym = if y > 0 { y - 1 } else { 0 };
        let yp = if y + 1 < h { y + 1 } else { h - 1 };
        let top = src[ym * w..ym * w + w];
        let mid = src[y * w..y * w + w];
        let bot = src[yp * w..yp * w + w];
        let out = dst[y * w..y * w + w];
        if sobel {
            sobel3x3_row(top, mid, bot, out);
        } else {
            blur3x3_row(top, mid, bot, out);
        }
    }
}

fn main() {
    let raw = make_scene(W, H);
    var blurred: u8[>..] = [];
    var edges: u8[>..] = [];
    blurred.resize(W * H, 0);
    edges.resize(W * H, 0);

    apply_stage(raw, blurred, W, H, false);
    apply_stage(blurred, edges, W, H, true);

    var strong_edges = 0;
    for p in edges {
        if p >= 128 { strong_edges += 1; }
    }
    print("scene ", W, "x", H, ", strong edge pixels (>=128): ", strong_edges);
    let ramp = " .:-=+*#%@";
    for y in 2..H - 2 {
        var line: u8[>..] = [];
        for x in W {
            let v = edges[y * W + x];
            let idx = (v * (ramp.len - 1)) / 255;
            line.push(ramp[idx]);
        }
        print(line);
    }
}
```

```text
scene 48x16, strong edge pixels (>=128): 336
  -@@@@@@@@@@@@@@@-            -*###*-          
  *@@@@@@@@@@@@@@@*        -*#@@@@@@@@@#*-      
  #@@@#########@@@#       *@@@@@@@@@@@@@@@*     
  #@@#         #@@#      *@@@@@@@@#@@@@@@@@*    
  #@@#         #@@#     -@@@@@#*-   -*#@@@@@-   
  #@@#         #@@#     *@@@*           *@@@*   
  #@@#         #@@#     *@@@             @@@*   
  #@@#         #@@#     *@@@*           *@@@*   
  #@@#         #@@#     -@@@@@#*-   -*#@@@@@-   
  #@@@#########@@@#      *@@@@@@@@#@@@@@@@@*    
  *@@@@@@@@@@@@@@@*       *@@@@@@@@@@@@@@@*     
  -@@@@@@@@@@@@@@@-        -*#@@@@@@@@@#*-      
```

看看这清晰锐利的矩形与圆形轮廓——它们完全由两个零越界检查的 `checkfree simd fn` 计算核算出！

---

## 2. 含光环区的条带消息与无死锁 `qlimit` 背压控制

接下来，我们使用 `thread_fn` 将这条流水线扩展到所有 CPU 核心上。

在共享内存型语言中，工作线程通常会直接读取共享的 `src` 缓冲区，同时向 `dst` 中互不重叠的行写入结果——但能否避免行别名冲突或在边界光环区（halo）上发生竞争，全凭程序员的自律。而在 Goose 中，工作线程采用**无共享**模型。那么，在没有共享 `src` 指针的情况下，工作线程该如何对 `[y0, y0 + rows)` 这些行执行 3×3 模板计算呢？

**直接将上下各 1 行的边界光环区打包进扁平消息中！**

```text
BandJob.pixels (total = (rows + 2) * width bytes):
┌───────────────────────────────────────┐  row 0          = halo above y0 (clamped at 0)
├───────────────────────────────────────┤  row 1          = y0
│            active band                │  ...
│            (rows rows)                │  row rows       = y0 + rows - 1
├───────────────────────────────────────┤  row rows + 1   = halo below (clamped at H-1)
└───────────────────────────────────────┘
```

通过扁平结构体 `struct BandJob { y0: i32, rows: i32, width: i32, mode: u8, pixels: u8[] }` 发送 `(rows + 2) * width` 字节的数据，带来了巨大的工程优势：
- **零共享内存、零锁：** 每个工作线程独占自己的输入光环区数据，并在各自的私有数据栈上构造 `BandResult { y0, rows, pixels: u8[] }`。
- **摊薄传输开销：** 选择 `BAND_ROWS = 8`（在百万像素级图像上可取 `16`–`32`）既能让光环区重复拷贝的比例微乎其微，又能保持粗粒度的队列通信。
- **通过 `qlimit` 实现有界内存：** 调用 `qlimit<BandJob>(workers * 2)` 可限制在途任务数量，确保主线程绝不会将整段 4K 视频全部缓存在内存中。由于有界队列满时 `qput` 会立即返回 `false`，我们的生产者采用了**边推边排空（drain-while-pushing）**循环：
  - 尝试调用 `qput(job)`。
  - 若返回 `false`，则调用 `qget<BandResult>(1)`（等待 1 ms），将任意已完成的条带写回输出图像，然后重试！
  - 因为工作线程总会持续消费 `BandJob` 并将结果推入无界的 `BandResult` 队列，生产者**绝不可能发生死锁**。

---

## 3. 受控故障隔离

如果流水线支持用户自定义滤镜模式或不可信的处理阶段，并收到了一个非法任务，该怎么办？将测试工作线程放入 `thread_spawn_contained` 中运行，可以把任何 Goose 运行时故障（`assert` 失败、越界错误或 `abort`）严格隔离在该工作线程内部：`thread_wait(id, why)` 会返回 `false`，将精确的错误诊断信息写入 `why`，干净地释放该工作线程的数据栈，同时让主流水线毫发无损地继续执行。

---

## 4. 完整多线程流水线（原生编译与运行）

由于多线程程序需要原生编译而非在 TinyCC JIT 下运行，请使用 `goose build` 编译并运行这个完整的综合项目：

```bash
goose build pipeline.goose -o pipeline
./pipeline                  # runs benchmark + verification + fault-isolation test
./pipeline --out edges.pgm  # also writes a standard binary PGM (P5) image
```

<!-- build -->
```goose
import std;
import os;

const W = 96;
const H = 48;
const BAND_ROWS = 8;

const MODE_BLUR:  u8 = 1;
const MODE_SOBEL: u8 = 2;

// --- 1. Flat queue messages -------------------------------------------

struct BandJob {
    y0:     i32,
    rows:   i32,   // 0 signals worker shutdown
    width:  i32,
    mode:   u8,
    pixels: u8[],  // (rows + 2) * width bytes (includes top & bottom 1-row halos)
}

struct BandResult {
    y0:     i32,
    rows:   i32,
    pixels: u8[],  // rows * width bytes
}

// --- 2. Check-free SIMD 3x3 kernels -----------------------------------

checkfree inline fn abs_i32(v: i32) -> i32 {
    return if v < 0 { -v } else { v };
}

checkfree inline fn clamp_u8(v: i32) -> u8 {
    if v < 0 { return 0; }
    if v > 255 { return 255; }
    return v as u8;
}

checkfree inline fn u8_i32(b: u8) -> i32 {
    return b;
}

checkfree simd fn blur3x3_row(
    top: const u8[:],
    mid: const u8[:],
    bot: const u8[:],
    dst: u8[:],
) {
    let w = dst.len;
    assert(w >= 2);
    assert(top.len == w && mid.len == w && bot.len == w);

    dst[0] = mid[0];
    for x in 1..w - 1 {
        let s: i32 =
              u8_i32(top[x - 1]) + 2 * u8_i32(top[x]) + u8_i32(top[x + 1])
            + 2 * u8_i32(mid[x - 1]) + 4 * u8_i32(mid[x]) + 2 * u8_i32(mid[x + 1])
            + u8_i32(bot[x - 1]) + 2 * u8_i32(bot[x]) + u8_i32(bot[x + 1]);
        dst[x] = ((s + 8) >> 4) as u8;
    }
    dst[w - 1] = mid[w - 1];
}

checkfree simd fn sobel3x3_row(
    top: const u8[:],
    mid: const u8[:],
    bot: const u8[:],
    dst: u8[:],
) {
    let w = dst.len;
    assert(w >= 2);
    assert(top.len == w && mid.len == w && bot.len == w);

    dst[0] = 0;
    for x in 1..w - 1 {
        let tl = u8_i32(top[x - 1]);
        let tc = u8_i32(top[x]);
        let tr = u8_i32(top[x + 1]);
        let ml = u8_i32(mid[x - 1]);
        let mr = u8_i32(mid[x + 1]);
        let bl = u8_i32(bot[x - 1]);
        let bc = u8_i32(bot[x]);
        let br = u8_i32(bot[x + 1]);

        let gx = -tl + tr - 2 * ml + 2 * mr - bl + br;
        let gy = -tl - 2 * tc - tr + bl + 2 * bc + br;
        dst[x] = clamp_u8(abs_i32(gx) + abs_i32(gy));
    }
    dst[w - 1] = 0;
}

// Process a haloed band of (rows + 2) rows into (rows) output rows.
fn process_band(
    haloed: const u8[:],
    rows: i64,
    w: i64,
    mode: u8,
) -> u8[>..] {
    assert(haloed.len == (rows + 2) * w);
    var out: u8[>..] = [];
    out.resize(rows * w, 0);

    for r in rows {
        let top = haloed[r * w..(r + 1) * w];
        let mid = haloed[(r + 1) * w..(r + 2) * w];
        let bot = haloed[(r + 2) * w..(r + 3) * w];
        let dst = out[r * w..(r + 1) * w];
        if mode == MODE_BLUR {
            blur3x3_row(top, mid, bot, dst);
        } else if mode == MODE_SOBEL {
            sobel3x3_row(top, mid, bot, dst);
        } else {
            abort("unsupported filter mode");
        }
    }
    return out;
}

// --- 3. Share-nothing worker pool & contained validator ---------------

thread_fn filter_worker() {
    loop {
        let job = qget<BandJob>();
        guard job.rows > 0 else { break; }
        let out = process_band(job.pixels, job.rows, job.width, job.mode);
        qput(BandResult { y0: job.y0, rows: job.rows, pixels: copy(out) });
    }
}

// Demonstrates fault containment if a bad filter mode reaches a worker.
thread_fn untrusted_stage(mode: u8) {
    var dummy: u8[>..] = [];
    dummy.resize(3 * 4, 0);
    let _ = process_band(dummy, 1, 4, mode);
}

// --- 4. Host helpers: scene synthesis, band packing, PGM export -------

fn make_scene(w: i64, h: i64) -> u8[>..] {
    var img: u8[>..] = [];
    img.resize(w * h, 24);
    for y in h {
        for x in w {
            if x >= 8 && x <= 32 && y >= 8 && y <= 36 {
                img[y * w + x] = 215;
            }
            let dx = x - 66;
            let dy = y - 24;
            if dx * dx + dy * dy <= 225 {
                img[y * w + x] = 245;
            }
            if abs((x - y) - 10) <= 2 && y >= 6 && y <= 40 {
                img[y * w + x] = 180;
            }
        }
    }
    return img;
}

fn pack_haloed_band(src: const u8[:], w: i64, h: i64, y0: i64, rows: i64) -> u8[>..] {
    var buf: u8[>..] = [];
    for ry in -1..rows + 1 {
        let sy = clamp(y0 + ry, 0, h - 1);
        buf.append(src[sy * w..sy * w + w]);
    }
    return buf;
}

fn store_band(dst: u8[:], w: i64, res: const BandResult&) {
    let y0 = res.y0 as i64;
    let rows = res.rows as i64;
    for r in rows {
        let dst_off = (y0 + r) * w;
        let src_off = r * w;
        for x in w {
            dst[dst_off + x] = res.pixels[src_off + x];
        }
    }
}

fn run_stage_serial(src: const u8[:], w: i64, h: i64, mode: u8) -> u8[>..] {
    var dst: u8[>..] = [];
    dst.resize(w * h, 0);
    for y in h {
        let ym = if y > 0 { y - 1 } else { 0 };
        let yp = if y + 1 < h { y + 1 } else { h - 1 };
        let top = src[ym * w..ym * w + w];
        let mid = src[y * w..y * w + w];
        let bot = src[yp * w..yp * w + w];
        let out = dst[y * w..y * w + w];
        if mode == MODE_BLUR {
            blur3x3_row(top, mid, bot, out);
        } else {
            sobel3x3_row(top, mid, bot, out);
        }
    }
    return dst;
}

// Dispatch one full image stage across the live worker pool with qlimit backpressure.
fn run_stage_parallel(src: const u8[:], w: i64, h: i64, mode: u8) -> u8[>..] {
    var dst: u8[>..] = [];
    dst.resize(w * h, 0);

    var sent = 0;
    var received = 0;
    var y0 = 0;

    while y0 < h {
        let rows = min(BAND_ROWS, h - y0);
        let packed = pack_haloed_band(src, w, h, y0, rows);
        loop {
            if qput(BandJob {
                y0: y0 as i32,
                rows: rows as i32,
                width: w as i32,
                mode: mode,
                pixels: copy(packed),
            }) {
                sent += 1;
                break;
            }
            // Queue is full: drain a completed band to avoid deadlock.
            let res, got = qget<BandResult>(1);
            if got {
                store_band(dst, w, res);
                received += 1;
            }
        }
        y0 += rows;
    }

    while received < sent {
        let res = qget<BandResult>();
        store_band(dst, w, res);
        received += 1;
    }
    return dst;
}

fn encode_pgm(pixels: const u8[:], w: i64, h: i64) -> u8[>..] {
    var out: u8[>..] = [];
    format(out, "P5\n", w, " ", h, "\n255\n");
    out.append(pixels);
    return out;
}

fn main() {
    let raw = make_scene(W, H);

    // 1. Single-threaded reference (Blur -> Sobel).
    let serial_blur = run_stage_serial(raw, W, H, MODE_BLUR);
    let serial_edges = run_stage_serial(serial_blur, W, H, MODE_SOBEL);

    // 2. Bounded multi-threaded worker pool (reused across both stages!).
    let workers = min(4, max(1, hardware_threads()));
    qlimit<BandJob>(workers * 2);

    var ids: i64[>..] = [];
    for _ in workers {
        ids.push(thread_spawn(filter_worker));
    }

    let par_blur = run_stage_parallel(raw, W, H, MODE_BLUR);
    let par_edges = run_stage_parallel(par_blur, W, H, MODE_SOBEL);

    // Send one shutdown sentinel per worker and join cleanly.
    for _ in workers {
        while !qput(BandJob { y0: 0, rows: 0, width: 0, mode: 0, pixels: [] }) {
            let _, _ = qget<BandResult>(1);
        }
    }
    for id in ids {
        let ok = thread_wait(id);
        assert(ok);
    }

    // 3. Verify pixel-for-pixel determinism against the serial reference.
    var mismatches = 0;
    var checksum: u64 = 0;
    for i in W * H {
        if par_edges[i] != serial_edges[i] {
            mismatches += 1;
        }
        checksum = checksum * 131 + par_edges[i];
    }

    // 4. Verify fault containment on a bad filter mode.
    var why: u8[>..] = [];
    let bad_ok = thread_wait(thread_spawn_contained(untrusted_stage, 99), why);
    let caught = !bad_ok && find(why, "unsupported filter mode") >= 0;

    // 5. Build binary PGM image (and optionally write if `--out <path>` is passed).
    let pgm = encode_pgm(par_edges, W, H);
    let argv = args();
    for i in 1..argv.len - 1 {
        if argv[i] == "--out" {
            let wrote = write_file(argv[i + 1], pgm);
            print("wrote ", argv[i + 1], ": ", wrote);
        }
    }

    print("pipeline ", W, "x", H, " (workers>0: ", workers > 0, ")");
    print("mismatches=", mismatches, " checksum=", checksum);
    print("contained fault isolated: ", caught);
    print("PGM bytes: ", pgm.len);
}
```

```text
pipeline 96x48 (workers>0: true)
mismatches=0 checksum=1256989707077777756
contained fault isolated: true
PGM bytes: 4621
```

仔细观察 `main()` 中的执行过程：
- **同一个工作线程池**在整个两阶段（模糊与 Sobel）执行期间保持存活，没有任何重复创建线程的开销。
- `qlimit<BandJob>(workers * 2)` 约束了队列的内存上限，而每当队列写满时，生产者便会顺势排空已完成的 `BandResult`。
- 每一个像素都与单线程参考结果完全一致（`mismatches=0`）。
- 即便非法滤镜模式（`99`）在 `untrusted_stage` 中触发了 `abort("unsupported filter mode")`，`thread_spawn_contained` 也能将其干净地捕获，而 `main()` 得以不受影响地继续运行。

---

## 练习

1. **添加 3×3 锐化滤镜（`MODE_SHARPEN = 3`）：** 使用拉普拉斯锐化算子（`5 * mid[x] - top[x] - bot[x] - mid[x - 1] - mid[x + 1]`，钳位到 `0..255`）实现 `checkfree simd fn sharpen3x3_row`，将其接入 `process_band` 和 `run_stage_serial`，并运行三阶段流水线（`模糊 → 锐化 → Sobel`）。
2. **串行与并行扩展性基准测试：** 将参数增大至 `W = 1024`、`H = 1024`、`BAND_ROWS = 32`，在 `run_stage_serial` 和 `run_stage_parallel` 外围添加 `clock()` 计时（来自 `os`），使用 `goose build` 编译，并对比 1、2、4 以及 `hardware_threads()` 个工作线程下的加速比。
3. **读取真实的 PGM（`P5`）文件：** 编写 `decode_pgm(data: const u8[:]) -> i64, i64, u8[>..], bool` 函数来解析二进制 `P5` 文件头（跳过以 `#` 开头的注释行），并将磁盘上的真实灰度图像送入你的并行流水线！
4. **统计背压事件：** 在 `run_stage_parallel` 中添加一个计数器，每当 `qput(BandJob { ... })` 返回 `false` 时加 1；将限制调低为 `qlimit<BandJob>(2)` 且设置 `BAND_ROWS = 4`，观察背压机制如何主动对生产者进行限流。

---

## 本章要点

- **行切片模板计算**是 Goose 中最具普适性的性能甜蜜点：它们既能满足无检查函数（`checkfree fn`）的静态证明要求，又能将视图提升至寄存器，并在 clang、GCC 和 MSVC 下都能顺利自动向量化。
- **含光环区的扁平条带消息（`u8[]`）**将二维并行模板计算转化为纯粹的无共享消息传递——无需互斥锁，无需共享缓冲区，且能确定性地重组输出结果。
- **在 `qlimit` 队列上采用边推边排空策略**，既限制了内存占用上限，又从根本上杜绝了生产者–消费者死锁。
- **`thread_spawn_contained`** 将工作线程的中止转化为结构化的错误诊断信息，从而确保出现故障的处理阶段绝不会拖垮整条流水线。
