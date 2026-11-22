# Chapter 21: Capstone 2 — A Multi-Threaded Image Processing Pipeline

Image and signal processing pipelines are where two distinct halves of systems programming collide: **tight, vectorizable inner loops** (where a single misplaced bounds check or pointer reload ruins SIMD throughput) and **multi-core parallelism** (where shared mutable buffers invite subtle data races and lock contention).

Goose addresses both halves with zero overlap between them: `checkfree simd fn` guarantees bounds-check-free, AVX2/AVX-512-ready inner kernels, while `thread_fn` and typed queues over flat variable-size structs distribute work across CPU cores with **zero shared mutable state**.

In this second capstone, we will build a two-stage **parallel grayscale image processing pipeline** (3×3 Gaussian-like box blur followed by 3×3 Sobel edge detection) with bounded-queue backpressure, deterministic serial-vs-parallel verification, contained fault isolation, and binary PGM (`P5`) export.

### Architecture Overview

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

### Concepts Synthesized in This Capstone

| Feature | Chapter | Role in This Project |
|---|---|---|
| Row-slice BCE & `checkfree fn` | Ch. 18 | Compiler proves zero bounds checks remain in both 3×3 blur and Sobel kernels. |
| `simd fn` & `inline fn` | Ch. 19 | Multi-versions hot convolution loops for AVX2/AVX-512 under clang with zero early exits. |
| Flat variable-size structs (`u8[]`) | Ch. 14 | Each `BandJob` and `BandResult` carries a contiguous strip of pixels across queues in one `memcpy`. |
| `qlimit<T>` backpressure | Ch. 14 | Caps queued bands so memory stays bounded while draining completed results to prevent deadlock. |
| `thread_spawn_contained` | Ch. 14 | Isolates an invalid/untrusted filter worker and captures its abort message cleanly. |
| `os` CLI & file I/O | Ch. 16–17 | Optional `--out <file.pgm>` argument exports standard binary PGM (`P5`) images. |

---

## 1. Check-Free SIMD Row Kernels (Blur and Sobel)

Both a 3×3 box blur and a 3×3 Sobel edge detector compute each output pixel `dst[x]` from the 3×3 neighborhood formed by three contiguous row slices: `top`, `mid`, and `bot`.

Passing those three row slices directly to a `checkfree simd fn` gives us three wins at once:
1. **Zero bounds checks:** A single `assert(top.len == w && mid.len == w && bot.len == w && dst.len == w)` lets Goose prove every `top[x - 1]`, `mid[x]`, and `bot[x + 1]` access inside `for x in 1..w - 1`.
2. **Backend-friendly vectorization:** Both clang and MSVC vectorize 1D row-slice stencils cleanly (remember Chapter 19's `blur` lesson: 298 ms row-slice vs. 952 ms flat on MSVC!).
3. **Exact integer widening:** Widening pixel sums into `i32` via an `inline fn` avoids 8-bit wrapping while staying 32-bit wide for maximum SIMD lane density (8 lanes per AVX2 register, 16 per AVX-512 register).

Let's start with a **self-contained, JIT-runnable preview** of our two `checkfree simd fn` kernels running on a synthetic 48×16 geometric test target (a bright rectangle, a circle, and a diagonal bar) so you can run it right now and see the detected edges in ASCII:

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

Look at that clean rectangle and circle outline—computed by two `checkfree simd fn` kernels with zero bounds checks!

---

## 2. Haloed Band Messages and Deadlock-Free `qlimit` Backpressure

Now let's scale this pipeline across all CPU cores using `thread_fn`.

In a shared-memory language, threads might all read directly from a shared `src` buffer while writing disjoint rows of `dst`—relying on human discipline not to alias rows or race on halos. In Goose, workers share **nothing**. How does a worker run a 3×3 stencil on rows `[y0, y0 + rows)` without a shared `src` pointer?

**Include the 1-row top and bottom halo directly inside the flat message!**

```text
BandJob.pixels (total = (rows + 2) * width bytes):
┌───────────────────────────────────────┐  row 0          = halo above y0 (clamped at 0)
├───────────────────────────────────────┤  row 1          = y0
│            active band                │  ...
│            (rows rows)                │  row rows       = y0 + rows - 1
├───────────────────────────────────────┤  row rows + 1   = halo below (clamped at H-1)
└───────────────────────────────────────┘
```

Sending `(rows + 2) * width` bytes in a flat `struct BandJob { y0: i32, rows: i32, width: i32, mode: u8, pixels: u8[] }` has huge engineering benefits:
- **Zero shared memory, zero locks:** Every worker owns its input halo and builds its `BandResult { y0, rows, pixels: u8[] }` on its private data stack.
- **Amortized overhead:** Choosing `BAND_ROWS = 8` (or `16`–`32` on megapixel images) keeps halo duplication tiny and queue traffic coarse-grained.
- **Bounded memory via `qlimit`:** Calling `qlimit<BandJob>(workers * 2)` caps in-flight jobs so the main thread never buffers an entire 4K video in RAM. Because `qput` returns `false` immediately when a bounded queue is full, our producer uses a **drain-while-pushing** loop:
  - Try `qput(job)`.
  - If `false`, call `qget<BandResult>(1)` (1 ms wait) to drain any completed band into the output image, then retry!
  - Because workers always drain `BandJob` and push to the unbounded `BandResult` queue, the producer can **never deadlock**.

---

## 3. Contained Fault Isolation

What if a pipeline supports user-configured filter modes or untrusted stages and receives a bad job? Wrapping a test worker in `thread_spawn_contained` isolates any Goose runtime failure (`assert`, bounds error, or `abort`) to that worker alone: `thread_wait(id, why)` returns `false`, populates `why` with the exact diagnostic, releases the worker's data stacks cleanly, and lets the main pipeline continue without losing work.

---

## 4. Complete Multi-Threaded Pipeline (Build & Run Natively)

Because threaded programs compile natively rather than under the TinyCC JIT, build and run this complete capstone with `goose build`:

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

Look closely at what happens in `main()`:
- The **same worker pool** stays alive across both stages (blur and Sobel) with zero thread respawn overhead.
- `qlimit<BandJob>(workers * 2)` bounds queue memory while the producer drains completed `BandResult`s whenever the queue fills.
- Every single pixel matches the single-threaded reference (`mismatches=0`).
- Even when an invalid filter mode (`99`) triggers `abort("unsupported filter mode")` inside `untrusted_stage`, `thread_spawn_contained` captures the error cleanly and `main()` continues uninterrupted.

---

## Exercises

1. **Add a 3×3 Sharpen filter (`MODE_SHARPEN = 3`):** Implement `checkfree simd fn sharpen3x3_row` using the Laplacian sharpen kernel (`5 * mid[x] - top[x] - bot[x] - mid[x - 1] - mid[x + 1]`, clamped to `0..255`), wire it into `process_band` and `run_stage_serial`, and run a 3-stage pipeline (`Blur → Sharpen → Sobel`).
2. **Benchmark serial vs. parallel scaling:** Increase `W = 1024`, `H = 1024`, `BAND_ROWS = 32`, add `clock()` timing (from `os`) around `run_stage_serial` and `run_stage_parallel`, compile with `goose build`, and compare speedups across 1, 2, 4, and `hardware_threads()` workers.
3. **Read real PGM (`P5`) files:** Write `decode_pgm(data: const u8[:]) -> i64, i64, u8[>..], bool` that parses a binary `P5` header (skipping `#` comments) and feeds real grayscale images from disk into your parallel pipeline!
4. **Count backpressure events:** Add a counter in `run_stage_parallel` that increments every time `qput(BandJob { ... })` returns `false`, lower `qlimit<BandJob>(2)` with `BAND_ROWS = 4`, and observe backpressure actively throttling the producer.

---

## Key Takeaways

- **Row-slice stencils** are the universal sweet spot in Goose: they satisfy `checkfree fn`, hoist views into registers, and auto-vectorize under clang, GCC, and MSVC alike.
- **Haloed flat band messages (`u8[]`)** turn 2D parallel stencils into pure share-nothing message passing with zero mutexes, zero shared buffers, and deterministic reassembly.
- **Drain-while-pushing on `qlimit` queues** bounds memory usage while making producer–consumer deadlocks impossible.
- **`thread_spawn_contained`** turns worker aborts into structured error diagnostics so faulty stages never bring down a pipeline.
