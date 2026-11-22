# Appendix B: Goose for C++ and Rust Programmers

If you already know C++ or Rust, you know 90% of Goose's surface syntax—and your biggest risk is carrying over habits designed to work around a heap allocator or borrow checker that Goose doesn't have. This appendix gives you a direct mental-model translation guide and highlights the five habits worth updating on day one.

---

## 1. Direct Translation Table

| Concept | Idiomatic C++ | Idiomatic Rust | Idiomatic Goose |
|---|---|---|---|
| Dynamic array | `std::vector<T>` | `Vec<T>` | `T[>..]` (grow-only, stable addresses) or `T[>..<]` (LIFO stack) |
| Owned string | `std::string` | `String` | `u8[>..]` (builder) or inline `u8[varint]` / `u8[]` / `u8[..k]` |
| Borrowed string / slice | `std::string_view` / `std::span<T>` | `&str` / `&[T]` / `&mut [T]` | `const u8[:]` / `const T[:]` / `T[:]` |
| Tagged union / sum type | `std::variant<A, B>` or virtual base class | `enum E { A, B }` | Fixed `E` (indexable, union-sized) or variable `E..` (exact variant size!) |
| Nullable pointer / ref | `T*` (unchecked) | `Option<&T>` / `Option<&mut T>` | `T?` / `const T?` (flow-narrowed by `if`/`while`/`guard`/`assert`) |
| Lifetime annotations | None (programmer discipline) | `'a` + `&` / `&mut` exclusivity | None (inferred via monomorphized root provenance; aliasing allowed) |
| Resource cleanup | RAII destructors (`~T()`) | `Drop` trait | Memory freed in $O(1)$ at scope exit; non-memory resources via `defer` |
| Error propagation | Exceptions or `std::expected` | `Result<T, E>` + `?` | Trailing `bool` (local), `T?` (lookup), or `return E from f` (deep) |
| Graph / AST / arena | `std::deque` / custom arena + pointers | `Vec<Node>` + `u32` indices / `slotmap` | `T[>..]` + `T&<u32>?` relative links, or `slot_pool<T>` + `u32 in pool` |
| Bounds-check removal | Unchecked `operator[]` (UB) | Iterator combinators / `unsafe get_unchecked` | BCE + index types (`u32 in a`) + compiler-verified `checkfree fn` |
| Generics & lambdas | Templates + lambdas | Traits + `impl Fn` | Untyped/generic params (`fn f<T, F>`) + compile-time trailing blocks `{ it }` |
| Concurrency | `std::thread` + `std::mutex` / atomics | `std::thread` + `Arc<Mutex<T>>` / channels | Share-nothing `thread_fn` + flat messages over typed queues (`qput`/`qget`) |
| Serialization | Protobuf / FlatBuffers / custom | `serde` / `rkyv` | `bytes_of` / `to_bytes` / verified `from_bytes<A>` directly on live arrays |

---

## 2. Five Idiom Shifts (With Runnable Goose)

### Shift 1: Keep Typed References Across Growth (No `reserve`, No Index Workarounds)

In C++, pushing onto a `std::vector` invalidates every pointer and iterator unless you pre-`reserve` a hard upper bound. In safe Rust, holding `&mut` references into a `Vec` while pushing is forbidden, pushing you toward raw integer indices.

In Goose, a `T[>..]` **never relocates**, so `.push(...)` returns a live reference that stays valid for the entire scope:

```goose
struct Task { id: i32, priority: i32 }

fn main() {
    var tasks: Task[>..] = [];
    let first .= tasks.push(Task { id: 1, priority: 10 });
    for i: i32 in 2..5000 {
        tasks.push(Task { id: i, priority: 1 });
    }
    first.priority = 99; // Still valid 4,999 pushes later!
    print("tasks=", tasks.len, " first.priority=", tasks[0].priority);
}
```

```text
tasks=4999 first.priority=99
```

### Shift 2: Prefer Variable-Mode ADTs (`E..`) Over Pointer-Indirected Polymorphism

Where C++ allocates `std::vector<std::unique_ptr<Expr>>` and Rust pads every `enum Expr` to the size of its largest variant, Goose packs heterogeneous variants—including inline strings—tightly into one buffer with `E..[>..]`:

```goose
enum LogEntry {
    Heartbeat { tick: u32 },
    Metric { tick: u32, value: f64 },
    Alert { tick: u32, msg: u8[varint] },
}

fn main() {
    var log: LogEntry..[>..] = [];
    log.push(LogEntry.Heartbeat { tick: 1 });
    log.push(LogEntry.Metric { tick: 2, value: 98.6 });
    log.push(LogEntry.Alert { tick: 3, msg: "disk 95%" });

    var alerts = 0;
    for entry in log {
        match entry {
            Alert &a => {
                alerts += 1;
                print("alert@", a.tick, ": ", a.msg);
            },
            _ => {},
        }
    }
    print("total bytes=", bytes_of(log).len, " alerts=", alerts);
}
```

```text
alert@3: disk 95%
total bytes=32 alerts=1
```

### Shift 3: Replace `Result`/`?` Chains and Exceptions With `return … from`

You don't need to wrap every internal helper's return type in `Result<T, Err>` or pay for exception unwinding tables. Keep inner helpers returning clean direct types (`i64`, `Node&`) and jump straight to the entry point on failure:

```goose
import std;

fn parse_port(text: const u8[:]) -> i64, u8[>..] {
    fn read_num(s: const u8[:]) -> i64 {
        let v, ok = parse_int(s);
        guard ok else { return 0, str("not an integer: '", s, "'") from parse_port; }
        return v;
    }

    let p = read_num(text);
    guard p >= 1 && p <= 65535 else {
        return 0, str("port out of range: ", p) from parse_port;
    }
    return p, str("");
}

fn main() {
    let p1, e1 = parse_port("8080");
    let p2, e2 = parse_port("99999");
    print("ok: ", p1, " (err='", e1, "')");
    print("bad: ", p2, " (err='", e2, "')");
}
```

```text
ok: 8080 (err='')
bad: 0 (err='port out of range: 99999')
```

### Shift 4: Use `slot_pool` + Index Types for Non-Nested Lifetimes

When C++ uses `new`/`delete` or Rust reaches for `slotmap`/`slab` + `usize`, use Goose's `slot_pool<T>` for $O(1)$ slot reuse and `u32 in pool.items` when you want zero bounds checks in hot loops without `unsafe`.

### Shift 5: Pair Data Stacks With `defer` Instead of RAII/Drop

You never write destructors to free memory in Goose—leaving a scope resets its data stack in one store. For external resources (files, GPU contexts, UI contexts, audio devices, C handles), place `defer cleanup();` right after acquisition so cleanup runs in exact reverse LIFO order on every exit path (including `return … from`).

---

## 3. Muscle-Memory Pitfalls to Watch Out For

1. **`=` writes through a reference; `.=` binds or rebinds a reference.**
   In C++, references cannot be rebound; in Rust, `*r = val` vs `r = &other` makes the distinction explicit via `*`. In Goose, references are transparent, so `cur = next` copies the *value* of `next` into `cur`'s target—use `cur .= next` to move the reference itself!
2. **`let` is not `const`!**
   In Rust, `let x = vec![1, 2]` makes `x` deeply immutable unless you write `let mut x`. In Goose, `let xs = [1, 2]` forbids reassigning `xs` as a whole (`xs = ...`) while still allowing element writes (`xs[0] = 9`). Use `const` (`const xs = ...` or `const u8[:]`) for read-only contents.
3. **Narrow integers do not promote to `int`!**
   In C/C++, `u8 + u8` promotes to `int` automatically. In Goose, `u8 + u8` stays `u8` and wraps modulo 256. Widen explicitly (`var sum: u16 = a; sum += b;`) before summing bytes.
4. **No implicit moves for non-fixed values.**
   Fresh results are constructed directly in place (`let xs = make()`), which costs zero copies. Assigning an existing non-fixed variable to another requires writing `copy(xs)` so every $O(n)$ copy is visible in the source.
5. **No `unsafe` block—use `checkfree` + index types instead.**
   Where Rust programmers reach for `unsafe { slice.get_unchecked(i) }` to remove bounds checks in hot graphs or matrices, Goose gives you compiler-checked `u32 in arr` index types and `checkfree fn` so you get identical zero-check assembly while staying 100% memory-safe.
