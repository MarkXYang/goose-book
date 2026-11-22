# Chapter 2: Familiar Syntax, Deliberate Choices

If you have written C, C++, Rust, or Go, you can read Goose on sight. It uses braces for blocks, semicolons for statements, postfix `: Type` annotations, `let` and `var` for bindings, `fn` for functions, and `match` for branching.

Yet every familiar piece hides a deliberate design decision made in service of Goose's priorities—**speed, safety, and expressiveness**:

- Arithmetic happens at the **exact declared width** so tight loops stay in 32-bit or 8-bit registers and vectorize cleanly.
- `%` is **Euclidean** (never negative), so wraparound indexing never produces negative out-of-bounds indices.
- `let` controls *rebinding*, while `const` controls *writability*—keeping two distinct concepts cleanly separated.
- Control flow is expression-oriented (`if`, `match`, `block`), paired with `guard` for flat early exits and `defer` for deterministic cleanup.
- Functions return **multiple values directly** as a calling convention—no tuple packing—and use Uniform Function Call Syntax (UFCS) instead of classes or `impl` blocks.

By the end of this chapter, you will be able to:

- Use `let`, `var`, `const`, named constants, and type aliases accurately.
- Write exact-width integer, float, bitwise, and memberwise aggregate arithmetic without unexpected promotions or hidden overflows.
- Structure readable control flow with expression blocks, `match`, `guard`, `defer`, and Goose's `for` loops.
- Write free functions with multiple return values, default parameters, and method-style UFCS calls.

---

## 2.1 Bindings, Constants, and Type Syntax

Goose separates **whether a name can be reassigned** from **whether its contents can be modified**:

- `let` declares a binding that cannot be reassigned as a whole.
- `var` declares a variable that *can* be reassigned.
- `const` marks contents as read-only (`const x = e;` is shorthand for a `let` of type `const T`).

```goose
fn main() {
    let answer = 42;                 // inferred as i64; cannot reassign `answer`
    var count: i32 = 0;              // explicit type; reassignable
    count += 1;
    count++;                         // statement only, not an expression

    let xs = [1, 2, 3];
    xs[0] = 9;                       // fine: `let` forbids `xs = ...`, not element writes

    const limits = [10, 20, 30];     // neither reassignable nor writable inside
    var view: const u8[:] = "goose"; // reassignable binding to read-only bytes
    view = view[1..];

    print(answer, " ", count, " ", xs, " ", limits, " ", view);
}
```

```
42 2 [9, 2, 3] [10, 20, 30] oose
```

Notice the symmetry:
- `let xs = [1, 2, 3];` means *the binding `xs` is fixed*, while its elements remain writable.
- `var view: const u8[:] = "goose";` is the exact opposite: *the binding `view` can be re-pointed*, while the bytes it views cannot be overwritten.

Attempting to mutate a `const` binding's elements is caught at compile time:

<!-- compile-fail -->
```goose
fn main() {
    const limits = [10, 20, 30];
    limits[0] = 99;                  // error: cannot write through const
}
```

> **Coming from Rust:** In Rust, `let` means immutable unless you add `mut`. In Goose, `let` means *single-assignment binding* (like `final` in Java or `const` pointer in C), while read-only data is a property of the type (`const T`).

### Definite Initialization and Shadowing

Every local variable must be initialized before it is read on every control-flow path. A `let` without an initializer must be assigned **exactly once** along every path:

```goose
fn main() {
    let label: const u8[:];
    let ready = true;
    if ready {
        label = "go";
    } else {
        label = "wait";
    }
    print(label);
}
```

```
go
```

Assigning an uninitialized `let` inside a loop declared outside of it is rejected, because the loop could assign it more than once. Inner scopes may shadow outer names freely.

### Named Constants and Type Aliases

An **untyped** `let` or `const` global with a compile-time constant initializer is a **named constant**. Like a raw literal, an integer named constant adapts to any integer type that can hold its value, works as a fixed array length, and can appear in `match` arms:

```goose
let WIDTH = 16;
let HALF = WIDTH / 2;

type ByteId = u8;

fn main() {
    let grid: u8[WIDTH] = [0; WIDTH];
    let id: ByteId = HALF;           // HALF adapts to u8
    let big = WIDTH * 1_000_000_000; // WIDTH adapts to i64
    let tag = match 8 {
        HALF => "middle",
        _    => "other",
    };
    print(grid.len, " ", id, " ", big, " ", tag);
}
```

```
16 8 16000000000 middle
```

A global *with* an explicit type (such as `let WIDTH: i64 = 16;`) is an ordinary `i64` global and does **not** adapt to narrower types. A `type` declaration creates a transparent alias—`ByteId` and `u8` are the exact same type.

### Lexical Notes Worth Knowing

- **Nested block comments:** `/* outer /* inner */ still a comment */` nests properly, so commenting out a block of code that already contains `/* ... */` never breaks compilation.
- **Digit separators:** Underscores are allowed between digits in decimal, hex, and float literals (`1_000_000`, `0xFF_AA`, `3.141_592`, `0x1.8p3`).
- **Evaluation order:** Strictly left-to-right everywhere—operands, function arguments, struct fields, and assignment targets.
- **Postfix types:** Every type constructor except `const` is postfix (`T[4]`, `T[>..]`, `T[:]`, `T&`, `T?`). We will cover each in Chapters 4 through 12.

---

## 2.2 Exact-Width Scalars, Euclidean `%`, and Conversions

Goose provides fixed-width two's-complement integers (`i8`, `i16`, `i32`, `i64`, `u8`, `u16`, `u32`, `u64`), IEEE floats (`f32`, `f64`), 1-byte `bool`, and the compact `varint` storage type (covered with structs and arrays in Chapter 4).

### Arithmetic Stays at the Operands' Width

In C, adding two `uint8_t` values silently promotes both to `int`. Goose never silently promotes integers: **`u8 + u8` is an 8-bit add**, and **`i32 * i32` is a 32-bit multiply**. This keeps numeric kernels in narrow registers so the C backend can auto-vectorize them.

Integer literals have no committed type of their own: they adapt to whatever integer type they meet (`i64` when unconstrained, or `u64` above `i64.max`). A literal that does not fit its target type is a compile error.

```goose
fn main() {
    let b: u8 = 255;
    let answer = 42;                 // unconstrained literal -> i64
    let wrapped: u8 = b + 1;         // 8-bit unsigned add wraps to 0
    let widened = b + answer;        // u8 widens implicitly into i64 -> 297
    let fits_u8 = b - 55;            // 55 adapts to u8 -> 200
    print(wrapped, " ", widened, " ", fits_u8);
}
```

```
0 297 200
```

When two typed operands meet in a binary operator, Goose picks a common type by strict rules:

1. Equal types keep that type.
2. A literal adapts to the other operand's type (compile error if out of range).
3. If one type widens implicitly into the other without losing values (`u8 → i16`, `i32 → i64`, `f32 → f64`), the wider type wins.
4. Any integer meeting a float converts to that float type (`n * 0.5` is `f32` in an `f32` context, `f64` otherwise).
5. Same-width signed/unsigned mixes (`u32 + i32`) or `u64` mixed with signed integers are **compile errors**—cast explicitly so your intent is visible.

### Unsigned Wrapping, Signed Overflow, and `as` vs. `as!`

- **Unsigned overflow wraps modulo 2^width** in every build, by design—hashing, PRNGs, and bit tricks rely on it.
- **Signed overflow aborts in `--debug` builds** and wraps in release builds. Division or remainder by zero always aborts (as does `i64.min / -1`).
- **`x as T`** converts between numeric types and is **range-checked in `--debug`** builds (aborting if the value changes), truncating/wrapping in release builds.
- **`x as! T`** is **intentionally unchecked** even under `--debug`—use it whenever bit truncation or sign reinterpretation is deliberate.

```goose
fn main() {
    // Unsigned math wraps cleanly (FNV-1a hash):
    var h: u64 = 14695981039346656037;
    for c in "goose" {
        h = (h ^ c) * 1099511628211;
    }

    let big = 300;
    let low_byte: u8 = big as! u8;   // intentional truncation: 44
    let pi_approx = 3.75;
    let whole = pi_approx as i64;    // truncates toward zero: 3
    print(h, " ", low_byte, " ", whole);
}
```

```
3870774399305851480 44 3
```

> **Compiler Tip:** Goose warns on redundant casts (for example, `b as i64` when `b` is a `u8` already widening implicitly into an `i64`). Let implicit widening work for you and reserve `as` / `as!` for conversions that can narrow, change sign, or decide an operation's width.

### Euclidean `%` vs. `rem` and `div_euclid`

In C, C++, and Rust, `-7 % 3` is `-1`. Whenever you wrap a coordinate around a grid or ring buffer of width `W`, negative remainders force ugly `((x % W) + W) % W` workarounds—and confuse bounds-check elimination.

In Goose, **integer `%` is Euclidean**: the result always lies in `[0, |b|)` and is **never negative**. Because `x % n` is guaranteed to be non-negative and `< n` for positive `n`, the compiler eliminates bounds checks on `buf[x % n]` automatically.

Integer `/` still truncates toward zero (`-7 / 3 == -2`). When you want the exact matching quotient or truncating remainder, `std` provides `div_euclid` (paired with `%`) and `rem` (paired with `/`):

```goose
import std;

fn main() {
    print("truncating: ", -7 / 3, " rem ", rem(-7, 3));
    print("euclidean:  ", div_euclid(-7, 3), " mod ", -7 % 3);
    // Tile coordinate and in-tile offset for position x = -1 with tile width 10:
    print("tile=", div_euclid(-1, 10), " offset=", -1 % 10);
}
```

```
truncating: -2 rem -1
euclidean:  -3 mod 2
tile=-1 offset=9
```

### Bitwise Operators and Memberwise Aggregate Math

Two quality-of-life touches make systems and graphics code much cleaner in Goose:

1. **Bitwise precedence follows Rust, not C:** `~`, `&`, `|`, `^`, `<<`, `>>` bind **tighter** than `==` and `!=`, so `flags & MASK != 0` parses as `(flags & MASK) != 0`. Constants like `~4` and `1 << k` adapt to the destination integer width.
2. **Memberwise arithmetic on structs and fixed arrays:** Goose has no operator overloading; instead, `+`, `-`, `*`, `/`, `%`, and unary `-` apply element-by-element to structs and fixed arrays whose fields are numeric, and `*`/`/` accept a scalar on either side when all leaves share one type.

```goose
struct Vec3 { x: f32, y: f32, z: f32 }

fn main() {
    // Bitwise flags with clean precedence and constant adaptation:
    var flags: u8 = 0;
    let READ: u8 = 1;
    let WRITE: u8 = 2;
    flags |= READ | WRITE;
    flags &= ~READ;
    let bit40: u64 = 1 << 40;
    print("writable=", flags & WRITE != 0, " bit40=", bit40);

    // Memberwise struct and fixed-array math:
    let a = Vec3 { 1, 2, 3 };
    let b = Vec3 { 0.5, 0.5, 0.5 };
    var c = (a + b) * 2;
    print("c=", c, " sum=", [1, 2, 3] + [10, 20, 30]);
}
```

```
writable=true bit40=1099511627776
c=Vec3 { 3.0, 5.0, 7.0 } sum=[11, 22, 33]
```

Note also that `bool` never coerces from integers: write `if count != 0`, not `if count`.

---

## 2.3 Expressions, `block`, `guard`, and `defer`

Goose is **expression-oriented**: `if`, `match`, `block`, and bare `{ ... }` evaluate to their trailing expression (without a semicolon). There is no ternary `?:` operator because `if cond { a } else { b }` does the job cleanly.

Block-ended statements (`if`, `match`, `block`, loops, `guard ... else { ... }`) need no trailing semicolon. To use one as an operand inside a larger expression, wrap it in parentheses: `let v = (if ok { 10 } else { 20 }) + 1;`.

### `if` and `match`

Used as expressions, `if` requires a final `else`, and `match` on integers requires a wildcard `_` arm. Integer `match` arms accept literals, named constants, half-open ranges `lo..hi`, and comma-separated lists:

```goose
let ZERO = 0;

fn describe_hp(hp: i64) -> const u8[:] {
    let status = if hp > 50 { "healthy" } else { "hurt" };
    match hp {
        ZERO     => "down",
        1..20    => "critical",
        20..51   => status,
        _        => "healthy",
    }
}

fn main() {
    for hp in [0, 12, 35, 80] {
        print(hp, " -> ", describe_hp(hp));
    }
}
```

```
0 -> down
12 -> critical
35 -> hurt
80 -> healthy
```

### Flat Validation with `guard` and `block`

Deeply nested `if` ladders bury the happy path. Goose gives you two tools that compose beautifully:

- `guard cond;` runs the rest of the current block only when `cond` is true (in a loop body, failing the guard skips to the next iteration).
- `guard cond else { ... }` runs the `else` block instead—typically containing `return`, `break`, or `continue`.
- `block { ... }` creates a labeled-style target for `break value;` without needing a fake loop.

```goose
struct Unit { alive: bool, energy: i32, stunned: bool }

fn action(u: Unit) -> const u8[:] {
    block {
        guard u.alive else { break "fallen"; }
        guard !u.stunned else { break "stunned"; }
        guard u.energy >= 10 else { break "resting"; }
        "ready"
    }
}

fn main() {
    print(action(Unit { true, 25, false }));
    print(action(Unit { true, 5, false }));
    print(action(Unit { false, 50, false }));
}
```

```
ready
resting
fallen
```

In Chapter 5, you will see `guard` pull double duty by narrowing nullable optionals (`T?`) into safe non-null references (`T&`) for the rest of the block.

### Deterministic Scope Cleanup with `defer`

Goose has no destructors—memory is reclaimed automatically when data stacks rewind at scope exit. For **non-memory resources** (closing a graphics window, restoring a state counter, unlocking an external handle, flushing a log), Goose provides `defer`.

`defer stmt;` or `defer { ... }` runs when its enclosing block exits **by any path**: normal completion, `break`, `continue`, `return`, or a long-distance `return ... from` (Chapter 10). Multiple defers run in **LIFO order** (last registered, first executed), and only defers that execution actually reached will run.

```goose
var depth = 0;

fn scan(xs: i64[:]) -> i64 {
    depth++;
    defer depth--;                   // runs on every return from `scan`
    var visited = 0;
    for x in xs {
        defer visited++;             // runs at the end of each iteration, even on `continue`
        if x < 0 { continue; }
        if x > 100 { return visited; }
    }
    return visited;
}

fn main() {
    print("r1=", scan([1, -2, 3]), " depth=", depth);
    print("r2=", scan([10, 200, 30]), " depth=", depth);
}
```

```
r1=3 depth=0
r2=1 depth=0
```

> **Watch Out:** Look closely at `r2=1`: on iteration 0 (`x == 10`), `defer visited++` runs at the end of the iteration, making `visited` equal to `1`. On iteration 1 (`x == 200`), `return visited` builds the return value (`1`) *first*, and then runs the loop body's `defer visited++` and the function's `defer depth--` as those scopes exit. Also note that a `defer` body cannot jump out of itself (`return`/`break`/`continue` targeting an outer scope) and does not run on hard aborts (`abort`, failed `assert`, or `exit`).

---

## 2.4 Loops

Goose has three loop constructs—`for`, `while`, and `loop`:

- `for i in n` iterates `0, 1, ..., n - 1`.
- `for i in a..b` iterates the half-open range `[a, b)`.
- `for i: u8 in 0..256` lets you specify the loop variable's exact type—notice that `256` is allowed as an exclusive upper bound for `u8` so you can iterate all 256 byte values cleanly without overflow!
- `for x in xs` iterates elements of any array or slice (by copy for fixed-size elements, by reference for variable-size elements).
- `for &x in xs` binds every element by reference so you can mutate in place.
- `for x, i in xs` binds both the element and its index (`i64` by default, or a narrower type when the length fits).
- `loop { ... }` loops unconditionally and can evaluate to a value via `break expr;`.

```goose
fn main() {
    var xs: i64[>..] = [2, 4, 6];
    for &x in xs {
        x *= 10;                     // mutates elements in place
    }

    var weighted = 0;
    for x, i in xs {
        weighted += x * (i + 1);
    }

    var bytes = 0;
    for b: u8 in 0..256 {
        if b % 64 == 0 { bytes++; }
    }

    var n = 0;
    let first_hit = loop {
        n += 7;
        if n % 5 == 0 { break n; }
    };

    print("xs=", xs, " weighted=", weighted, " quarter_marks=", bytes, " hit=", first_hit);
}
```

```
xs=[20, 40, 60] weighted=280 quarter_marks=4 hit=35
```

> **Memory Model:** A `for` loop walks its array **in place**, without copying it. For a grow-only array `T[>..]`, `.len` is re-read before each iteration (so pushing inside a `for` extends the traversal), and the array cannot shrink while the loop is active—a safety rule we will explore fully in Chapter 6.

---

## 2.5 Functions, Multiple Returns, Defaults, and UFCS

Functions in Goose are **free functions**—there are no classes, methods, or `impl` blocks. Instead, Goose supports **Uniform Function Call Syntax (UFCS)**: writing `x.f(a, b)` calls `f(x, a, b)`. Built-in fields and members are checked first; otherwise, Goose resolves a free function taking `x` as its first parameter.

```goose
struct Rect { w: i64, h: i64 }

fn area(r: Rect) -> i64 { r.w * r.h }

fn clamp_val(x: i64, lo: i64 = 0, hi: i64 = 100) -> i64 {
    if x < lo { lo } else if x > hi { hi } else { x }
}

fn countdown_sum(var n: i64) -> i64 {  // `var` makes a by-value parameter reassignable
    var total = 0;
    while n > 0 {
        total += n;
        n--;
    }
    return total;
}

fn main() {
    let r = Rect { w: 4, h: 7 };
    print("area=", r.area(), " clamped=", 250.clamp_val(), " ", (-5).clamp_val(-2, 10));
    print("sum=", countdown_sum(5));
}
```

```
area=28 clamped=100 -2
sum=15
```

Notice two handy parameter rules shown above:
- Parameters are `let` bindings by default. Prefix a by-value parameter with `var` (like `var n: i64`) if you want to reassign it inside the function body.
- Trailing parameters may declare defaults (`lo: i64 = 0, hi: i64 = 100`), evaluated at each call site that omits them.

### Multiple Return Values

Goose has **no tuple type**. Instead, functions can declare comma-separated return types after `->` as part of the calling convention. By convention, a trailing `bool` reports success or failure for simple two-outcome operations:

```goose
fn divide(a: i64, b: i64) -> i64, bool {
    guard b != 0 else { return 0, false; }
    return a / b, true;
}

fn checked_half(n: i64) -> i64, bool {
    return divide(n, 2);             // forwards all return values directly
}

fn main() {
    let q, ok = divide(17, 5);
    var r = 0;
    var ok2 = false;
    r, ok2 = divide(9, 0);           // reassign existing variables with `=`
    let h, ok3 = checked_half(14);
    let only_first = divide(20, 4);  // single-value context takes the first result
    print(q, " ", ok, " | ", r, " ", ok2, " | ", h, " ", ok3, " | ", only_first);
}
```

```
3 true | 0 false | 7 true | 5
```

> **Watch Out:** Because multiple return values are a calling convention rather than a tuple value, an `if` or `match` branch cannot evaluate to `a, b`. Writing `return if ok { 1, true } else { 0, false };` is a compile error—return directly from each branch instead, just as `divide` does above.

---

## 2.6 Worked Example: A Torus Grid Rover

Let us bring Chapter 2's features together in a compact, self-contained simulation: a rover moving across a wraparound 2D grid, using named constants, structs with field defaults (`..`), memberwise vector math, Euclidean `%` wraparound, `guard`/`block` status checks, `defer` step accounting, UFCS, and multiple return values.

```goose
let GRID_W = 8;
let GRID_H = 6;

struct Vec2 { x: i64, y: i64 }

struct Rover {
    pos: Vec2,
    battery: i32 = 10,
    steps: i32 = 0,
}

fn wrap(p: Vec2) -> Vec2 {
    // Euclidean `%` wraps negative coordinates straight into [0, GRID_W / GRID_H).
    Vec2 { x: p.x % GRID_W, y: p.y % GRID_H }
}

fn status(r: Rover) -> const u8[:] {
    block {
        guard r.battery > 0 else { break "depleted"; }
        guard r.battery > 3 else { break "low-battery"; }
        "nominal"
    }
}

fn step(r: Rover&, delta: Vec2) -> Vec2, bool {
    guard r.battery > 0 else { return r.pos, false; }
    defer r.steps++;                 // counted whenever a move succeeds
    r.battery -= 2;
    r.pos = (r.pos + delta).wrap();  // memberwise `+` and UFCS `.wrap()`
    return r.pos, true;
}

fn main() {
    var rover = Rover { pos: Vec2 { 1, 0 }, .. };
    let moves: Vec2[6] = [
        Vec2 { -2, -1 },             // wraps across both edges!
        Vec2 { 3, 2 },
        Vec2 { 0, -4 },
        Vec2 { 1, 1 },
        Vec2 { 1, 0 },
        Vec2 { 1, 0 },               // battery hits 0 before this move
    ];

    for mv, i in moves {
        let pos, moved = rover.step(mv);
        print("move ", i, ": moved=", moved, " pos=", pos,
              " bat=", rover.battery, " [", rover.status(), "]");
    }
    print("total steps taken: ", rover.steps);
}
```

```
move 0: moved=true pos=Vec2 { 7, 5 } bat=8 [nominal]
move 1: moved=true pos=Vec2 { 2, 1 } bat=6 [nominal]
move 2: moved=true pos=Vec2 { 2, 3 } bat=4 [nominal]
move 3: moved=true pos=Vec2 { 3, 4 } bat=2 [low-battery]
move 4: moved=true pos=Vec2 { 4, 4 } bat=0 [depleted]
move 5: moved=false pos=Vec2 { 4, 4 } bat=0 [depleted]
total steps taken: 5
```

Look at move 5: `guard r.battery > 0 else { return r.pos, false; }` returned *before* execution reached `defer r.steps++`, so `rover.steps` stayed at `5` instead of `6`. That is why Goose's `defer` is scope-order aware—cleanup or bookkeeping only runs once you pass the `defer` line.

---

## Exercises

1. **Test exact-width arithmetic:** Edit the snippet from Section 2.2 in the playground to add a `u32` and an `i32` without a cast. Read the exact compiler diagnostic, then fix it using the minimal widening cast.
2. **Add terrain costs to the Rover:** Extend the Torus Grid Rover in Section 2.6 with a `fn tile_cost(p: Vec2) -> i32` function using `match (p.x + p.y) % 4` so some tiles cost `1` battery, some cost `2`, and rocky tiles (`3`) cost `3`. Reject a move if `rover.battery < cost`.
3. **Predict `defer` ordering:** Write a function with three `defer print(...)` statements—one before an `if`, one inside a `for` loop, and one after a `guard`—and verify the exact order they execute on both normal completion and early `return`.

---

## Key Takeaways

- `let` prevents rebinding, `var` allows rebinding, and `const` makes contents read-only; untyped constant globals adapt like literals.
- Arithmetic runs at the operands' exact width with no silent promotion to `int`, unsigned overflow wraps by definition, signed overflow aborts under `--debug`, and `as!` opts out of debug range checks when truncation is intentional.
- Integer `%` is Euclidean (never negative), making wraparound indexing safe and bounds-check friendly; `std` supplies `div_euclid` and `rem`.
- Expression-oriented `if`, `match`, `block`, `guard`, and `defer` keep control flow flat and resource cleanup deterministic.
- Free functions combine with UFCS (`x.f(a)`), trailing defaults, and multiple return values as a zero-overhead calling convention.

Now that you have Goose's everyday syntax under your belt, **Chapter 3** dives straight into the core of the language: **size classes, data stacks, the stack invariant, and the in-place construction guarantee**.
