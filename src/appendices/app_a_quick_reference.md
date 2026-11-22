# Appendix A: Quick Reference

This appendix gathers Goose's keywords, operators, type syntax, builtins, CLI flags, and core glossary into a single desk reference.

---

## 1. Keywords

### Reserved Keywords

`import` · `namespace` · `struct` · `enum` · `type` · `fn` · `thread_fn` · `export` · `simd` · `recursive` · `let` · `var` · `extern` · `const` · `pad` · `reusable` *(deprecated)* · `if` · `else` · `while` · `for` · `in` · `loop` · `block` · `guard` · `defer` · `match` · `return` · `break` · `continue` · `as` · `true` · `false` · `null` · `self` · `bool` · `varint` · `i8` · `i16` · `i32` · `i64` · `u8` · `u16` · `u32` · `u64` · `f32` · `f64`

### Contextual Keywords

Ordinary identifiers everywhere except in their designated syntactic position:

| Contextual Keyword | Recognized Position |
|---|---|
| `from` | After a `return` value: `return err from load;` |
| `upto` | Bound type: `u32 upto edges` |
| `inline` | Before `fn` (cannot combine with `extern`, `export`, `simd`, `recursive`, `thread_fn`) |
| `checkfree` | Before `fn`, `simd`, `inline`, `recursive`, or `thread_fn` |
| `root` | Before `struct`: `root struct Pool { ... }` |
| `fields`, `variants`, `types` | In a `const for` header: `const for <N, A> in fields<T>` |
| `has_default`, `same` | In a `const if` condition: `const if same<A, B>` |

### Function Modifier Order

When combining function modifiers, write them in this fixed order:

```text
export | extern  →  checkfree  →  simd  →  inline  →  recursive  →  fn | thread_fn
```

---

## 2. Operator Precedence

Tightest binding at the top (matching Rust's precedence):

| Precedence | Category | Operators |
|---|---|---|
| 1 (tightest) | Postfix | `f(…)`, `a[i]`, `a[lo..hi]`, `.field`, `x as T`, `x as! T` |
| 2 | Unary | `-`, `!`, `~`, `&` |
| 3 | Multiplicative | `*`, `/`, `%` *(Euclidean `%`)* |
| 4 | Additive | `+`, `-` |
| 5 | Shifts | `<<`, `>>` |
| 6 | Bitwise AND | `&` |
| 7 | Bitwise XOR | `^` |
| 8 | Bitwise OR | `\|` |
| 9 | Comparison | `<`, `<=`, `>`, `>=` |
| 10 | Equality & Identity | `==`, `!=`, `.==`, `.!=` |
| 11 | Logical AND | `&&` |
| 12 (loosest) | Logical OR | `\|\|` |

- **Assignments (statements, not expressions):** `=`, `.=`, `+=`, `-=`, `*=`, `/=`, `%=`, `&=`, `|=`, `^=`, `<<=`, `>>=`, `++`, `--`.
- **Ranges (`a..b`):** Allowed in slicing (`xs[a..b]`), `for` headers (`for i in a..b`), and `match` arms (`0..10 => ...`).
- **Arithmetic width:** Operations run at the operands' declared width (unsigned wraps; signed checks overflow in `--debug` and wraps in release).
- **Casts:** `as` checks range in `--debug`; `as!` explicitly wraps/truncates without checking.

---

## 3. Size Classes and Array Family Matrix

### Size Classes

| Size Class | Examples | Lives On | Copyable? | Indexable? |
|---|---|---|---|---|
| **Fixed** | Scalars, fixed structs, `T[k]`, `T[..k]`, fixed `Enum`, `T&`, `T[:]`, `T&<uN>` | Native call stack (or inline in aggregates) | Implicitly by value *(except structs holding self-relative refs)* | Yes |
| **Variable** | `T[]`, `T[uN]`, `T[varint]`, `T[..]`, variable `Enum..`, variable structs | Data stack (or inline inside containers) | Explicit `copy(x)` *(if no self-relative refs)* | `T[..]` and fixed-element `T[]` yes; variable-element arrays sequential only |
| **Resizable** | `T[>..]`, `T[>..<]`, structs with a resizable tail, `root struct` | Dedicated data stack(s) | Explicit `copy(x)` *(if no self-relative refs)* | Fixed-element yes; variable-element sequential only |

### Array Types

| Syntax | Name | Growth / Shrink | Capacity / Length Storage | Notes |
|---|---|---|---|---|
| `T[k]` | Fixed array | Neither | Compile-time constant `k` | Packed inline `T[k]` |
| `T[..k]` | Static limited array | Push/pop up to `k` | `u8`/`u16`/`u32`/`u64` len + `k` slots inline | Fixed size class! Great for small keys (`u8[..32]`) |
| `T[..]` | Runtime limited array | Push/pop up to runtime cap | `u32 cap` + `u32 len` + `cap` slots on data stack | Capacity fixed at creation |
| `T[]` / `T[uN]` / `T[varint]` | Variable array | None (frozen at construction) | Inline `u32` / `uN` / ULEB128 len + elements | Ideal for inline strings (`u8[varint]`) and flat queue payloads (`u8[]`) |
| `T[>..]` | Grow-only array | `push`/`append`; shrinks only when no views are live | Header + data stack (never moves on `push`) | Default dynamic container; references survive `push` |
| `T[>..<]` | Grow-shrink array | `push`/`pop`/`clear`/`resize` freely | Header + data stack | Interior references allowed in local variables only (never stored in fields/arrays) |
| `T[:]` / `const T[:]` | Slice | Read/write elements (no resize) | `{ data*, len }` (16 bytes) | Non-owning view into any indexable array |

---

## 4. References, Optionals, Relative Links, and Index Types

| Syntax | Meaning | Size | Key Rules |
|---|---|---|---|
| `T&` / `const T&` | Non-null reference | 8 B *(16 B fat ref for resizable)* | Transparent in value contexts; `.=` binds/rebinds, `=` writes target; `.==`/`.!=` compares identity |
| `T?` / `const T?` | Optional reference | 8 B (`null` = 0) | Narrowed to `T&` inside `if r`, `while r`, `guard r else`, `assert(r)` |
| `T&<u8\|u16\|u32>` | Self-relative link | 1 / 2 / 4 B | Offset from link's own address; non-copyable; directly `to_bytes`/`from_bytes` serializable; use `self` in literals |
| `T&<u8\|u16\|u32>?` | Optional self-relative link | 1 / 2 / 4 B | Offset `0` means `null`; non-null zero-offset store rejected |
| `T&<uN in pool>` | Pool-relative link | 1 / 2 / 4 B | Offset from global/root pool base; freely copyable; supports optional `?` |
| `uN in arr` | Index type | 1 / 2 / 4 / 8 B | Proven `0 <= v < arr.len`; eliminates bounds check on `arr[v]` |
| `uN upto arr` | Bound type | 1 / 2 / 4 / 8 B | Proven `0 <= v <= arr.len`; eliminates slice bound checks on `arr[lo..hi]` |

---

## 5. Builtins and Array Members

### Language Builtins

| Builtin | Signature / Behavior |
|---|---|
| `print(a, b, …)` | Formats all arguments + `\n` into one atomic write to stdout |
| `str(a, b, …) -> u8[>..]` | Formats arguments into a fresh string built at its destination |
| `format(out, a, b, …)` | Appends formatted arguments to a growable `u8` array `out` |
| `assert(cond)` | Aborts if false/null; narrows optionals and feeds facts to BCE |
| `abort(msg)` / `exit(code)` | Terminates program immediately (never returns; caught by `thread_spawn_contained` for `abort`) |
| `copy(x) -> T` | Explicit $O(\text{size})$ copy of a stored value |
| `default<T>() -> T` | Zero-initialized fixed-size `T` with declared field defaults applied |
| `to_bytes(a)` / `to_bytes(a, out)` | Framed byte image (`ULEB128(byte_len)` + element region) |
| `bytes_of(a) -> const u8[:]` | Zero-copy read-only view of an array's element region |
| `from_bytes<A>(b) -> A, bool` | Verifies untrusted framed bytes and constructs an owned array `A` |
| `embed_file(path) -> const u8[:]` | Embeds a file's bytes at compile time |
| `embed_shader(stage, glsl)` | Compiles GLSL (`"vert"`/`"frag"`/`"comp"`) at compile time to SPIR-V/MSL/HLSL |
| `src_file()` / `src_line()` | Compile-time file name and line (captures caller site when used as parameter default) |
| `hardware_threads() -> i64` | Recommended worker count capped by CPU cores and stack virtual budget |
| `thread_spawn(f, args…) -> i64` | Spawns an isolated `thread_fn` worker |
| `thread_spawn_contained(f, args…) -> i64` | Spawns a fault-isolated worker whose aborts are captured by `thread_wait` |
| `thread_wait(id)` / `thread_wait(id, msg) -> bool` | Joins worker; returns `false` and fills `msg` if contained worker aborted |
| `qput(v) -> bool` | Enqueues flat value `v` (returns `false` immediately if `qlimit` is reached) |
| `qget<T>()` / `qget<T>(ms)` / `qpoll<T>()` | Blocking, timed (`T, bool`), or non-blocking (`T, bool`) dequeue |
| `qlimit<T>(n)` | Sets maximum buffered messages for `T`'s queue |

### Array Members

`.len` · `.cap` · `.push(v)` *(returns ref)* · `.append(xs)` · `.pop()` · `.pop_n(n)` · `.resize(n)` / `.resize(n, fill)` · `.clear()` · `.index_of(ref)` · `.range_index(slice)` · `.check_length(n)` · `.move_from(other)`

---

## 6. CLI Cheat Sheet

| Command | Purpose |
|---|---|
| `goose file.goose [-- args]` | Compile & run in-process via TinyCC JIT (instant feedback, unoptimized) |
| `goose build file.goose [-o out]` | Compile optimized native executable via system C compiler (`-O2`) |
| `goose build --debug file.goose` | Native build with signed overflow & cast range checks enabled |
| `goose build --shared --header f.h f.goose` | Build a C-callable shared library + generated C header |
| `goose check file.goose` | Typecheck, optimize, and run BCE without executing |
| `goose check --stats --bce-lines f.goose` | Print total and per-line bounds-check elimination counts |
| `goose check --stacks --specs f.goose` | Print static data-stack counts and monomorphized specializations |
| `goose -o out.c [--standalone] f.goose` | Emit generated C99 source (`--standalone` embeds the full runtime) |
| `goose format [-w] file.goose` | Format Goose source code (`-w` writes in place) |
| `goose fetch file.goose` | Download pinned git package dependencies into `GOOSE_HOME` |
| `goose lsp` | Run the Language Server Protocol server over stdio |
| `goose toolchain` | Inspect detected C compiler (`clang`, `gcc`, `cl`) and built-in packages |

---

## 7. Compact Glossary

- **Balanced call:** A call that never leaves an array shorter than at entry, allowing pre-existing views to survive.
- **Candidate:** A variable whose storage could hold the target of a reference loaded from a container.
- **Case function:** A set of function overloads keyed on individual enum variants (`Shape.Circle`, `Shape.Rect&`) dispatched via jump table.
- **Data stack:** A virtual-address-space reservation (default 2 GB) with a bump pointer; every non-fixed value lives on one.
- **Exact root:** A root proving exact storage identity (required for `index_of`, relative links, and stack-top caching), versus an **inexact root** (outlives bound only).
- **Flat:** Containing no references, slices, or relative references at any depth (eligible for thread queues and C FFI).
- **Function value:** A compile-time function or trailing block `{ it > 0 }` passed as a generic parameter `F`; never heap-allocated and cannot escape.
- **Holder:** A struct or array that contains references or slices.
- **Narrowing:** Flow-sensitive refinement of `T?` into `T&` after `if`, `while`, `guard`, or `assert`.
- **Sequential array:** An array of variable-size elements (`T..[>..]`, `u8[][>..]`) that supports iteration and relative links, not random indexing.
- **Transparent reference:** A `T&` that acts as its pointee `T` in every value expression (reading fields, arithmetic, `=`); only `.=` and `.==`/`.!=` operate on the reference itself.
