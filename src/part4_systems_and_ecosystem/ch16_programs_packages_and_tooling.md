# Chapter 16: Program Structure, Packages, and Tooling

Small examples fit happily in one `.goose` file. Real applications span multiple modules, pull in reusable libraries, bundle fonts or shaders, run under CI format checks, and integrate with editors and external build systems.

Goose keeps project structure deliberately minimal: **whole-program compilation from a single root file, flat pinned Git/path packages with no central registry, and one `goose` binary** that handles running, building, checking, fetching, formatting, and LSP editor support.

By the end of this chapter, you will be able to:

- Organize multi-file Goose programs using `import`, namespaces, file-local namespace aliases, and global initialization rules.
- Embed compile-time assets with `embed_file` and locate runtime assets portably with `os::resource_dir()`.
- Consume and author Goose packages—pure Goose, C-backed, or CMake-backed—using flat root-file dependencies.
- Use the full `goose` CLI (`--stacks`, `--bce-lines`, `--stats`, `--diagnostics json`, `--standalone`, `goose format`, `goose lsp`) in daily work and CI.

---

## 16.1 Files, `import`, and Whole-Program Compilation

Every `goose` command takes one **root file**—the entry file containing `fn main()` (or `export fn`s for a library) and declaring the program's packages. The compiler follows `import` statements transitively and compiles the entire program as one unit.

- `import a.b.c;` loads `a/b/c.goose` **once** (duplicate imports are harmless). Goose searches in order:
  1. Relative to the importing file's package root (for your own program, the root file's directory).
  2. A module of a package declared in the root file (e.g., `import mylib.extra;`).
  3. The standard library directory (`stdlib/`, overridable via `--stdlib` or `GOOSE_STDLIB`).
- `import .helper;` (leading dot) resolves relative to the *importing file's* own directory.
- **Imports do not create namespaces**—they simply include the file's declarations into the compilation.
- **Runnable modules double as libraries:** Any `fn main()` inside an *imported* file is silently ignored! Give a reusable file `fn run_demo()` plus a tiny `fn main() { run_demo(); }`, and you can run it standalone *or* import it cleanly from another program.

---

## 16.2 Namespaces, Aliases, and Lookup Rules

Prefix a file (before its declarations) with `namespace geo;` to place every top-level declaration in that file inside namespace `geo`. Multiple files may contribute to the same namespace, and individual declarations may also qualify themselves explicitly (`fn geo::norm1(...)` or `fn ::main()` inside a namespaced file).

Name lookup follows three clear rules:

1. **Resolution order:** Lexical scopes (locals, type parameters, nested functions) → current namespace → global namespace and builtins.
2. **UFCS and `format` follow the type:** Calling `p.norm1()` on a `geo::Pt` searches namespace `geo` automatically from *any* namespace, and `print(p)` / `str(p)` automatically finds `geo::format`!
3. **Overload sets never merge across namespaces:** If namespace `geo` declares its own `hash`, calling `hash(x)` inside `geo` searches *only* `geo`'s overloads—write `::hash(x)` to reach the global `std` overloads.

When a package has a long namespace, add a **file-local alias** at the top of your file: `namespace g = goose_geometry;`. Aliases never leak to files that import yours.

```goose
import std;
namespace geo;

struct Pt { x: i64, y: i64 }

enum Kind { Origin, Offset { dx: i64, dy: i64 } }

fn norm1(p: Pt) -> i64 { abs(p.x) + abs(p.y) }  // `abs` found in global `std`

fn format(out: u8[>..]&, p: Pt) {
    format(out, "(", p.x, ",", p.y, ")");
}

// `::main` places the entry point in the global namespace despite `namespace geo;`:
fn ::main() {
    let p = geo::Pt { x: -3, y: 4 };
    let k: geo::Kind = geo::Kind.Offset { dx: 1, dy: 2 };
    // Both `print(p)` and `p.norm1()` find `geo::format` and `geo::norm1` automatically!
    print("pt=", p, " norm1=", p.norm1(), " kind=", k);
}
```

```
pt=(-3,4) norm1=7 kind=Offset { 1, 2 }
```

Note that `::` (namespace qualification) and `.` (variant/field/UFCS access) are distinct tokens, so `geo::Kind.Offset { ... }` parses unambiguously. Goose namespaces have no nesting and no privacy modifiers—they exist strictly for clean naming and C symbol hygiene.

---

## 16.3 Globals, Initialization Order, and Embedded Assets

### Globals and Whole-Program Arenas

Globals may have any type—scalars, structs, growable arrays, or `root struct` pools. They are initialized **before `main` runs**, imported files first and then in declaration order within each file, and their initializers may call functions.

- A `var` global given only a type starts at its default value (`var log: u8[>..];` is an empty grow-only array).
- A `const` global of flat fixed type with a compile-time initializer lives in **static read-only memory** shared by all threads.
- A global growable array owns the bottom of a data stack for the lifetime of the program—making it a zero-ceremony whole-program arena (and eligible for `in pool` relative links and index types!).
- Naming a global before its initializer has executed is caught at compile time.

```goose
let TAGS = ["alpha", "beta", "gamma"];           // static read-only data
let LAST_IDX = TAGS.len - 1;                     // named constant derived from global .len
var counts: i64[TAGS.len] = [0; TAGS.len];
var audit: u8[>..];                              // type-only `var` global starts empty

fn main() {
    counts[LAST_IDX] += 3;
    format(audit, "last=", TAGS[LAST_IDX], " count=", counts[LAST_IDX]);
    print(audit, " all=", counts);
}
```

```
last=gamma count=3 all=[0, 0, 3]
```

### Compile-Time vs. Runtime Assets

Goose makes a clean distinction between assets baked into the executable and assets shipped alongside it:

- **Baked in at compile time (`embed_file`, `embed_shader`):**

  <!-- fragment -->
  ```goose
  let FONT_BYTES = embed_file("assets/font.ttf");    // const u8[:] in static data
  ```
  `embed_file("path")` resolves relative to the calling source file at **compile time** and embeds the bytes directly into static data (deduplicated per path). `embed_shader(...)` similarly compiles GLSL shaders at compile time for `gfx`. Ideal for self-contained single-binary deployments and packages!
- **Shipped beside the executable (`os::resource_dir()`):**
  For large external asset files (such as game WADs or textures), use `str(resource_dir(), "data/map.wad")` from `import os;`: under `goose run`, `resource_dir()` points to the root source file's directory; under `goose build`, it resolves to the compiled executable's own directory (or `Contents/Resources/` inside a macOS `.app` bundle).

---

## 16.4 Flat Pinned Packages

Goose's package model is designed to prevent dependency hell by enforcing one strict rule:

> **Only the root file declares package sources.**

In your root file (`app.goose`), declare each package once with `import name from "source";`:

```text
import mylib from "joe/mylib@v2";                             // GitHub shorthand at tag v2
import mylib from "github.com/joe/mylib@3f2c1a9";             // pinned commit (reproducible)
import mylib from "https://git.example.com/x/mylib.git@main"; // full Git URL at branch
import mylib from "../mylib";                                 // local directory (edited live!)
```

- Submodules of `mylib` are then imported anywhere in your project as `import mylib.extra;`.
- **Blessed packages** (`gfx`, `audio`, `ui`, `physics`) are known by name—just write `import gfx;` without a `from "..."` source (and if your compiler was built with `-DGOOSE_BUILTIN_LIBS=all`, they are already compiled inside `goose`!).
- **Transitive requirements stay flat:** If `mylib` depends on `vec_extras`, `mylib.goose` writes `import vec_extras from "github.com/x/vec_extras@v2";` as a *requirement*. If your root file omits `vec_extras`, the compiler tells you the exact line to add; if two libraries disagree on the source repository, it is an error. Every program contains **at most one version** of each package, pinned in one visible place.

### Package Layout & Authoring

A Goose package is simply a Git repository (or directory) with no registry registration required:

```text
mylib/
  mylib.goose        # Entry file (`namespace mylib;`)
  mylib.build        # Optional sidecar for C sources, c_headers, or CMake target
  extra.goose        # Additional module (`import mylib.extra;`)
  src/, include/     # Optional C code
  examples/, test/   # Runnable root programs demonstrating/testing the package
```

A package backed by an existing CMake C library needs only two lines in `mylib.build`:

```text
// mylib.build
cmake_target = "mylib";
cmake_options = ["MYLIB_SIMD=ON"];
exclude = ["tools", "third_party/demo"];
// Optional: mark non-thread-safe global C libraries so workers cannot call them:
// main_thread_only = true;
```

### Fetching, Offline Builds, and the Cache

```bash
goose fetch app.goose               # download all declared packages without compiling
goose fetch --update app.goose      # re-fetch packages pinned to a tag or branch
goose build --offline app.goose     # guarantee zero network access
```

Everything Goose fetches or compiles lives under `~/.goose` (overridable via `GOOSE_HOME`): `pkg/` (Git checkouts), `build/pkg/` (per-toolchain native builds), `build/runtime/` (cached runtime object), `build/prog/` (per-program generated C & objects), and `config` (e.g., `cc = "clang";`). Deleting `~/.goose` is always safe—everything inside it is regenerated on demand.

---

## 16.5 Mastering the `goose` CLI and Tooling

One executable covers your entire development and CI lifecycle:

| Command / Flag | Purpose |
|---|---|
| `goose app.goose -- arg1 arg2` | Fast TinyCC in-process run; arguments after `--` go to `os::args()` |
| `goose build -o bin/app app.goose` | Native optimizing build (`-O2` / `/O2`); add `--debug` for `-O0 -g` + overflow/range checks |
| `goose build --dry-run app.goose` | Print exact C compiler & linker commands without running them |
| `goose check --stacks app.goose` | Report exact data-stack count per program/worker and `hardware_threads()` cap |
| `goose check --bce-lines app.goose` | Report per-line eliminated vs. remaining bounds checks (Chapter 18) |
| `goose check --stats app.goose` | Print declaration, specialization, inlining, and BCE summary statistics |
| `goose check --diagnostics json app.goose` | Emit machine-readable JSON diagnostics for CI or custom tools |
| `goose run --stack-reserve 16G app.goose` | Increase virtual reservation per data stack (default `2G`) |
| `goose run -DGS_STACK_STATS=1 app.goose` | Print actual runtime stack high-water usage on stderr at exit |
| `goose format src/` / `goose format --check .` | Format all `.goose` files in place, or fail with exit code `1` in CI if unformatted |
| `goose toolchain` / `goose version` | Inspect detected C compiler, built-in packages, `GOOSE_HOME`, and runtime hash |
| `goose completion bash\|zsh\|fish\|powershell` | Generate shell tab-completion scripts |
| `goose lsp` | Start the built-in Language Server Protocol server for VS Code, Neovim, Helix, or Emacs |

### Integrating Generated C Into External Build Systems

If you want Make, CMake, Ninja, or Bazel to drive the C compiler yourself, emit the C directly:

```bash
# Single self-contained C file (easiest for external builds):
goose --standalone -o app.c app.goose
cc -O2 app.c -o app -lm -pthread

# Or share one compiled runtime object across many programs:
goose --emit-runtime goose_runtime.c
cc -O2 -c goose_runtime.c
goose -o app.c app.goose
cc -O2 app.c goose_runtime.o -o app -lm -pthread
```

For programs using built-in native packages like `gfx`, `goose --gfx-link cc` (or `msvc`) prints the response file path to pass to your linker (`@$(goose --gfx-link cc)`).

---

## Exercises

1. **Inspect compiler statistics & runtime stack stats:** Save any worked example from Part II or III as `demo.goose` and run:
   ```bash
   goose check --stats --stacks demo.goose
   goose run -DGS_STACK_STATS=1 demo.goose
   ```
   Compare the static stack count from `--stacks` with the live usage reported on stderr by `GS_STACK_STATS=1`.
2. **Test namespace UFCS & explicit `::`:** Extend Section 16.2 by adding a second `struct Box { lo: Pt, hi: Pt }` and `fn width(b: Box) -> i64` inside `namespace geo;`, then verify inside `fn ::main()` that `b.width()` resolves without `geo::` while `geo::Box { ... }` uses the namespace prefix.
3. **Create a two-file local package:** In a temporary folder, create `greet/greet.goose` (`namespace greet; fn hello(name: const u8[:]) -> u8[>..] { str("hi, ", name) }`) and `main.goose` (`import greet from "./greet"; fn main() { print(greet::hello("goose")); }`), and run `goose main.goose`.

---

## Key Takeaways

- Programs compile whole from one **root file**; `import` loads files once, and `fn main()` in imported files is ignored automatically.
- **Namespaces** (`namespace foo;`, `foo::name`, `::global`, file-local aliases) organize names cleanly while letting UFCS and `format` follow each value's type automatically.
- Use **`embed_file`** / **`embed_shader`** for compile-time static assets and **`os::resource_dir()`** for shipped runtime files.
- **Packages are flat and pinned in the root file** (`import lib from "owner/repo@ref"` or `"../local"`), cached under `~/.goose`, with optional `.build` sidecars for C or CMake targets.
- One `goose` CLI covers JIT runs, native builds, C emission, formatting, shell completions, diagnostic flags (`--stacks`, `--bce-lines`, `--stats`, `--diagnostics json`), and `goose lsp`.

In **Chapter 17: Tour of the Standard Library**, we survey all 20 modules that ship under `stdlib/`—all written in readable Goose—so you know what tools are already at your disposal before diving into performance engineering in Part V!
