# Chapter 15: Interoperating with C

No systems language lives on an island. Whether you need a battle-tested compression library, an OS API, a GPU driver, or want to embed high-speed Goose kernels inside an existing C, C++, or Python application, crossing the C boundary has to be fast and predictable.

Because Goose compiles to C, calling C requires no heavyweight FFI bridge—every call compiles to a **direct C call**. Goose supports C interoperability in **both directions**:

1. **`extern fn`** — bind static C symbols (system libraries, libm, or C sources listed in a `.build` sidecar).
2. **`extern "lib" { ... }`** — dynamically load shared libraries by name at startup with **zero C toolchain** required.
3. **`export fn`** — compile a Goose program into a standalone C module or shared library (`.so` / `.dylib` / `.dll`) with an auto-generated `.h` header.

By the end of this chapter, you will be able to:

- Declare `extern fn` bindings and know which Goose types cross the C ABI directly.
- Bundle your own C sources and headers cleanly using `.build` sidecar files.
- Bind shared libraries dynamically with `extern "lib" { ... }`, `pad`, and the `ffi` module.
- Export Goose functions to C hosts with `export fn`, `--header`, `--shared`, `goose_init`, and `goose_exit`.

---

## 15.1 Calling C with `extern fn`

An `extern fn` is a top-level, fully typed, non-generic function declaration with no body. By default it binds to the C symbol with the same name; add `= "c_symbol"` to bind a different C name:

```goose
import std;

// Bind directly to C99 <math.h> symbols (available in both `goose run` and `goose build`):
extern fn cube_root(x: f64) -> f64 = "cbrt";
extern fn hypot(x: f64, y: f64) -> f64;
extern fn scale_pow2(x: f64, exp: i32) -> f64 = "ldexp";

fn main() {
    var root_text: u8[>..] = [];
    format_flt(root_text, cube_root(27.0), 3);
    print("cbrt(27)=", root_text, " hypot(3,4)=", hypot(3.0, 4.0),
          " ldexp(1.5, 4)=", scale_pow2(1.5, 4));
}
```

```
cbrt(27)=3.000 hypot(3,4)=5.0 ldexp(1.5, 4)=24.0
```

### What Types Can Cross the Boundary?

Goose refuses to mis-marshal complex types silently—only types with a plain, unambiguous C shape are accepted at an `extern fn` declaration:

| Goose Type | C Representation |
|---|---|
| `i8..i64`, `u8..u64`, `f32`, `f64`, `bool` | Exact C fixed-width integers, `float`, `double`, `bool` |
| Flat fixed-size `struct`, `T[k]`, `T[..k]`, fixed-mode `enum` | Packed C struct by value |
| `T&` / `const T&` (for flat fixed-size `T`) | `T *` / `const T *` pointer |
| `T[:]` / `const T[:]` (for flat fixed-size `T`) | `struct { T *data; int64_t len; }` |
| `T[>..]&` (especially `u8[>..]&`) | `gs_rref` builder handle (`gs_bld_append(out, ptr, len)` in C) |
| Return type | One scalar, one flat fixed-size value, or void |

Types with Goose-specific layouts—`varint`, optionals `T?`, variable-size values (`u8[]`) by value, or structs containing references—are rejected right at the declaration:

<!-- compile-fail -->
```goose
extern fn bad_c_fn(s: u8[]) -> i32;  // error: variable-size type cannot cross extern fn by value

fn main() {
    print(bad_c_fn("hi"));
}
```

> **Safety Contract:** Mark read-only slice/reference parameters `const` so Goose lets you pass string literals and `const` data into C. C code must **never** retain a pointer to a borrowed Goose slice or reference after the `extern fn` returns.

---

## 15.2 Bundling Your Own C with `.build` Sidecars

When your Goose program includes C files of its own, place a **sidecar file** named `foo.build` right next to your root file `foo.goose`:

```text
my_app/
  app.goose
  app.build
  impl.c
  include/
    adapters.h
```

A `.build` file uses clean Goose-like syntax (`//` comments, `key = value;`, string lists) and supports `_linux`, `_macos`, and `_windows` suffixes on every `c_*` key:

```text
// app.build
c_sources = ["impl.c"];
c_include = ["include"];
c_headers = ["include/adapters.h"];     // spliced into generated C for inline adapters
c_defines = ["FAST_CRC=1"];
c_flags_cc = ["-O3"];                   // gcc / clang flags (c_flags_msvc for MSVC)
c_libs = ["m"];
c_libs_windows = ["user32"];
```

Both `goose build app.goose` and `goose run app.goose` read `app.build` automatically: `goose build` compiles and links `impl.c` into your executable, while `goose run` builds `impl.c` once into a cached shared library under `~/.goose` and loads it into the in-process TinyCC run.

### Writing C Against Goose Slices and Builders

In a header included via `c_headers` (or `--include shim.h`), C can inspect Goose slices directly (`sl_u8` / `sl_i32` with `.data` and `.len`), write into Goose structs through pointers (Goose adds a `_g` suffix to user type and field names), and append bytes into a Goose `u8[>..]&` builder using `gs_bld_append`:

```c
/* include/adapters.h */
#include <string.h>

static uint32_t crc32_bytes(sl_u8 s) {
    uint32_t crc = 0xFFFFFFFFu;
    for (int64_t i = 0; i < s.len; i++) {
        crc ^= s.data[i];
        for (int k = 0; k < 8; k++)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

static void c_version(gs_rref out) {
    const char *v = "C adapter 1.0";
    gs_bld_append(out, v, strlen(v));
}
```

On the Goose side, you just declare the matching signatures and call them like any Goose function:

<!-- fragment -->
```goose
extern fn crc32_bytes(s: const u8[:]) -> u32;
extern fn c_version(out: u8[>..]&);
```

---

## 15.3 Zero-Toolchain Dynamic FFI: `extern "lib" { ... }`

What if you want to call a shared library already installed on the system—or shipped beside your executable—**without compiling any C wrapper at all**?

Write an **`extern "lib" { ... }`** block. At program startup, Goose opens the shared library dynamically and resolves every symbol your program actually calls (uncalled declarations are ignored, and if no symbol in the block is called, the library is never even opened!):

<!-- check-only -->
```goose
import ffi;

struct SDLRect { x: i32, y: i32, w: i32, h: i32 }

extern "SDL2" {
    fn SDL_Init(flags: u32) -> i32;
    fn SDL_Quit();
    fn SDL_GetError() -> u64;                         // returns `const char *` as opaque u64
    fn SDL_CreateWindow(title: const u8&, x: i32, y: i32, w: i32, h: i32, flags: u32) -> u64;
    fn delay(ms: u32) = "SDL_Delay";                  // rename symbol with `= "..."`
}

fn main() {
    if SDL_Init(0x20) < 0 {
        abort(str("SDL error: ", ffi::cstr(SDL_GetError())));
    }
    let title = "Goose Window\0";                     // explicit NUL terminator for C
    let win = SDL_CreateWindow(title[0], 100, 100, 640, 480, 0);
    if win != 0 { delay(100); }
    SDL_Quit();
}
```

### Essential Rules for Raw C Libraries

When binding a C shared library directly without a C adapter, keep five rules in mind:

1. **Library names & `.build` overrides:** `"SDL2"` resolves to `libSDL2.so` on Linux, `libSDL2.dylib` on macOS, and `SDL2.dll` on Windows. A path containing `/` (like `"bin/steam_api64"`) loads relative to `resource_dir()`. If a platform uses a versioned soname, map it in your `.build` sidecar rather than hard-coding it in Goose:
   ```text
   dylibs_linux = ["SDL2=libSDL2-2.0.so.0"];
   ```
2. **Exact C types:** `int` is `i32`, `unsigned` is `u32`, `size_t` is `u64`, and C `enum`s (including `SDL_bool`) are **`i32`**, not `bool`. Avoid C `long` (32 bits on Windows, 64 bits on Linux/macOS).
3. **Opaque handles & C-owned memory:** Represent opaque C pointers (`SDL_Window *`, `FILE *`, `const char *`) as **`u64`**. To read a C-owned string or byte buffer back into Goose memory safely, import `ffi` and call **`ffi::cstr(ptr)`** or **`ffi::bytes(ptr, n)`**.
4. **NUL-terminated strings & `(ptr, len)` pairs:** For `const char *`, append `\0` and pass `title[0]` to a `const u8&` parameter. For C functions expecting `(ptr, count)`, pass `xs[0]` and `xs.len` as two arguments.
5. **Struct alignment with `pad`:** Because Goose structs are packed, insert `pad n` wherever C's ABI inserts alignment padding—and pass any struct containing `pad` **by reference** (`const MyStruct&`), never by value (so padding bytes don't shift register-passing conventions).

> **When You Still Need a Tiny C Adapter:** C callbacks (passing a function pointer into C) and C variadic functions (`printf`-style `...`) cannot be bound directly in an `extern "lib"` block; wrap those in a few lines of C in `c_sources` / `c_headers`.

---

## 15.4 Calling Goose from C: `export fn`, `--header`, and `--shared`

`export fn` is the mirror image of `extern fn`: it generates a C-callable wrapper around a top-level, non-generic Goose function so a C, C++, Rust, or Python (`ctypes`) host can call into Goose!

- Write `export fn name(...)` to export under the Goose name, or `export "c_name" fn name(...)` to choose a custom C symbol.
- Parameters and return values follow the same C-compatible types as `extern fn`, **plus** one convenient extra: an exported function may return **`const u8[:]`**, which C receives as `const uint8_t *` along with an extra `int64_t *out_len` output parameter (remember: Goose byte slices are **not** NUL-terminated!).
- Your Goose `fn main()` (if present) runs once inside `goose_init(argc, argv)`—making `main` the natural initialization hook for your library!

```goose
var call_count: i32 = 0;

export fn greeting() -> const u8[:] {
    call_count += 1;
    return "Hello from Goose!";
}

export "goose_add" fn add(a: i32, b: i32) -> i32 {
    call_count += 1;
    return a + b;
}

export fn total_calls() -> i32 { call_count }

fn main() {
    // Runs once when C calls `goose_init`, or directly when run as a program:
    print(greeting(), " sum=", add(20, 22), " calls=", total_calls());
}
```

```
Hello from Goose! sum=42 calls=2
```

### Building and Calling from a C Host

You can embed Goose into C in **two** ways:

**Option A — Shared library (`.so` / `.dylib` / `.dll`):**
```bash
goose build --shared --header mylib.h mylib.goose
```

**Option B — Standalone C file compiled into your C project:**
```bash
goose --standalone --header mylib.h -o mylib.c mylib.goose
cc -O2 mylib.c host.c -o app -lm -pthread
```

And here is the matching C host (`host.c`):

```c
#include "mylib.h"
#include <stdio.h>

int main(int argc, char **argv) {
    goose_init(argc, argv);          /* initializes runtime, globals, and Goose main() */

    int64_t len = 0;
    const uint8_t *msg = greeting(&len);
    fwrite(msg, 1, (size_t)len, stdout);   /* slices are not NUL-terminated! */
    putchar('\n');

    printf("20 + 22 = %d (calls=%d)\n", goose_add(20, 22), total_calls());

    goose_exit();                    /* releases all Goose stacks cleanly */
    return 0;
}
```

> **Host Threading & Reload Rules:** Call exported functions on the same thread that called `goose_init`. Calling `goose_exit()` releases every data stack and runtime structure so a host can even hot-reload a rebuilt Goose shared library and call `goose_init` again in the same process (ensure all Goose workers have finished before calling `goose_exit`).

---

## 15.5 Worked Example: A Dual-Direction Native Geometry Kernel

Here is a clean, self-contained Goose library that **calls C** (`extern fn` into `libm`) internally while **exporting** a tidy, C-callable geometry API (`export fn`) with flat fixed-size structs and a slice-processing function—runnable directly with `goose` for quick tests *and* ready to ship as a shared library with `goose build --shared --header geo.h`!

```goose
import std;

extern fn hypot(x: f64, y: f64) -> f64;
extern fn atan2(y: f64, x: f64) -> f64;

struct Vec2 { x: f64, y: f64 }
struct Bounds { min_x: f64, min_y: f64, max_x: f64, max_y: f64 }

var last_label: u8[..48] = "uninitialized";

export "geo_dist" fn distance(a: Vec2, b: Vec2) -> f64 {
    hypot(b.x - a.x, b.y - a.y)
}

export "geo_angle_deg" fn angle_degrees(a: Vec2, b: Vec2) -> f64 {
    atan2(b.y - a.y, b.x - a.x) * 180.0 / 3.141592653589793
}

export "geo_bounds" fn compute_bounds(pts: const Vec2[:], out: Bounds&) -> bool {
    guard pts.len > 0 else { return false; }
    var lo_x = pts[0].x;
    var lo_y = pts[0].y;
    var hi_x = lo_x;
    var hi_y = lo_y;
    for p in pts[1..] {
        if p.x < lo_x { lo_x = p.x; }
        if p.y < lo_y { lo_y = p.y; }
        if p.x > hi_x { hi_x = p.x; }
        if p.y > hi_y { hi_y = p.y; }
    }
    out = Bounds { min_x: lo_x, min_y: lo_y, max_x: hi_x, max_y: hi_y };
    last_label.clear();
    format(last_label, "bounds(", pts.len, " pts)");
    return true;
}

export "geo_status" fn status_text() -> const u8[:] { last_label }

fn main() {
    let pts: Vec2[3] = [
        Vec2 { 0.0, 0.0 },
        Vec2 { 3.0, 4.0 },
        Vec2 { -2.0, 1.0 },
    ];
    var b = default<Bounds>();
    let ok = compute_bounds(pts, b);

    var deg: u8[>..] = [];
    format_flt(deg, angle_degrees(pts[0], Vec2 { 1.0, 1.0 }), 1);

    print("dist=", distance(pts[0], pts[1]), " deg=", deg);
    print("ok=", ok, " bounds=", b, " status=", status_text());
}
```

```
dist=5.0 deg=45.0
ok=true bounds=Bounds { -2.0, 0.0, 3.0, 4.0 } status=bounds(3 pts)
```

Notice how `status_text()` returns `last_label` (a global `u8[..48]`) as `const u8[:]`: because `last_label` is a global, its address stays valid after `status_text()` returns to the C host until the next call updates it!

---

## Exercises

1. **Generate a real C header and shared library:** Save Section 15.5 as `geo.goose` and run:
   ```bash
   goose build --shared --header geo.h geo.goose
   ```
   Open the generated `geo.h` and inspect the exact C prototypes emitted for `geo_dist`, `geo_bounds`, and `geo_status` (notice the extra `int64_t *` length parameter on `geo_status`!).
2. **Test `extern fn` const enforcement:** Declare `extern fn mutate_buf(s: u8[:]);` and try calling `mutate_buf("literal");`. Verify that Goose catches the const violation at compile time unless the extern parameter is declared `const u8[:]`.
3. **Call a C math function directly:** Add `extern fn floor(x: f64) -> f64;` and `extern fn fmod(x: f64, y: f64) -> f64;` to Section 15.1 and compare `fmod(-7.0, 3.0)` with Goose's integer `-7 % 3`.

---

## Key Takeaways

- **`extern fn`** binds directly to C symbols for scalars, flat packed fixed structs, `T&` pointers, `T[:]` (`{ data, len }`) slices, and `u8[>..]&` builders (`gs_bld_append`).
- Place program-owned C files, include paths, inline `c_headers`, and platform library flags in a **`foo.build`** sidecar next to `foo.goose`.
- **`extern "lib" { ... }`** loads shared libraries dynamically at startup with zero C toolchain; use `u64` for opaque pointers, `ffi::cstr`/`ffi::bytes` to copy C memory into Goose, and `pad` for C struct alignment.
- **`export fn`** + `--header` / `--shared` exposes Goose libraries to C hosts via `goose_init(argc, argv)` and `goose_exit()`.

Next, in **Chapter 16: Program Structure, Packages, and Tooling**, we step back to see how multi-file Goose programs, namespaces, compile-time embedded assets, Git packages, and the full `goose` CLI fit together.
