# Chapter 14: Share-Nothing Concurrency

In C, C++, and even safe Rust, multithreading usually begins with shared memory: mutexes, read-write locks, atomics, memory orderings, and careful reasoning about lock hierarchies to avoid deadlocks and false sharing.

Goose takes a cleaner path inspired by message-passing systems like Erlang: **there is no shared mutable memory between threads.** There are no locks, no atomics, no memory-ordering flags, and no data races anywhere in the language.

Instead, a worker declared with **`thread_fn`** is compiled as an independent program with its own private data stacks and its own snapshotted copy of any globals it uses. Threads communicate exclusively by passing **flat, contiguous values** through built-in **typed queues**—which is fast in Goose because even variable-size structs (like a row of pixels or a text buffer) are already one contiguous block copied with a single `memcpy`.

> **Build Note:** Programs that use `thread_fn` must be compiled natively with **`goose build`** rather than `goose run`, because the embedded TinyCC JIT cannot place thread-local storage for worker data stacks. Clicking **▶ Run** in the interactive playground builds `thread_fn` examples natively automatically.

By the end of this chapter, you will be able to:

- Spawn isolated `thread_fn` workers with `thread_spawn`, `thread_wait`, and `hardware_threads()`.
- Exchange flat, variable-size values safely across threads using typed queues (`qput`, `qget`, `qpoll`, `qlimit`).
- Isolate worker crashes and supervise retries cleanly with `thread_spawn_contained` and `thread_wait(id, msg)`.
- Build deterministic parallel worker pools that reassemble out-of-order results.

---

## 14.1 `thread_fn`: Isolated Workers and Global Snapshots

When you spawn a `thread_fn`, think of it as launching a lightweight mini-program inside the same process:

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

Key properties of `thread_fn` workers:

- **Private data stacks:** Every worker gets its own set of data stacks computed statically from the functions reachable from that `thread_fn`. Returning from the worker immediately releases all of its stacks, whether or not anyone calls `thread_wait`.
- **Snapshotted globals:** When `thread_spawn` starts a worker, Goose copies the current values of any flat globals reachable by that worker. If the worker modifies a global, it modifies **its own private copy**—never `main`'s!
- **Static read-only data is shared safely:** String literals and compile-time `const` globals live in shared read-only memory.
- **Flatness enforced at compile time:** Non-flat globals (holding references/slices to mutable data) and global `root struct` owners cannot be used by a `thread_fn` (worker-local `root struct`s and pools are completely fine). Worker parameters must also be flat and cannot carry default values.

Watch global snapshot isolation in action:

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

Both workers saw `counter == 100` (the value at spawn time), mutated their own copies to `105` and `120`, and left `main`'s `counter` completely untouched at `100`—zero locks needed!

---

## 14.2 Typed Queues, Flat Messages, and Backpressure

Goose provides one thread-safe FIFO queue per **message type**—so `Job` messages and `Result` messages automatically live in separate queues without declaring channel objects.

| Builtin | Signature | Behavior |
|---|---|---|
| `qput(v)` | `-> bool` | Appends flat value `v` to `T`'s queue; returns `false` immediately if at `qlimit` (return value may be ignored) |
| `qget<T>()` | `-> T` | Removes and returns the next `T`, blocking until one arrives |
| `qget<T>(ms)` | `-> T, bool` | Waits at most `ms` milliseconds; returns `(zero_val, false)` on timeout |
| `qpoll<T>()` | `-> T, bool` | Non-blocking take; returns `(zero_val, false)` if queue is empty |
| `qlimit<T>(n)` | | Caps `T`'s queue to at most `n` messages for backpressure |
| `hardware_threads()` | `-> i64` | Suggested worker count, bounded by both CPU cores and stack address-space budget |
| `thread_spawn(f, args...)` | `-> i64` | Starts worker `f`, returning a unique never-reused thread ID |
| `thread_wait(id)` / `thread_wait(id, msg)` | `-> bool` | Blocks until worker ends; returns `false` if contained worker aborted (and captures message into `msg`) |

Because queues are keyed by type, define distinct structs (`struct Job { ... }`, `struct Result { ... }`) rather than sending raw `i64`s—giving you both compile-time message routing and self-documenting protocols.

### Flat Values Only

Any value sent via `qput` (or passed as a `thread_fn` argument) must be **flat**: no references, slices, or relative references at any depth. Flat variable-size structs (like `struct Packet { id: i32, body: u8[] }`) are completely legal and cross the queue as one contiguous `memcpy`!

Trying to queue a non-flat struct containing a slice is caught immediately at compile time:

<!-- compile-fail -->
```goose
struct BadMsg { text: const u8[:] }

fn main() {
    qput(BadMsg { text: "nope" });   // error: BadMsg is not flat (contains a slice)
}
```

### Bounded Queues, Polling, and Timeouts

By default a queue is unbounded until you call `qlimit<T>(n)`. Setting a limit lets a producer apply backpressure (when full, `qput` returns `false` immediately without blocking), while `qpoll<T>()` and `qget<T>(ms)` let consumers check without hanging forever:

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

Notice that queued messages outlive their sender and can hold owned variable-size strings (`u8[]`) cleanly!

---

## 14.3 Fault Isolation with `thread_spawn_contained`

Normally, a failed `assert`, bounds check, division by zero, or `abort(msg)` terminates the entire process. For long-running services, batch runners, or untrusted input processors, you often want to **isolate a crash to a single worker**, log the failure, and keep going.

Start that worker with **`thread_spawn_contained(worker, args...)`**:

- If the worker triggers an `assert`, bounds check, or `abort(msg)`, **only that worker terminates**, and all of its data stacks are freed cleanly.
- Nothing is printed to stderr automatically. Instead, `thread_wait(id, why)` returns **`false`** and appends the worker's exact abort message (including source file and line!) into `why`.
- Only the *first* `thread_wait(id, why)` for a given worker receives the message; subsequent waits still return `false`.

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

> **Watch Out:** Contained workers isolate Goose runtime checks and `abort(msg)`—they do **not** catch native stack overflow, `exit(code)`, or aborts on `main`'s thread. Also, external resources opened through raw C `extern fn` calls stay open if a contained worker aborts mid-flight.

---

## 14.4 Thread Safety at the C / FFI Boundary

Goose's memory isolation covers every Goose data structure and every stateful OS call in the standard library (such as `print`, file I/O, and queues).

Once you call external C libraries through `extern fn` or `extern "lib" { ... }` (Chapter 15), however, the compiler cannot see what shared static state that C code touches. Treat third-party C libraries according to their own thread-safety documentation—and synchronize or isolate calls to non-thread-safe C libraries (such as GUI/GPU APIs like `gfx`) on one dedicated thread.

---

## 14.5 Worked Example: A Parallel Mandelbrot Worker Pool

Let us put the whole share-nothing concurrency model together in a classic parallel worker pool: rendering a 40×12 ASCII slice of the Mandelbrot set across `hardware_threads()` workers, sending flat variable-size rows (`RowResult { y: i32, cells: u8[] }`) back through a typed queue, reassembling them in order, and verifying 100% agreement with a single-threaded render!

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

Notice how simple, clean, and race-free that parallel renderer is: **zero mutexes, zero shared buffers, zero atomics**—just flat `RowJob`s going in, flat `RowResult`s coming out, and deterministic reassembly at the end.

---

## Exercises

1. **Test queue backpressure with a producer/consumer:** Set `qlimit<i64>(4)` and spawn a slow consumer worker that reads 10 items. In `main`, push 10 items in a loop, retrying whenever `qput` returns `false`, and count how many times backpressure kicked in.
2. **Add contained fault handling to a worker pool:** Modify Section 14.5 so `render_worker` calls `abort("bad row")` if `job.y == 5`. Spawn workers with `thread_spawn_contained`, inspect `thread_wait(id, why)`, and log the captured abort message without crashing `main`.
3. **Measure serial vs. parallel speedup:** Using `import os;` and `clock()`, increase Section 14.5 to `W = 200`, `H = 100`, `MAXITER = 5000`, build it natively (`goose build`), and print the serial vs. multi-threaded milliseconds to compare scaling on your CPU!

---

## Key Takeaways

- Goose enforces **share-nothing concurrency**: every `thread_fn` runs as a separate compiled program with its own data stacks and a spawn-time snapshot of any flat globals it touches.
- Threads exchange **flat** values (including variable-size structs with inline arrays) via per-type queues (`qput`, `qget<T>`, `qget<T>(ms)`, `qpoll<T>`, `qlimit<T>`) with a single contiguous `memcpy`.
- **`thread_spawn_contained`** + `thread_wait(id, msg)` isolates worker panics (`assert`, bounds checks, `abort`) and hands the error text cleanly to a supervisor.
- Threaded Goose programs compile natively with `goose build` (not TinyCC JIT).

Next, in **Chapter 15: Interoperating with C (Both Directions)**, we cross the native boundary—calling C libraries via `extern fn` and zero-toolchain `extern "lib" { ... }`, bundling C sources with `.build` sidecars, and exporting Goose libraries back to C via `export fn` and `--shared`.
