# 第 14 章：无共享并发

在 C、C++，甚至安全的 Rust 中，多线程编程通常从共享内存开始：互斥锁、读写锁、原子操作、内存序，以及为了避免死锁和伪共享而必须仔细推敲的加锁层次结构。

Goose 受到 Erlang 等消息传递系统的启发，选择了一条更简洁的道路：**线程之间不存在共享的可变内存。** 语言中没有任何锁、没有原子操作、没有内存序标志，也完全不存在数据竞争。

相反，使用 **`thread_fn`** 声明的工作线程会被编译为一个独立程序，拥有自己私有的数据栈，以及它所访问的任何全局变量的快照副本。线程之间完全通过内置的**类型化队列（typed queue）**传递**扁平（flat）、连续的值**来通信——这在 Goose 中非常高效，因为即使是变长结构体（例如一行像素或一段文本缓冲区），其本身也已经是一块连续内存，只需一次 `memcpy` 即可完成复制。

> **构建说明：** 使用 `thread_fn` 的程序必须通过 **`goose build`** 进行原生编译，而不能使用 `goose run`，因为内置的 TinyCC JIT 无法为工作线程的数据栈分配线程局部存储（TLS）。在交互式演练场中点击 **▶ Run** 时，会自动对 `thread_fn` 示例执行原生构建。

学完本章后，你将能够：

- 使用 `thread_spawn`、`thread_wait` 和 `hardware_threads()` 启动隔离的 `thread_fn` 工作线程。
- 使用类型化队列（`qput`、`qget`、`qpoll`、`qlimit`）在线程间安全地交换扁平的变长值。
- 使用 `thread_spawn_contained` 和 `thread_wait(id, msg)` 隔离工作线程崩溃并优雅地监督重试。
- 构建确定性的并行工作线程池，并按顺序重组乱序返回的结果。

---

## 14.1 `thread_fn`：隔离的工作线程与全局快照

当你启动一个 `thread_fn` 时，可以将其视为在同一进程内启动了一个轻量级的迷你程序：

```text
  Main Thread Program                    Worker Thread Program
+----------------------+               +------------------------+
| Main Data Stacks     |               | Worker Data Stacks     |
| Main Globals (g = 1) | --snapshot--> | Worker Globals (g = 1) |
+----------------------+   at spawn    +------------------------+
           |                                       ^
           | qput(Job { ... })                     | qget<Job>()
           v                                       |
      [ Typed Queue<Job> ] ------------------------+
      [ Typed Queue<Result> ] <--------------------+
           |                                       |
           +----------> qget<Result>()   qput(Result { ... })
```

`thread_fn` 工作线程的核心特性包括：

- **私有数据栈：** 每个工作线程都会获得一套独立的数据栈，其配置根据从该 `thread_fn` 可达的函数在编译期静态计算得出。一旦工作线程返回，其所有数据栈会立即释放，无论是否有人调用 `thread_wait`。
- **全局变量快照：** 当 `thread_spawn` 启动一个工作线程时，Goose 会复制该工作线程可达的所有扁平全局变量的当前值。如果工作线程修改了某个全局变量，它修改的**只是自己的私有副本**——绝不会影响 `main` 线程中的变量！
- **静态只读数据可安全共享：** 字符串字面量和编译期 `const` 全局变量位于共享只读内存中。
- **编译期强制扁平性：** `thread_fn` 不能访问非扁平全局变量（包含指向可变数据的引用或切片）以及全局 `root struct` 所有者（不过工作线程局部的 `root struct` 和内存池完全合法）。工作线程的参数也必须是扁平类型，且不能带有默认值。

下面来看全局快照隔离的实际运行效果：

<!-- build -->
```goose
struct Report { worker_id: i64, seen: i64 }

var counter = 10;

thread_fn bump_worker(id: i64, add: i64) {
    // Modifies ONLY this worker's snapshot of `counter`!
    counter += add;
    qput(Report { worker_id: id, seen: counter });
}

fn main() {
    counter = 100;                   // updated BEFORE spawning workers

    let t1 = thread_spawn(bump_worker, 1, 5);
    let t2 = thread_spawn(bump_worker, 2, 20);
    thread_wait(t1);
    thread_wait(t2);

    var r1 = qget<Report>();
    var r2 = qget<Report>();
    // Sort by worker_id so printed order is deterministic:
    if r1.worker_id > r2.worker_id {
        let tmp = r1;
        r1 = r2;
        r2 = tmp;
    }
    print("worker 1 saw=", r1.seen, " worker 2 saw=", r2.seen);
    print("main counter is still=", counter);
}
```

```
worker 1 saw=105 worker 2 saw=120
main counter is still=100
```

两个工作线程看到的都是 `counter == 100`（启动时刻的快照值），各自将私有副本修改为 `105` 和 `120`，而 `main` 线程中的 `counter` 依然完好无损地保持为 `100`——全程不需要任何锁！

---

## 14.2 类型化队列、扁平消息与背压

Goose 为每种**消息类型**提供一个线程安全的先进先出（FIFO）队列——因此 `Job` 消息和 `Result` 消息会自动进入彼此独立的队列，无需显式声明通道对象。

| 内置函数 | 签名 | 行为 |
|---|---|---|
| `qput(v)` | `-> bool` | 将扁平值 `v` 追加到类型 `T` 的队列中；若队列已达到 `qlimit` 上限则立即返回 `false`（返回值可忽略） |
| `qget<T>()` | `-> T` | 取出并返回下一个 `T`，若队列为空则阻塞直到有消息到达 |
| `qget<T>(ms)` | `-> T, bool` | 最多等待 `ms` 毫秒；超时时返回 `(zero_val, false)` |
| `qpoll<T>()` | `-> T, bool` | 非阻塞取出；若队列为空则返回 `(zero_val, false)` |
| `qlimit<T>(n)` | | 将类型 `T` 的队列容量上限设为最多 `n` 条消息，用于施加背压 |
| `hardware_threads()` | `-> i64` | 建议的工作线程数，同时受 CPU 核心数和栈地址空间预算约束 |
| `thread_spawn(f, args...)` | `-> i64` | 启动工作线程 `f`，返回一个唯一且永不复用的线程 ID |
| `thread_wait(id)` / `thread_wait(id, msg)` | `-> bool` | 阻塞直到工作线程结束；如果受控工作线程异常中止则返回 `false`（并将中止信息捕获到 `msg` 中） |

由于队列是按类型区分的，建议定义清晰独立的结构体（如 `struct Job { ... }`、`struct Result { ... }`），而不是直接传递裸 `i64`——这样既能获得编译期消息路由，又能让通信协议本身自文档化。

### 仅限扁平值

任何通过 `qput` 发送的值（或作为 `thread_fn` 参数传递的值）都必须是**扁平类型**：在任意嵌套层级上都不能包含引用、切片或相对引用。扁平的变长结构体（例如 `struct Packet { id: i32, body: u8[] }`）则完全合法，并会作为一整块连续内存通过单次 `memcpy` 穿过队列！

如果试图将包含切片的非扁平结构体放入队列，编译器会立即报错：

<!-- compile-fail -->
```goose
struct BadMsg { text: const u8[:] }

fn main() {
    qput(BadMsg { text: "nope" });   // error: BadMsg is not flat (contains a slice)
}
```

### 有界队列、轮询与超时

默认情况下，在调用 `qlimit<T>(n)` 之前队列是无界的。设置容量上限可以让生产者感知背压（队列满时，`qput` 会立即返回 `false` 而不会阻塞），而 `qpoll<T>()` 和 `qget<T>(ms)` 则允许消费者在不无限期挂起的前提下检查队列：

<!-- build -->
```goose
struct Packet { id: i32, text: u8[] }

fn main() {
    qlimit<Packet>(2);               // allow at most 2 packets buffered at once

    let ok1 = qput(Packet { id: 1, text: "alpha" });
    let ok2 = qput(Packet { id: 2, text: "beta" });
    let ok3 = qput(Packet { id: 3, text: "overflow" }); // queue is full -> false!
    print("puts: ", ok1, " ", ok2, " ", ok3);

    let p1, got1 = qpoll<Packet>();
    let p2, got2 = qget<Packet>(10);
    let _, got3 = qget<Packet>(5);   // empty: times out after 5 ms -> false!
    print("got1=", got1, " (", p1.id, ":", p1.text, ")");
    print("got2=", got2, " (", p2.id, ":", p2.text, ")");
    print("got3=", got3);
}
```

```
puts: true true false
got1=true (1:alpha)
got2=true (2:beta)
got3=false
```

注意，入队的消息在生命周期上可以长于发送者，并且能够干净利落地持有自拥有的变长字符串（`u8[]`）！

---

## 14.3 使用 `thread_spawn_contained` 实现故障隔离

通常情况下，失败的 `assert`、越界检查、除以零或 `abort(msg)` 会终止整个进程。但对于长期运行的服务、批处理运行器或不可信输入处理器，你往往希望**将崩溃隔离在单个工作线程内**，记录故障信息，并让主程序继续运行。

使用 **`thread_spawn_contained(worker, args...)`** 启动这样的受控工作线程：

- 如果工作线程触发了 `assert`、越界检查或 `abort(msg)`，**只有该工作线程会终止**，其所有数据栈都会被干净地释放。
- 不会自动向 stderr 打印任何内容。取而代之的是，`thread_wait(id, why)` 会返回 **`false`**，并将该工作线程确切的中止信息（包含源文件名和行号！）追加到 `why` 中。
- 对于同一个工作线程，只有*第一次*调用 `thread_wait(id, why)` 会收到该消息；后续等待调用仍返回 `false`。

<!-- build -->
```goose
import std;

thread_fn serve_job(attempt: i64) {
    assert(attempt >= 2);            // simulates a bug on attempts 0 and 1
    qput(attempt * 10);
}

fn main() {
    var attempt = 0;
    var why: u8[>..] = [];
    while !thread_wait(thread_spawn_contained(serve_job, attempt), why) {
        let has_assert = find(why, "assert failed") >= 0;
        print("attempt ", attempt, " crashed (captured assert? ", has_assert, ")");
        why.clear();
        attempt++;
    }
    let result = qget<i64>();
    print("attempt ", attempt, " succeeded with result=", result);
}
```

```
attempt 0 crashed (captured assert? true)
attempt 1 crashed (captured assert? true)
attempt 2 succeeded with result=20
```

> **注意：** 受控工作线程隔离的是 Goose 运行时检查和 `abort(msg)`——它**不会**捕获原生栈溢出、`exit(code)`，也不会捕获 `main` 线程上的中止。此外，如果受控工作线程在执行中途异常中止，它通过原始 C `extern fn` 调用打开的外部资源仍会保持打开状态。

---

## 14.4 C / FFI 边界处的线程安全

Goose 的内存隔离覆盖了所有 Goose 数据结构，以及标准库中所有有状态的操作系统调用（例如 `print`、文件 I/O 和队列）。

然而，一旦你通过 `extern fn` 或 `extern "lib" { ... }`（第 15 章）调用外部 C 库，编译器便无法得知那些 C 代码访问了哪些共享静态状态。对于第三方 C 库，请严格遵循其自身的线程安全文档——对于非线程安全的 C 库（例如 `gfx` 等 GUI/GPU API），应当对其进行同步保护，或者将相关调用隔离在单个专用线程中执行。

---

## 14.5 综合实战：并行 Mandelbrot 工作线程池

现在让我们把整套无共享并发模型整合到一个经典的并行工作线程池示例中：在 `hardware_threads()` 个工作线程之间并行渲染一块 40×12 的 Mandelbrot 集 ASCII 切片，通过类型化队列传回扁平的变长行结果（`RowResult { y: i32, cells: u8[] }`），按原始行序重新拼装，并与单线程渲染结果进行 100% 逐字节一致性验证！

<!-- build -->
```goose
import std;

let W = 40;
let H = 12;
let MAXITER = 200;

struct RowJob    { y: i32 }          // y == -1 signals a worker to exit
struct RowResult { y: i32, cells: u8[] }

fn mandelbrot_iters(cx: f64, cy: f64) -> i64 {
    var x = 0.0;
    var y = 0.0;
    var i = 0;
    while i < MAXITER && x * x + y * y <= 4.0 {
        let nx = x * x - y * y + cx;
        y = 2.0 * x * y + cy;
        x = nx;
        i++;
    }
    return i;
}

fn render_row(y: i64) -> u8[>..] {
    let ramp = " .:-=+*#%@";
    var cells: u8[>..] = [];
    for x in W {
        let cx = -2.2 + 3.0 * x as f64 / W;
        let cy = -1.2 + 2.4 * y as f64 / H;
        let n = mandelbrot_iters(cx, cy);
        cells.push(if n == MAXITER { '@' } else { ramp[(n * 3) % (ramp.len - 1)] });
    }
    return cells;
}

thread_fn render_worker() {
    loop {
        let job = qget<RowJob>();
        guard job.y >= 0 else { break; }
        // `render_row` builds directly into `RowResult.cells`; `qput` copies one flat block.
        qput(RowResult { y: job.y, cells: render_row(job.y) });
    }
}

fn main() {
    // 1. Single-threaded reference render:
    var serial: u8[>..] = [];
    for y in H { serial.append(render_row(y)); }

    // 2. Parallel worker pool:
    let workers = min(4, max(1, hardware_threads()));
    for y in H       { qput(RowJob { y: y as i32 }); }
    for _ in workers { qput(RowJob { y: -1 }); }    // one shutdown sentinel per worker

    var ids: i64[>..] = [];
    for _ in workers { ids.push(thread_spawn(render_worker)); }

    // Collect out-of-order rows into one buffer + offset table:
    var parallel: u8[>..] = [];
    var row_offset: i64[>..] = [];
    row_offset.resize(H, -1);

    for _ in H {
        let res = qget<RowResult>();
        row_offset[res.y] = parallel.len;
        parallel.append(res.cells);
    }
    for id in ids { thread_wait(id); }

    // 3. Verify bit-for-bit equality against serial render and print preview:
    var matches = true;
    for y in H {
        let off = row_offset[y];
        let got = parallel[off..off + W];
        let want = serial[y * W..(y + 1) * W];
        if got != want { matches = false; }
    }
    print("rendered ", W, "x", H, " (workers>0: ", workers > 0, "); matches serial? ", matches);
    print("midline: |", parallel[row_offset[H / 2]..row_offset[H / 2] + W], "|");
}
```

```
rendered 40x12 (workers>0: true); matches serial? true
midline: |---@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@ * *-- |
```

请注意这个并行渲染器是多么简单、清晰且毫无数据竞争：**零互斥锁、零共享缓冲区、零原子操作**——只有扁平的 `RowJob` 进去、扁平的 `RowResult` 出来，以及末尾的确定性重组。

---

## 练习

1. **用生产者/消费者测试队列背压：** 设置 `qlimit<i64>(4)`，并启动一个读取 10 条数据的慢速消费者工作线程。在 `main` 中循环推送 10 条数据，每当 `qput` 返回 `false` 时进行重试，并统计背压一共触发了多少次。
2. **为工作线程池添加受控故障处理：** 修改第 14.5 节的示例，使 `render_worker` 在 `job.y == 5` 时调用 `abort("bad row")`。使用 `thread_spawn_contained` 启动工作线程，检查 `thread_wait(id, why)`，并在不导致 `main` 崩溃的前提下打印捕获到的中止信息。
3. **测量串行与并行加速比：** 使用 `import os;` 和 `clock()`，将第 14.5 节的参数提高到 `W = 200`、`H = 100`、`MAXITER = 5000`，使用原生构建（`goose build`）编译运行，并打印串行与多线程分别消耗的毫秒数，观察在你当前 CPU 上的扩展加速效果！

---

## 本章要点

- Goose 强制实行**无共享并发**：每个 `thread_fn` 都作为独立编译的程序运行，拥有自己私有的数据栈，以及它所访问的任何扁平全局变量在启动时刻的快照副本。
- 线程通过按类型划分的队列（`qput`、`qget<T>`、`qget<T>(ms)`、`qpoll<T>`、`qlimit<T>`）交换**扁平**值（包括含内联数组的变长结构体），底层只需单次连续 `memcpy`。
- **`thread_spawn_contained`** 搭配 `thread_wait(id, msg)` 可以隔离工作线程的崩溃（`assert`、越界检查、`abort`），并将错误文本干净地移交给监督者处理。
- 使用多线程的 Goose 程序需要通过 `goose build` 进行原生编译（而非 TinyCC JIT）。

接下来，在**第 15 章：与 C 的双向互操作**中，我们将跨越原生边界——通过 `extern fn` 和零工具链的 `extern "lib" { ... }` 调用 C 库，利用 `.build` 伴随文件打包 C 源码，并通过 `export fn` 与 `--shared` 将 Goose 库反向导出给 C 使用。
