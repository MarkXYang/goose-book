# Appendix D: Language Status, Limitations, and Further Reading

Goose is a young, actively developed systems language whose compiler, specification, standard library, benchmarks, and 31 sample programs all live together in one transparent repository. This appendix documents the exact scope of the current implementation, its deliberate v1 omissions, active design directions, and where to read next.

---

## 1. Compiler & Platform Status

Every runnable and native-build example in this book was tested against:

- **Compiler:** `goose v2026.4` (whole-program C++ compiler, ~30k LOC, Apache-2.0 license)
- **Platforms:** Linux, macOS, and Windows (64-bit little-endian x86-64 and ARM64)
- **Backends:**
  - Bundled **TinyCC (libtcc)** in-process JIT (`goose file.goose`) for zero-toolchain instant runs.
  - Native **C99** code generation (`goose build` / `goose -o out.c`) supporting **clang**, **GCC**, and **MSVC** (with AVX2/AVX-512 `simd fn` multi-versioning under clang on x86-64).
- **Built-in Packages:** `gfx` (SDL3 GPU + compile-time GLSL → SPIR-V/MSL/HLSL `embed_shader`), `audio` (SDL3 PCM mixer + offline renderer), `ui` (Nuklear widgets, fonts, and HUDs), and `physics` (Box3D rigid-body simulation).
- **Editor Support:** Built-in `goose lsp` Language Server Protocol server and `goose format` formatter, with official VS Code extension (`vscode/goose-language.vsix`) and configurations for Neovim, Helix, and Emacs.

---

## 2. Deliberate v1 Omissions & Current Limitations

Knowing what Goose *does not* currently include saves you from hunting for features that are intentionally out of scope or still on the roadmap:

| Area | Current Status / Limitation | Practical Workaround Today |
|---|---|---|
| **Move semantics for resizables** | No implicit move for non-fixed locals; reassigning or returning two different locals requires `copy(x)`. | Construct directly in destination (`let x = f()`, `v.push(f())`) and return one named local per function. |
| **Multiple resizables in one ordinary struct** | An ordinary `struct` may have at most one resizable field (as its last field). | Use `root struct` to bundle multiple independently stored resizable arrays under one owning lifetime. |
| **Nested owning `root struct` fields** | `root struct` owners must currently be local or global declarations (cannot be owned inside another `root struct`). | Declare peer `root struct` / `slot_pool` / `slice_pool` variables side by side and pass them by reference. |
| **Relative refs over thread queues** | Values containing relative references are not flat and cannot cross `qput`/`qget` directly. | Serialize with `to_bytes(a)` into a flat `u8[]` message and reload with `from_bytes<A>` on the worker. |
| **Labeled `break` / `continue`** | Not in v1. | Extract the inner loop into a nested helper function (or use `block { ... }` with early `break` / `return`). |
| **Module/namespace privacy** | No `pub`/`private` access modifiers in v1; namespaces organize names cleanly without visibility enforcement. | Keep internal helpers in a sub-namespace (e.g., `detail::helper`) or nested inside public functions. |
| **Networking, HTTP, subprocesses** | Not part of v1 `stdlib/os.goose`. | Bind C libraries cleanly via `extern fn`, `extern "lib" { ... }`, or a native package `.build` sidecar (Chapter 15). |
| **Deprecated `reusable` / `reusable[]`** | Still supported for legacy code, planned for removal. | Use `slot_pool<T>` (slots) and `slice_pool<T>` (contiguous runs) from the standard library. |
| **Big-endian hosts** | Serialization (`to_bytes`/`from_bytes`) and C codegen target 64-bit little-endian hosts. | Use `binary` (`read_*be` / `write_*be`) when exchanging big-endian external wire formats. |
| **BCE limits** | Plain `u64` variables (non-index-type), arbitrary array contents, and disjunctions in `while` exits are not tracked. | Keep counters in `i64`, store validated indices as `u32 in arr`, and add entry `assert`s. |

---

## 3. Active Research & Roadmap (`docs/design/`)

Goose's design notes track the next wave of language and tooling capabilities while preserving its zero-GC, annotation-free foundation:

- **`par for` / structured child threads:** Fork–join data parallelism that safely borrows read-only views of parent data stacks without copying large inputs across queues.
- **Coroutines:** Stack-disciplined generators and state machines without heap-allocated futures.
- **Ownership without destructors:** Zero-copy transfers of non-fixed values without introducing RAII drop glue or borrow-checker annotations.
- **`goose dap`:** A Debug Adapter Protocol server for step-through source debugging in VS Code, Neovim, and other editors.
- **WebAssembly target:** Compiling Goose to compact, predictable Wasm modules.

---

## 4. Further Reading and Primary Sources

All authoritative documentation lives in the Goose repository (`github.com/aardappel/goose`):

### Core Language Documentation
- **Tutorial (`docs/tutorial.md`):** Concise example-driven walkthrough of Goose's distinctive ideas.
- **Language Reference (`docs/reference.md`):** Complete topic-by-topic manual with compiling examples for every construct.
- **Language Specification (`docs/goose_spec.md`):** Formal rules for types, lifetimes, evaluation order, C representations, and open items (`Appendix B`).
- **Standard Library (`docs/stdlib.md`):** Complete API documentation for all 14 stdlib modules (`std`, `slot_pool`, `slice_pool`, `dictionary`, `vec`, `mat`, `quat`, `math`, `os`, `ffi`, `binary`, `base64`, `csv`, `json`, `regex`, `exception`) plus `gfx`, `audio`, `ui`, and `physics`.

### Tooling, Internals, and Benchmarks
- **Command Line (`docs/command_line.md`) & Packages (`docs/packages.md`):** Full CLI flags, sidecar `.build` files, C interop headers, and publishing packages.
- **Editor Setup (`docs/editors.md`):** Configuring `goose lsp` in VS Code, Neovim, Helix, and Emacs.
- **Compiler Implementation (`docs/implementation.md`):** Pass-by-pass guide to the compiler internals, BCE domain, C codegen, runtime, and Section 9 (*Writing fast Goose*).
- **Benchmarks (`bench/summary.md`, `bench/results.md`, `bench/design.md`):** Full comparative results against idiomatic C++, expert C++, and safe Rust across all 16 benchmarks and three input sizes.

### Recommended Samples to Read Next (`samples/`)
If you want to study larger idiomatic Goose programs beyond the capstones in Part VI, read these four repository samples:
1. **`samples/18_json.goose`** — Full JSON parser with `\uXXXX` UTF-8 decoding and pretty-printing.
2. **`samples/26_file_tree.goose`** — Directory tree combining `slot_pool` and `slice_pool` with 4-byte `in pool` links.
3. **`samples/30_mini_minecraft.goose`** — Complete playable voxel game with procedural terrain, chunk meshing, lighting, crafting, day/night cycle, and validated atomic saves—with zero external asset files.
4. **`samples/31_mini_doom.goose`** — Playable Freedoom E1M1 engine demonstrating checked WAD parsing (`binary`), recursive BSP floor clipping with caller-owned scratch, combat, and `audio` sound effects.
