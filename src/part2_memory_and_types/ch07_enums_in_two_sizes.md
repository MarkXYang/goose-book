# Chapter 7: Enums in Two Sizes and Tag Dispatch

Goose has no inheritance, no classes, no interfaces, and no vtables. Its sole form of dynamic polymorphism is the **algebraic data type (`enum`)**—a tagged union where every possible variant is known to the compiler.

If you have used Rust's `enum` or C++'s `std::variant`, Goose's enums will look familiar at first—until you meet the two ideas that set them apart:

1. **Two storage modes, chosen at every use site:** An enum can be stored in **fixed mode** (`Shape`, like Rust or C++—every element takes the size of the largest variant) or in **variable mode** (`Shape..`, where each element takes *only* the size of its own variant, inline strings included).
2. **Case functions:** Instead of repeating giant `match` blocks everywhere, you can write one function overload per variant type (`Shape.Circle`, `Shape.Rect`, `Shape.Dot`) and call it on the enum. The compiler checks exhaustiveness and dispatches on the tag through a jump table.

By the end of this chapter, you will be able to:

- Declare C-like, payload-bearing, and generic enums and match them exhaustively.
- Choose between **fixed mode (`E`)** and **variable mode (`E..`)** and explain the safety trade-off between variant replacement and interior references.
- Pack variable-size strings directly inside variable-mode enums with zero pointers.
- Write exhaustive **case functions** (both by value and by reference) as Goose's vtable-free virtual dispatch idiom.

---

## 7.1 Declaring Enums and Exhaustive `match`

An `enum` lists its variants. Each variant may carry struct-like fields (with optional defaults and `..` partial literals) or carry no payload at all:

```goose
enum Shape {
    Circle { r: f64 },
    Rect { w: f64, h: f64 },
    Dot,
}

enum Maybe<T> {
    None,
    Some { val: T },
}

fn describe(s: Shape) -> const u8[:] {
    match s {
        Circle c => if c.r > 2.0 { "large circle" } else { "circle" },
        Rect r   => if r.w == r.h { "square" } else { "rectangle" },
        Dot      => "dot",
    }
}

fn is_round(s: Shape) -> bool {
    match s {
        Circle, Dot => true,         // multiple variants in one arm (no payload bound)
        Rect        => false,
    }
}

fn main() {
    let c = Shape.Circle { r: 3.0 };
    let r = Shape.Rect { w: 2.0, h: 2.0 };
    let d = Shape.Dot;
    let m: Maybe<i64> = Maybe<i64>.Some { val: 42 };

    print(describe(c), ", ", describe(r), ", ", describe(d));
    print("round: ", is_round(c), " ", is_round(r));
    match m {
        Some s => print("maybe=", s.val),
        None   => print("empty"),
    }
}
```

```
large circle, square, dot
round: true false
maybe=42
```

Notice the rules for `match` on an enum:

- **Enum type vs. variant type:** Without a type annotation, `let c = Shape.Circle { r: 3.0 };` gives `c` the concrete *variant type* `Shape.Circle` (it converts implicitly to `Shape` when passed to `describe(c)`). To bind a local as the general enum type so you can `match` on it directly, annotate it (`let m: Maybe<i64> = ...`).

- **Exhaustive:** Every variant must appear in an arm, or a trailing `_ => ...` arm must cover the rest. Naming a variant twice is a compile error.
- **Expression-oriented:** `match` evaluates to a value when its arms produce a common type.
- **An enum with no payloads** (like `enum Color { Red, Green, Blue }`) takes just **1 byte**—a C-like enum with exhaustive matching.

---

## 7.2 Two Storage Modes: Fixed (`Shape`) vs. Variable (`Shape..`)

In Rust and C++, every value of an `enum` or `std::variant` reserves enough bytes for its **largest** variant. If 80% of your nodes are 1-byte `Dot`s or tiny integers and one rare variant carries a 64-byte payload, every single element pays 65 bytes.

In Goose, you decide the storage mode **at each container or variable** simply by writing `Shape` or `Shape..`:

```text
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }
Elements pushed: [ Circle(1.0), Rect(2.0, 3.0), Dot, Dot ]

Fixed mode — Shape[>..]  (17 bytes per element = 68 bytes total):
+---+--------+--------+---+--------+--------+---+-----------------+---+-----------------+
| C |  1.0   | (pad)  | R |  2.0   |  3.0   | D |    (unused)     | D |    (unused)     |
+---+--------+--------+---+--------+--------+---+-----------------+---+-----------------+
  1B    8B       8B    1B     8B       8B    1B         16B        1B         16B

Variable mode — Shape..[>..]  (9 + 17 + 1 + 1 = 28 bytes total!):
+---+--------+---+--------+--------+---+---+
| C |  1.0   | R |  2.0   |  3.0   | D | D |
+---+--------+---+--------+--------+---+---+
  1B    8B    1B     8B       8B    1B  1B
```

### The Soundness Trade-Off

Why are there two modes instead of one? Because allowing **both** overwriting a variant in place *and* holding a reference into its payload would cause type confusion (if you held `r: Rect&` while someone replaced the enum with a `Circle`, `r` would read `Circle` bytes as a `Rect`!).

Goose solves this cleanly without needing a borrow checker by giving each mode one half of the pair:

| Property | Fixed Mode (`Shape`) | Variable Mode (`Shape..`) |
|---|---|---|
| Size class | **fixed** (`1 + max payload`) | **variable** (`1 + own payload`) |
| Array access | **Indexable** (`shapes[i]`) and poppable | **Sequential** (iterated with `for`, or indexed via a side array of references) |
| Replace variant in place? | **Yes** (`shapes[1] = Shape.Circle { ... }`) | **No** (tag is frozen forever; neighbors sit right behind it) |
| Reference into payload (`Rect &r`)? | **No** (`match` copies payload by value only) | **Yes** (`match s { Rect &r => r.w += 1.0; }` mutates in place!) |
| Variable-size payload fields (`u8[varint]`)? | **No** (all payloads must be fixed) | **Yes** (strings and arrays pack inline with zero pointers!) |

Let us watch both modes run side by side and measure their exact byte sizes with `bytes_of(...).len`:

```goose
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }

fn main() {
    // Fixed mode: indexable and variant-overwritable, 17 bytes each.
    var fixed: Shape[>..] = [];
    fixed.push(Shape.Circle { r: 1.0 });
    fixed.push(Shape.Rect { w: 2.0, h: 3.0 });
    fixed.push(Shape.Dot);
    fixed.push(Shape.Dot);
    fixed[2] = Shape.Circle { r: 5.0 };          // replace Dot with Circle in place!
    print("fixed bytes=", bytes_of(fixed).len, " fixed[2]=", fixed[2]);

    // Variable mode: exact variant size (9 + 17 + 1 + 1 = 28 bytes) + in-place payload edit!
    var packed: Shape..[>..] = [];
    packed.push(Shape.Circle { r: 1.0 });
    packed.push(Shape.Rect { w: 2.0, h: 3.0 });
    packed.push(Shape.Dot);
    packed.push(Shape.Dot);

    for s in packed {                            // variable-size elements bind by reference
        match s {
            Rect &r => { r.w += 10.0; },         // edits payload straight inside `packed`!
            _       => {},
        }
    }
    print("packed bytes=", bytes_of(packed).len, " packed=", packed);
}
```

```
fixed bytes=68 fixed[2]=Circle { 5.0 }
packed bytes=28 packed=[Circle { 1.0 }, Rect { 12.0, 3.0 }, Shape.Dot, Shape.Dot]
```

If you try to take an interior payload reference (`Rect &r`) from a **fixed-mode** enum, the compiler rejects it immediately to preserve type safety:

<!-- compile-fail -->
```goose
enum Shape { Circle { r: f64 }, Rect { w: f64, h: f64 }, Dot }

fn main() {
    var fixed: Shape[>..] = [];
    fixed.push(Shape.Rect { w: 2.0, h: 3.0 });
    match fixed[0] {
        Rect &r => { r.w += 1.0; },  // error: cannot take payload reference into fixed-mode enum
        _       => {},
    }
}
```

> **Memory Model:** On heterogeneous workloads (such as event logs, compiler ASTs, or mixed record streams where small variants outnumber large ones), variable-mode enums are one of Goose's biggest performance weapons: the benchmark suite measures roughly **4x less memory and 2x the speed** compared to a fixed-size Rust `enum` on the `records` benchmark.

---

## 7.3 Variant Types and Case Functions

Every variant of an enum is also a **standalone type**: `Shape.Circle`, `Shape.Rect`, `Shape.Dot`. You can declare variables, struct fields, parameters, and references (`Shape.Rect&`) of a specific variant type directly.

That enables **case functions**—Goose's clean alternative to virtual methods:

1. Define one overload per variant (`fn area(s: Shape.Circle)`, `fn area(s: Shape.Rect)`, `fn area(s: Shape.Dot)`).
2. Call `area(s)` with an enum value (`Shape` or `Shape..`).
3. The compiler verifies that **every variant has exactly one matching overload**, checks that all return types agree, and generates a direct **jump table on the tag**—with no vtable pointer stored in any object!

Even better: if an overload takes a variant **by reference** (`s: Shape.Rect&`), calling it on a **variable-mode** enum (`Shape..`) binds and mutates the payload in place!

```goose
import math;

enum Shape {
    Circle { r: f64 },
    Rect { w: f64, h: f64 },
    Dot,
}

// By-value case functions: work on BOTH fixed `Shape` and variable `Shape..`!
fn area(s: Shape.Circle) -> f64 { PI * s.r * s.r }
fn area(s: Shape.Rect) -> f64   { s.w * s.h }
fn area(s: Shape.Dot) -> f64    { 0.0 }

// By-reference mutating case functions: work on variable-mode `Shape..`!
fn scale(s: Shape.Circle&, k: f64) { s.r *= k; }
fn scale(s: Shape.Rect&, k: f64)   { s.w *= k; s.h *= k; }
fn scale(s: Shape.Dot&, k: f64)    {}

fn main() {
    var packed: Shape..[>..] = [];
    packed.push(Shape.Circle { r: 1.0 });
    packed.push(Shape.Rect { w: 2.0, h: 3.0 });
    packed.push(Shape.Dot);

    var before = 0.0;
    for s in packed { before += s.area(); }      // UFCS + tag dispatch!

    for s in packed { s.scale(2.0); }            // in-place mutation via case function!

    var after = 0.0;
    for s in packed { after += s.area(); }

    let c = Shape.Circle { r: 2.0 };             // variant type used directly (no dispatch)
    print("before=", before, " after=", after, " direct=", c.area());
}
```

```
before=9.141592653589793 after=36.56637061435917 direct=12.566370614359172
```

Notice that `c.area()` on a concrete `Shape.Circle` calls `area(Shape.Circle)` directly with zero tag check, while `s.area()` on `Shape..` dispatches cleanly on the tag. And if you later add a `Tri` variant to `Shape`, the compiler immediately flags every case-function call site until `Tri` gets its own overloads!

---

## 7.4 Preview: Self-Referential Variable-Mode Trees

Combine variable-mode enums with narrow relative references (`Expr..&<u16>`, covered in Chapter 12) and case functions, and an entire expression tree packs into a handful of contiguous bytes:

```goose
enum Expr {
    Num { v: f64 },
    Add { l: Expr..&<u16>, r: Expr..&<u16> },
    Neg { x: Expr..&<u16> },
}

fn eval(e: Expr.Num&) -> f64           { e.v }
recursive fn eval(e: Expr.Add&) -> f64 { eval(e.l) + eval(e.r) }
recursive fn eval(e: Expr.Neg&) -> f64 { -eval(e.x) }

fn main() {
    var pool: Expr..[>..] = [];
    let a = pool.push(Expr.Num { v: 10.5 });     // 9 bytes (1 tag + 8 f64)
    let b = pool.push(Expr.Num { v: 4.5 });      // 9 bytes
    let sum = pool.push(Expr.Add { l: a, r: b });// 5 bytes (1 tag + 2 + 2)
    let neg = pool.push(Expr.Neg { x: sum });    // 3 bytes (1 tag + 2)
    print("result=", eval(neg), " total=", bytes_of(pool).len, " bytes");
}
```

```
result=-15.0 total=26 bytes
```

Every `Neg` node takes just **3 bytes** and every `Add` takes **5 bytes**—no wasted padding for `f64`, no heap allocations, and direct jump-table dispatch!

---

## 7.5 Worked Example: Choosing Both Modes in One Program

To cement when to pick fixed mode vs. variable mode, let us build a small two-part telemetry program:

1. **A Mini Stack VM (Fixed Mode `Op`):** Instructions live in `Op[>..]` so the program counter can **index** `code[pc]` in O(1) and an assembler pass can **patch** a jump instruction in place (`code[1] = Op.JumpIfZero { to: ... }`).
2. **An Inline Event Stream (Variable Mode `Event..`):** Heterogeneous events—1-byte `Tick`, 9-byte `Gauge`, and variable-length `Note` carrying an inline `u8[varint]` string—pack into one flat `Event..[>..]` with a side index `(Event..&)[>..]` for random access, mutated and summarized through exhaustive **case functions**!

```goose
// --- Part 1: Fixed-mode bytecode (indexable & patchable) ------------------
enum Op {
    Push { v: i64 },
    Add,
    Mul,
    JumpIfZero { to: u16 },
    Halt,
}

fn run_vm(code: Op[:]) -> i64 {
    var stack: i64[>..<] = [];
    var pc = 0;
    loop {
        let op = code[pc];
        pc++;
        match op {
            Push p       => { stack.push(p.v); },
            Add          => { let b = stack.pop(); let a = stack.pop(); stack.push(a + b); },
            Mul          => { let b = stack.pop(); let a = stack.pop(); stack.push(a * b); },
            JumpIfZero j => { if stack.pop() == 0 { pc = j.to; } },
            Halt         => { break; },
        }
    }
    return stack.pop();
}

// --- Part 2: Variable-mode events with inline text & case functions -------
enum Event {
    Tick,
    Gauge { id: i32, value: f32 },
    Note  { msg: u8[varint] },       // variable-size field -> variable mode only!
}

fn describe_event(e: Event.Tick&) -> u8[>..]  { str("tick") }
fn describe_event(e: Event.Gauge&) -> u8[>..] { str("gauge#", e.id, "=", e.value) }
fn describe_event(e: Event.Note&) -> u8[>..]  { str("note:", e.msg) }

fn calibrate(e: Event.Tick&, delta: f32)  {}
fn calibrate(e: Event.Gauge&, delta: f32) { e.value += delta; }
fn calibrate(e: Event.Note&, delta: f32)  {}

fn main() {
    // 1. Assemble and patch a tiny fixed-mode program: compute (6 + 4) * 3 = 30.
    var code: Op[>..] = [];
    code.push(Op.Push { v: 6 });
    code.push(Op.Push { v: 0 });     // placeholder, patched below!
    code.push(Op.Add);
    code.push(Op.Push { v: 3 });
    code.push(Op.Mul);
    code.push(Op.Halt);
    code[1] = Op.Push { v: 4 };      // overwrite variant/payload in place
    print("vm result=", run_vm(code), " (", bytes_of(code).len, " bytes for ", code.len, " ops)");

    // 2. Pack heterogeneous events inline and keep an index of references beside them.
    var events: Event..[>..] = [];
    var index: (Event..&)[>..] = [];
    index.push(events.push(Event.Tick));
    index.push(events.push(Event.Gauge { id: 1, value: 20.5 }));
    index.push(events.push(Event.Note { msg: "boot ok" }));
    index.push(events.push(Event.Tick));
    index.push(events.push(Event.Note { msg: "ready" }));

    // Mutate all gauges in place via a by-reference case function:
    for e in events { e.calibrate(1.5); }

    // Random access in reverse order via `index`:
    print("packed ", events.len, " events in ", bytes_of(events).len, " bytes:");
    for idx in [4, 1, 2, 0] {
        print("  [", idx, "] ", describe_event(index[idx]));
    }
}
```

```
vm result=30 (54 bytes for 6 ops)
packed 5 events in 27 bytes:
  [4] note:ready
  [1] gauge#1=22.0
  [2] note:boot ok
  [0] tick
```

Look at those numbers: 5 heterogeneous events—including two variable-length strings `"boot ok"` and `"ready"`, a gauge, and two ticks—occupy just **27 contiguous bytes and zero heap allocations**, while still allowing both in-place payload mutation and O(1) indexed access through `index`!

---

## Exercises

1. **Experience exhaustive case-function checking:** In Section 7.3, add a fourth variant `Square { side: f64 }` to `enum Shape`. Click **▶ Run** without adding its `area` or `scale` overloads yet, read Goose's exact diagnostic, and then add both overloads.
2. **Measure the mode savings:** In Section 7.2, push 100 `Shape.Dot` values onto both `fixed` and `packed`, and compare `bytes_of(fixed).len` vs. `bytes_of(packed).len`.
3. **Extend the Mini VM:** Add a `Sub` variant and a `Dup` variant to `enum Op` in Section 7.5, implement them in `run_vm`, and assemble a program that computes `(7 * 7) - 9`.

---

## Key Takeaways

- Algebraic data types (`enum`) are Goose's sole form of dynamic polymorphism—exhaustive, zero-vtable, and packed inline.
- Every enum supports two storage modes chosen at the point of use:
  - **Fixed mode (`E`)**: `1 + max payload` bytes per value; indexable and variant-replaceable; no interior payload references.
  - **Variable mode (`E..`)**: `1 + own payload` bytes per value; sequential and tag-frozen; allows interior payload references (`Variant &v`) and inline variable-size fields (`u8[varint]`).
- **Case functions** dispatch on an enum's tag through an exhaustive jump table by writing one overload per variant type (`E.Variant` or `E.Variant&`).

In **Chapter 8: Structs, Root Structs, and Non-Nested Lifetimes**, we round out Part II by examining struct layout, `root struct` for bundling multiple growable containers under one owner, and the standard library's `slot_pool<T>` and `slice_pool<T>` for data whose lifetimes do not nest.
