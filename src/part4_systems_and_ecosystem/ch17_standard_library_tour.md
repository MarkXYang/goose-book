# Chapter 17: Tour of the Standard Library

Goose ships with **20 standard library modules** under `stdlib/`, almost all of them written in clean, readable Goose. Whenever you wonder how an idiomatic data structure or parser is structured in Goose, opening `stdlib/dictionary.goose`, `stdlib/json.goose`, or `stdlib/regex.goose` is one of the best references you can read.

Every module follows the same conventions:

- Read or mutate elements in place → `T[:]`
- Grow or shrink an array → `A&` (generic over array kind)
- Return a fresh collection → `T[>..]` constructed in place at the caller's destination
- "Maybe an element" → `T?`; positions → `i64` (`-1` if absent); two-outcome scalars → trailing `bool`
- Text output appends into `u8[>..]&` builders; `str(...)` builds fresh text

| Global Namespace Modules | Own-Namespace Modules (`ns::name`) |
|---|---|
| `std`, `dictionary`, `slot_pool`, `slice_pool`, `vec`, `mat`, `quat`, `math`, `os` | `exception`, `ffi`, `binary`, `base64`, `csv`, `json`, `regex`, `gfx`, `audio`, `physics`, `ui` |

By the end of this chapter, you will be able to:

- Reach immediately for `std` algorithms, bit operations, `rng`, `ByteSet`, and `dictionary<K, V>`.
- Use `vec`, `mat`, `quat`, and `math` for 2D/3D linear algebra without confusing elementwise `*` and `mul`.
- Parse, transform, and emit external data with `binary`, `base64`, `csv`, `json`, and `regex`.
- Know when to pull in `os` and the four multimedia packages (`gfx`, `audio`, `physics`, `ui`).

---

## 17.1 Core Utilities (`std`) and Hash Tables (`dictionary`)

### Highlights of `std`

Beyond the string helpers you met in Chapter 4 (`trim`, `split`, `parse_int`, `each_utf8`), `std` provides:

- **Scalars & bits:** `min`, `max`, `clamp`, `abs`, `sign`, `lerp`, `div_euclid`, `rem`, `isqrt`, `gcd`, `lcm`, `is_pow2`, `next_pow2`, `ilog2`, `popcount`, `clz`, `ctz`, `rotl`, `rotr`, `byte_swap`, `swap`.
- **Hashing & PRNG:** `hash` (integers, floats, `bool`, `u8[:]`), `hash_combine`, and `rng` (splitmix64: `rand_u64`, `rand_range`, `rand_flt`, `rand_gaussian`, `shuffle`).
- **Searching, sorting, heaps:** `find`, `position`, `contains`, `any`, `all`, `count`, `lower_bound`, `binary_search`, `sort`, `stable_sort`, `reverse`, `dedup`, `retain`, `filter`, `map`, `fold`, `heap_push`, `heap_pop`.
- **SIMD byte scanning:** `ByteSet` (`byte_set(",\"\n")`) with `find_any` and `find_pair` (16 bytes at a time via SSE2 on x86-64).

### `dictionary<K, V>`

`dictionary<K, V>` is an open-addressing hash table with 31-bit hash tags and backward-shift deletion. Both `K` and `V` must be fixed-size (`const u8[:]` slices or `u8[..k]` small strings make great zero-copy keys; `dictionary<K, bool>` acts as a set). For a custom struct key, simply define `fn hash(k: MyKey) -> u64` using `hash_combine`.

> **Watch Out:** `dictionary`'s internal slot array is grow-shrink (`[>..<]`), so finish using any reference returned by `d.get(k)` before calling `d.insert`, `d.update`, `d.remove`, or `d.clear`—or use `d.update(k, default_val) { it += 1; }` to look up and mutate in a single call.

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

## 17.2 Math and Linear Algebra (`vec`, `mat`, `quat`, `math`)

Goose includes GLSL/GLM-inspired 2D and 3D math modules—all `checkfree`-friendly (zero remaining bounds checks!):

- **`vec`:** `vec2<T>`, `vec3<T>`, `vec4<T>`, plus aliases `float2/3/4`, `double2/3/4`, `int2/3/4`, `dot`, `cross`, `length`, `normalize`, `distance`, `lerp`, `xy`, `xyz`.
- **`mat`:** Column-major `float2x2`, `float3x3`, `float4x4` (and `double*`), `translate`, `rotate`, `scale`, `perspective`, `ortho`, `look_at`, `transpose`, `inverse`, `transform_point`, `transform_vector`.
- **`quat`:** `floatq` / `doubleq`, `angle_axis`, `rotate`, `slerp`, `from_euler`, `mat4_cast`, `compose`.
- **`math`:** Full `f32`/`f64` libm (`sqrt`, `sin`, `cos`, `atan2`, `pow`, `exp`, `log`, `floor`, `ceil`, `mul_add`) plus `PI` and `radians`.

> **Watch Out:** Because `*` is Goose's built-in **elementwise** operator on structs and fixed arrays, `m1 * m2` multiplies matrices entry-by-entry (like GLSL's `matrixCompMult`). Always use **`mul(m1, m2)`** for matrix multiplication and quaternion Hamilton products!

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

## 17.3 External Data Formats and Regex (`binary`, `base64`, `csv`, `json`, `regex`)

Five focused, namespaced modules handle everyday binary and text formats with zero heap allocations:

- **`binary`:** Checked little- and big-endian reads/writes (`read_u16le`, `read_u32be`, `write_u64le`, `reader`, `subreader`) for external file formats like PNG, WAV, or Doom WADs (Chapter 13 used `binary` for our `"GSX1"` header).
- **`base64`:** RFC 4648 `base64::encode(out, src)` and `base64::decode(out, src) -> bool`, implemented as auto-vectorized `simd fn`s.
- **`csv`:** RFC 4180 zero-copy `csv::each_record(text) { fields => ... }` (reusing one slice buffer per record!) and `csv::format_field(out, s)`.
- **`json`:** Three styles in one module:
  1. **Streaming writer** (`json::format_key`, `format_string`, `format_number`, `format_int`, `format_bool`).
  2. **Document tree** (`json::parse(text, doc, decoded)` pushing self-relative `json::Node`s + `member` / `each` / `as_*`).
  3. **Zero-allocation streaming reader** (`json::reader(text)` + `each_element` / `each_member` / `read_int` / `skip` / `finish`).
- **`regex`:** DFA-based linear-time regular expressions (`regex::compile`, `find`, `matches`, `each_captures`, `format_replaced`, `regex::group`), using SIMD `ByteSet` prefilters to skip non-matching regions fast.

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

> **Tip:** Compile each `Regex` **once** outside your search loop (`regex::compile` builds forward and reverse DFAs up front so each search runs in one table lookup per byte).

---

## 17.4 System (`os`) and Interactive Packages (`gfx`, `audio`, `physics`, `ui`)

### `os`

`import os;` provides portable OS services (global namespace):

- **CLI arguments:** `args()`, `arg_count()`, `arg(i, out)`
- **Files & directories:** `read_file`, `write_file`, `append_file`, `write_file_atomic`, `rename_file`, `delete_file`, `file_exists`, `list_dir`, `resource_dir`
- **Time, entropy, memory, stderr:** `clock()` (high-resolution seconds), `sleep_ms`, `random_seed()`, `trim_memory()`, `write_stderr`

### The Four Multimedia & Simulation Packages

Goose also ships four blessed native packages (pre-compiled into release binaries and `-DGOOSE_BUILTIN_LIBS=all` builds):

- **`gfx` (SDL3 GPU API):** Modern cross-platform 2D/3D GPU rendering and compute shaders across Vulkan, Metal, and Direct3D 12, with compile-time `embed_shader` (GLSL → SPIR-V/MSL/HLSL), keyboard/mouse/gamepad input, and headless offscreen rendering (`gfx::open_headless` / `GOOSE_GFX_HEADLESS=1`).
- **`audio` (SDL3 PCM mixer):** Procedural mono/stereo sound playback usable with or without a graphics window.
- **`physics` (Box3D):** Multi-threaded rigid-body 3D physics with batch transform retrieval for instanced rendering.
- **`ui` (Nuklear + font baking over `gfx`):** Crisp TTF text rendering, non-interactive game HUDs (`ui::label`, `ui::draw_text`), and full immediate-mode windows, buttons, sliders, and color pickers.

We will build a complete interactive application using these packages in **Chapter 22 (Capstone 3)**!

---

## 17.5 Worked Example: A CSV-to-JSON Telemetry Pipeline

Let us tie `csv`, `regex`, `base64`, `dictionary`, and `json` together into a realistic zero-heap-allocation ETL script: it reads CSV rows, extracts `env=...` tags with a compiled DFA regex, base64-encodes a token, counts records per environment, and formats a properly escaped JSON report:

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

Notice how effortlessly five stdlib modules compose: `csv::each_record` yields zero-copy slices, `regex::each_captures` extracts groups with a DFA, `base64::encode` encodes with SIMD, `json::format_*` escapes cleanly into one `u8[>..]` builder, and `json::parse` validates the output into a self-relative `json::Node[>..]` tree!

---

## Exercises

1. **Test `json::parse` error reporting:** In Section 17.5, break the generated JSON (e.g., omit the closing `]`) and inspect the exact byte-offset diagnostic returned by `json::parse` (without aborting—print `err`).
2. **Use a binary heap from `std`:** Import `std`, push `[40, 10, 30, 50, 20]` onto a `var h: i64[>..<] = [];` using `heap_push(h, x)`, and pop all five elements with `heap_pop(h)` to verify max-heap (or custom-comparator min-heap) order.
3. **Explore `std/regex` capture slices:** Write a snippet using `regex::each_captures` over `"a=1 b=2 c=3"` that stores each key in a `dictionary<const u8[:], i64>` using `line[c.slots[2]..c.slots[3]]` (remembering why `regex::group(c, 1)` is rooted at `c` while slicing `line` via `c.slots` outlives the block!).

---

## Key Takeaways

- Goose's **20 standard library modules** are written in readable Goose following uniform slice (`T[:]`), builder (`u8[>..]&`), optional (`T?`), and trailing-`bool` conventions.
- **`std`** and **`dictionary`** cover bit math, PRNG, SIMD `ByteSet` search, sorting/heaps, and open-addressed hash tables.
- **`vec`**, **`mat`**, **`quat`**, and **`math`** provide `checkfree` 2D/3D linear algebra (use `mul(a, b)` for matrix/quaternion multiplication; `*` is elementwise).
- **`binary`**, **`base64`**, **`csv`**, **`json`**, and **`regex`** handle external formats with zero heap allocations, while **`os`**, **`gfx`**, **`audio`**, **`physics`**, and **`ui`** provide portable OS and interactive multimedia capabilities.

That completes **Part IV: Concurrency, Interop, and Ecosystem**! In **Part V: Performance Engineering**, we begin with **Chapter 18: Index Types, Bounds-Check Elimination, and `checkfree`**, learning how to prove zero bounds checks remain in hot loops and graph traversals!
