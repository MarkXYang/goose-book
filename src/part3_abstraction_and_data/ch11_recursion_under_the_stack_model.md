# Chapter 11: Recursion Under the Stack Model

In Chapter 3, you learned that the Goose compiler computes the exact number of data stacks `N` statically before your program ever runs. Pause and think about what that means for **recursion**:

If a recursive function declared a growable array `var buf: u8[>..] = [];` and kept `buf` alive while calling itself, a recursion of depth 50 would need 50 simultaneous data stacks—and depth 1,000 would need 1,000. Because recursion depth is a runtime property, the compiler cannot assign a static stack per recursive activation.

Rather than silently falling back to a heap allocator, Goose makes recursion explicit with **`recursive fn`** and enforces one clear rule: **no non-fixed local or temporary may stay alive across a recursive call.** Once you learn the four idiomatic patterns for working with this rule, recursive code in Goose runs purely on the native call stack with zero per-call allocations—and often turns out *cleaner* than the traditional heap-allocated version.

By the end of this chapter, you will be able to:

- Write `recursive fn` cycles and understand every compiler rule on signatures, locals, temporaries, and stores.
- Use **block-scoped scratch** when temporary buffers are needed only before or after recursing.
- Structure divide-and-conquer algorithms with **caller-owned buffers** (such as recursive merge sort) and **balanced `T[>..<]` scratch stacks**.
- Build clean recursive-descent parsers by **nesting recursive functions** inside a non-recursive driver alongside `return ... from`.

---

## 11.1 `recursive fn` and the Core Stack Rule

Recursion in Goose is opt-in and visible in the signature:

1. **Mark the entry of every recursive cycle** with `recursive fn` (for mutual recursion between `a` and `b`, mark whichever enters the cycle—or both).
2. **Write explicit parameter and return types** on every function in the cycle (no inferred return types across a cycle).
3. **No by-value non-fixed parameters:** A function in a recursive cycle takes arrays/containers by reference (`T[>..]&`) or slice (`T[:]`), never by value.
4. **No non-fixed locals or temporaries live across a recursive call:** A growable or variable-size local cannot span a call back into the cycle—nor can a non-fixed temporary in the calling statement itself (for example, `recurse(str("x", n))` or `print(recurse(n))` where `print` holds a scratch buffer while `recurse` runs).
5. **Local-rooted references stay local:** You may pass a reference to a cycle function's own fixed local *down* to a callee, but you cannot store it in an outer holder or return it (activations of the same local cannot be distinguished statically). References rooted *outside* the cycle (caller pools, enclosing non-recursive locals, globals) have no restriction.

Here is what happens when a recursive function tries to keep a growable local alive across its recursive call:

<!-- compile-fail -->
```goose
recursive fn bad_walk(n: i64) -> i64 {
    var buf: u8[>..] = [];           // holds a data stack until the end of `bad_walk`
    format(buf, "level ", n);
    if n > 0 {
        let sub = bad_walk(n - 1);   // error: `buf` is still in scope across recursive call!
        return sub + buf.len;
    }
    return buf.len;
}

fn main() {
    print(bad_walk(3));
}
```

> **Compiler Tip:** Look out for **temporaries in the calling statement** too: `print("depth=", count(root))` may fail if `print` opens a non-fixed formatting temporary while calling a recursive function. Bind the recursive result first (`let c = count(root); print("depth=", c);`), and the error vanishes immediately.

---

## 11.2 Pattern 1: Block-Scoped Scratch Before Recursing

If a recursive function needs a growable buffer only *before* (or *after*) making its recursive calls—for example, formatting a line of output—simply wrap that buffer in a `{ ... }` block that **closes before the recursive call**. At the closing `}`, the data stack rewinds cleanly, so zero data stacks are held during the recursion!

```goose
struct Node {
    key: i32,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

recursive fn print_tree(n: Node&, depth: i64) {
    {
        // Scratch lives ONLY inside this block, freed before we recurse!
        var line: u8[>..] = [];
        for i in depth { line.append("  "); }
        format(line, "- ", n.key);
        print(line);
    }
    let l = n.left;
    if l { print_tree(l, depth + 1); }
    let r = n.right;
    if r { print_tree(r, depth + 1); }
}

recursive fn count_nodes(n: Node?) -> i64 {
    guard n else { return 0; }
    return 1 + count_nodes(n.left) + count_nodes(n.right);
}

fn main() {
    var pool: Node[>..] = [];
    let root .= pool.push(Node { key: 50 });
    let left .= pool.push(Node { key: 25 });
    let right .= pool.push(Node { key: 75 });
    left.left .= pool.push(Node { key: 10 });
    root.left .= left;
    root.right .= right;

    print_tree(root, 0);
    let total = count_nodes(root);
    print("total nodes: ", total);
}
```

```
- 50
  - 25
    - 10
  - 75
total nodes: 4
```

---

## 11.3 Pattern 2: Caller-Owned Buffers (Recursive Merge Sort)

For divide-and-conquer algorithms that need working storage proportional to the input size, allocate **one** scratch buffer in the non-recursive caller and pass slices down through the recursion. Every recursive level splits the slices (`xs[..mid]`, `tmp[..mid]`) with zero allocations and zero index-offset bookkeeping:

```goose
import std;

fn insertion_sort(xs: i32[:]) {
    for i in 1..xs.len {
        let v = xs[i];
        var j = i;
        while j > 0 && xs[j - 1] > v {
            xs[j] = xs[j - 1];
            j--;
        }
        xs[j] = v;
    }
}

recursive fn merge_sort(xs: i32[:], tmp: i32[:]) {
    if xs.len <= 4 {
        insertion_sort(xs);
        return;
    }
    let mid = xs.len / 2;
    merge_sort(xs[..mid], tmp[..mid]);
    merge_sort(xs[mid..], tmp[mid..]);

    var i = 0;
    var j = mid;
    var k = 0;
    while i < mid && j < xs.len {
        if xs[j] < xs[i] { tmp[k] = xs[j]; j++; }
        else             { tmp[k] = xs[i]; i++; }
        k++;
    }
    while i < mid    { tmp[k] = xs[i]; i++; k++; }
    while j < xs.len { tmp[k] = xs[j]; j++; k++; }
    xs.move_from(tmp[..xs.len]);
}

fn main() {
    var nums: i32[>..] = [42, 7, 19, 3, 88, 12, 5, 29, 1, 14];
    var scratch: i32[>..] = [];
    scratch.resize(nums.len, 0);     // one scratch buffer for the entire recursion

    merge_sort(nums, scratch);
    print("sorted: ", nums);
}
```

```
sorted: [1, 3, 5, 7, 12, 14, 19, 29, 42, 88]
```

Notice how fast this is: even sorting a million integers allocates the scratch buffer **once** in `main`, and every recursive call uses only a tiny native stack frame.

---

## 11.4 Pattern 3: Shared Grow-Shrink Scratch with Balanced Calls

What if each recursive activation needs dynamic temporary storage whose size is *not* known ahead of time—and that storage **must** stay alive across its recursive children? (Think of clipping a polygon of arbitrary corner count during a recursive BSP walk, or collecting a variable number of child results before reducing them.)

Pass a single grow-shrink array **`T[>..<]&`** from the caller and apply the **balanced call** pattern from Chapter 6:

1. Save `let mark = scratch.len;` on entry.
2. Push this activation's own working elements onto `scratch`.
3. Take a slice `let mine = scratch[mark..];` covering this activation's segment.
4. Make your recursive calls—each child pushes *above* `mine` and restores `scratch.resize(child_mark)` before returning!
5. Read `mine` safely after the recursion returns, then finish with `scratch.resize(mark);`!

```goose
// Each depth level pushes `depth + 1` values onto `scratch`, keeps a slice `mine`
// alive across its recursive child call, reads `mine` afterward, and restores `mark`.
recursive fn nested_work(scratch: i64[>..<]&, depth: i64) -> i64 {
    let mark = scratch.len;
    for k in depth + 1 {
        scratch.push(depth * 10 + k);
    }
    let mine = scratch[mark..];      // stays valid across the balanced recursive call!

    var sub = 0;
    if depth > 0 {
        sub = nested_work(scratch, depth - 1);
    }

    var local_sum = 0;
    for v in mine { local_sum += v; }

    scratch.resize(mark);            // balanced: caller's slice below `mark` survives!
    return local_sum + sub;
}

fn main() {
    var scratch: i64[>..<] = [];
    let total = nested_work(scratch, 3);
    print("total=", total, " scratch.len=", scratch.len);
}
```

```
total=210 scratch.len=0
```

Every activation shares **one** data stack in perfect LIFO harmony, with no fixed upper bound on per-level elements and zero heap allocations!

---

## 11.5 Pattern 4: Nested Recursive Functions Inside a Driver

When writing a recursive-descent parser, interpreter, or compiler pass, you usually have several tables (a token array, an AST pool, a cursor index, the source text).

Instead of declaring a giant `ParserState` struct and passing it to 15 recursive functions, declare the tables as **locals of one non-recursive driver function** and nest the recursive grammar functions right inside it!

- The driver owns every growable array (`tokens`, `nodes`, etc.).
- Every nested recursive function accesses those locals directly as free variables—which are rooted *outside* the recursive cycle!
- Any deep syntax error simply executes `return ..., str(...) from driver;` (Chapter 10), unwinding every recursive parse frame back to the driver!

---

## 11.6 Worked Example: A Recursive-Descent Expression Evaluator

Let us build a clean, self-contained arithmetic calculator using Pattern 4 (nested mutually recursive functions inside `evaluate`, fixed-mode `Token[>..]`, operator precedence `+`/`-`, `*`/`/`, unary `-`, parentheses `(...)`, and `return ... from evaluate` for syntax & runtime errors):

```goose
import std;

enum Token {
    Num { v: f64 },
    Op  { c: u8 },
    End,
}

fn evaluate(src: const u8[:]) -> f64, u8[] {
    var tokens: Token[>..] = [];     // owned outside the recursive cycle!
    var pos = 0;

    fn tokenize() {
        var i = 0;
        while i < src.len {
            let c = src[i];
            if c == ' ' || c == '\t' { i++; continue; }
            if ascii::is_digit(c) || c == '.' {
                let start = i;
                while i < src.len && (ascii::is_digit(src[i]) || src[i] == '.') { i++; }
                let v, ok = parse_flt(src[start..i]);
                guard ok else {
                    return 0.0, str("bad number '", src[start..i], "'") from evaluate;
                }
                tokens.push(Token.Num { v: v });
                continue;
            }
            guard find("+-*/()", src[i..i + 1]) >= 0 else {
                return 0.0, str("unexpected '", src[i..i + 1], "'") from evaluate;
            }
            tokens.push(Token.Op { c: c });
            i++;
        }
        tokens.push(Token.End);
    }

    fn peek_op(c: u8) -> bool {
        match tokens[pos] { Op o => o.c == c, _ => false }
    }

    // Grammar:
    //   expr   := term (('+' | '-') term)*
    //   term   := unary (('*' | '/') unary)*
    //   unary  := '-' unary | atom
    //   atom   := Num | '(' expr ')'
    recursive fn parse_expr() -> f64 {
        var v = parse_term();
        loop {
            if peek_op('+')      { pos++; v += parse_term(); }
            else if peek_op('-') { pos++; v -= parse_term(); }
            else                 { break; }
        }
        return v;
    }

    fn parse_term() -> f64 {
        var v = parse_unary();
        loop {
            if peek_op('*') { pos++; v *= parse_unary(); }
            else if peek_op('/') {
                pos++;
                let d = parse_unary();
                guard d != 0.0 else { return 0.0, "division by zero" from evaluate; }
                v /= d;
            } else { break; }
        }
        return v;
    }

    recursive fn parse_unary() -> f64 {
        if peek_op('-') { pos++; return -parse_unary(); }
        return parse_atom();
    }

    fn parse_atom() -> f64 {
        match tokens[pos] {
            Num n => { pos++; return n.v; },
            Op o  => {
                guard o.c == '(' else {
                    return 0.0, "expected number or '('" from evaluate;
                }
                pos++;
                let v = parse_expr();
                guard peek_op(')') else {
                    return 0.0, "missing closing ')'" from evaluate;
                }
                pos++;
                return v;
            },
            End => { return 0.0, "unexpected end of input" from evaluate; },
        }
    }

    tokenize();
    let result = parse_expr();
    match tokens[pos] {
        End => { return result, ""; },
        _   => { return 0.0, "trailing tokens after expression" from evaluate; },
    }
}

fn main() {
    let cases: const u8[:][6] = [
        "1 + 2 * 3",
        "(1 + 2) * (10 - 4) / 2",
        "-3 * -(2 + 5)",
        "10 / (3 - 3)",
        "(1 + 2",
        "4 + @",
    ];

    for expr in cases {
        let val, err = evaluate(expr);
        if err.len > 0 { print(expr, "  => ERROR: ", err); }
        else           { print(expr, "  => ", val); }
    }
}
```

```
1 + 2 * 3  => 7.0
(1 + 2) * (10 - 4) / 2  => 9.0
-3 * -(2 + 5)  => 21.0
10 / (3 - 3)  => ERROR: division by zero
(1 + 2  => ERROR: missing closing ')'
4 + @  => ERROR: unexpected '@'
```

Click **Stacks** on this calculator! Every nested recursive rule (`parse_expr`, `parse_term`, `parse_unary`, `parse_atom`) uses **zero** data stacks of its own—all token storage lives in `evaluate`'s single `tokens` array and vanishes in one pointer store the instant `evaluate` returns.

---

## Exercises

1. **Add exponentiation (`^`) to the calculator:** Extend Section 11.6 to recognize `'^'` (right-associative, higher precedence than `*`/`/` and lower than unary `-`, using `import math;` and `pow(base, exp)`). Verify that `2 ^ 3 ^ 2` evaluates to `512.0`.
2. **Try a balanced recursive permutation or subset generator:** Using Pattern 3 (`scratch: i64[>..<]&` with `mark` / `resize(mark)`), write a `recursive fn` that computes all sums of subsets of `[1, 2, 4]` and pushes them into a caller-owned `out: i64[>..]&`.
3. **Spot a hidden temporary across recursion:** Inside Section 11.2's `main()`, try replacing `let total = count_nodes(root); print("total nodes: ", total);` with a single line `print("total nodes: ", count_nodes(root));`. Check whether `print` accepts it or rejects a live temporary—and test `print_tree` calling `print(str(...))` directly vs. inside `{ ... }`.

---

## Key Takeaways

- Mark recursive cycles explicitly with **`recursive fn`** and write out full signatures across the cycle.
- **Core rule:** Never keep a non-fixed local or temporary alive across a recursive call—recursion uses the native call stack only, never dynamically growing sets of data stacks.
- Master four clean patterns:
  1. **Block-scoped scratch** (`{ var buf: u8[>..] = []; ... }` closed before recursing).
  2. **Caller-owned buffers/slices** (`merge_sort(xs, tmp)`).
  3. **Balanced `T[>..<]` scratch** (`let mark = s.len; ... s.resize(mark);`).
  4. **Nested recursive functions** inside a non-recursive driver sharing its locals alongside `return ... from`.

In **Chapter 12: Relative References and Pointer-Free Data Structures**, we turn our attention to building linked lists, binary search trees, directory trees, and ASTs using **1-, 2-, and 4-byte relative offsets**—structures that are dramatically smaller than pointer graphs and completely position-independent!
