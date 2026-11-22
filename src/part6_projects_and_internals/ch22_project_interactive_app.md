# Chapter 22: Capstone 3 — Building a Real-Time Interactive Application

Real-time games, simulations, and creative tools test every layer of a language at once: non-nested entity lifetimes (objects spawn and expire in arbitrary order), deterministic fixed-timestep physics, zero-pause frame rendering, immediate-mode UI overlays, procedural audio, crash-safe save files, and strict teardown order when the window closes.

Goose's built-in packages—`gfx` (SDL3 GPU), `ui` (Nuklear immediate-mode widgets & HUDs), `audio` (PCM mixer), and `physics` (Box3D)—combine with `root struct`, `slot_pool`, `defer`, `embed_shader`, and `from_bytes` to let you write complete interactive applications in clean, flat Goose with zero garbage-collection pauses.

In this third capstone, we will architect **Beacon Arena**, a real-time 3D entity sandbox, by splitting it cleanly into two layers:

```text
┌──────────────────────────────────────────────────────────────────────┐
│                  Presentation Shell (Main Thread)                    │
│  gfx (SDL3 GPU + embed_shader)  │  ui (Nuklear HUD)  │  audio (SFX)  │
│  LIFO defer teardown: audio::close -> ui::destroy -> gfx::close      │
└───────────────────────────────▲──────────────────────────────────────┘
                                │ reads snapshots / feeds input events
┌───────────────────────────────┴──────────────────────────────────────┐
│             Deterministic Simulation Core (Headless!)                │
│  slot_pool<Entity> (O(1) spawn/recycle)  │  root struct ArenaLog     │
│  Fixed-timestep step(dt = 1/60)          │  to_bytes/from_bytes +    │
│  Runs & tests with ZERO window (--test)  │  write_file_atomic saves  │
└──────────────────────────────────────────────────────────────────────┘
```

### Concepts Synthesized in This Capstone

| Feature | Chapter | Role in This Project |
|---|---|---|
| `slot_pool<T>` & `root struct` | Ch. 8 | Manages non-nested entity lifetimes (`Beacon`, `Pulse`) with $O(1)$ slot reuse and generation-checked handles. |
| `guard` & LIFO `defer` | Ch. 10 | Guarantees `audio::close()`, `ui::destroy(ctx)`, and `gfx::close()` unwind in exact reverse order on every exit path. |
| `to_bytes` / `from_bytes` + `binary` + `write_file_atomic` | Ch. 13, 17 | Saves and reloads complete world state behind a magic/version/checksum header with crash-safe atomic file replacement. |
| `embed_shader` (GLSL → SPIR-V / MSL / HLSL) | Ch. 16 | Compiles inline GLSL shaders at Goose compile time so no external shader toolchain is needed at runtime. |
| `vec`, `math`, `gfx`, `ui`, `audio` | Ch. 17 | 3D transforms, depth-tested mesh rendering, immediate-mode controls with `wants_mouse`/`wants_keyboard`, and synthesized chimes. |

---

## 1. The Headless Simulation Core and Validated Atomic Saves

The single best architectural habit in Goose game development—used by both `samples/30_mini_minecraft.goose` (`--test`) and `samples/31_mini_doom.goose` (`--frames N`)—is keeping your simulation state **completely independent of GPU handles and window state**:

1. **Entities in `slot_pool<Entity>`:** Entities spawn and expire in arbitrary order. Each `Entity` carries `alive: bool` and `gen: u32`. When a pulse expires, we increment `gen`, set `alive = false`, and call `free(entities, idx)`. Because `slot_pool` recycles freed indices from its LIFO freelist, `entities.items` never grows unboundedly during long play sessions.
2. **Handles carry `(index, gen)`:** Storing `struct Handle { idx: i32, gen: u32 }` lets us validate whether an entity is still the *same* logical object even after its slot has been recycled.
3. **Separate `root struct ArenaLog` for multi-array telemetry:** Recall from Chapter 8 that `root struct` owners are local or global variables (a `root struct` cannot yet own another `root struct` as a field). Keeping `var entities = slot_pool<Entity> {};` alongside `var log = ArenaLog {};` gives each its own clean owning root.
4. **Fixed-timestep simulation (`DT = 1.0 / 60.0`):** Every simulation step advances positions, orbital motion, pulse lifetimes, and beacon collisions by an exact constant `DT`. That makes simulations 100% deterministic—meaning you can save at tick 180, reload into a fresh world, step both to tick 240, and assert bit-for-bit identical positions and scores!
5. **Validated atomic saves:** Our save format packs:
   - A 32-byte little-endian header (`magic "BCNA"`, `version`, `tick`, `score`, `spawned`, `recycled`, `payload_hash`).
   - The `to_bytes(entities.items)` image (every entity slot verbatim—zero manual field-by-field serialization!).
   On disk, `write_file_atomic(path, save_blob)` writes a temporary file beside `path`, flushes it, and atomically renames it over `path` so a crash mid-write never destroys a player's save. On load, `from_bytes<Entity[>..]>` verifies every byte before restoring the entity slots and rebuilding `free_slots` from all slots where `!alive`.

Run this complete headless simulation core right now to watch entity spawning, slot recycling, fixed-timestep physics, save/load round-tripping, and tamper rejection in action:

```goose
import std;
import vec;
import math;
import binary;
import slot_pool;
import os;

const DT: f32 = 1.0 / 60.0;
const KIND_BEACON: u8 = 1;
const KIND_PULSE:  u8 = 2;

struct Entity {
    pos:   float3,
    vel:   float3,
    ttl:   f32,
    gen:   u32,
    kind:  u8,
    alive: bool,
}

struct Handle {
    idx: i32,
    gen: u32,
}

// Small pointer-free simulation metadata (saved in the binary header).
struct SimHeader {
    tick:     u32 = 0,
    score:    i32 = 0,
    spawned:  u32 = 0,
    recycled: u32 = 0,
}

// Custom root struct tracking multi-array event telemetry.
root struct ArenaLog {
    hit_ticks:   u32[>..] = [],
    hit_scores:  i32[>..] = [],
}

fn spawn_entity(
    pool: slot_pool<Entity>&,
    hdr: SimHeader&,
    kind: u8,
    pos: float3,
    vel: float3,
    ttl: f32,
) -> Handle {
    let was_reuse = pool.free_slots.len > 0;
    var next_gen: u32 = 1;
    if was_reuse {
        let peek = pool.free_slots[pool.free_slots.len - 1];
        next_gen = pool.items[peek].gen + 1;
        hdr.recycled += 1;
    }
    let idx = alloc_index(pool, Entity {
        pos: pos,
        vel: vel,
        ttl: ttl,
        gen: next_gen,
        kind: kind,
        alive: true,
    });
    hdr.spawned += 1;
    return Handle { idx: idx as i32, gen: next_gen };
}

fn is_live(pool: const slot_pool<Entity>&, h: Handle) -> bool {
    if h.idx < 0 || h.idx >= pool.items.len { return false; }
    let e = pool.items[h.idx];
    return e.alive && e.gen == h.gen;
}

fn despawn(pool: slot_pool<Entity>&, idx: i64) {
    if !pool.items[idx].alive { return; }
    pool.items[idx].alive = false;
    free(pool, idx);
}

fn step_world(
    pool: slot_pool<Entity>&,
    hdr: SimHeader&,
    log: ArenaLog&,
) {
    hdr.tick += 1;

    // 1. Advance active entities and expire finished pulses.
    for i in pool.items.len {
        if !pool.items[i].alive { continue; }
        pool.items[i].pos += pool.items[i].vel * float3 { DT, DT, DT };
        if pool.items[i].kind == KIND_BEACON {
            // Gentle orbital steering around origin.
            let p = pool.items[i].pos;
            pool.items[i].vel += float3 { -p.x * 0.4 * DT, 0.0, -p.z * 0.4 * DT };
        } else {
            pool.items[i].ttl -= DT;
            if pool.items[i].ttl <= 0.0 {
                despawn(pool, i);
            }
        }
    }

    // 2. Check pulse-vs-beacon hits (radius 0.85).
    for i in pool.items.len {
        if !pool.items[i].alive || pool.items[i].kind != KIND_PULSE { continue; }
        let ppos = pool.items[i].pos;
        for j in pool.items.len {
            if !pool.items[j].alive || pool.items[j].kind != KIND_BEACON { continue; }
            let d = ppos - pool.items[j].pos;
            if dot(d, d) <= 0.85 * 0.85 {
                hdr.score += 100;
                log.hit_ticks.push(hdr.tick);
                log.hit_scores.push(hdr.score);
                despawn(pool, i);
                // Knock beacon slightly outward on hit.
                pool.items[j].vel += float3 { 0.3, 0.0, -0.2 };
                break;
            }
        }
    }
}

// --- Validated atomic save/load ---------------------------------------

const SAVE_MAGIC: u32 = 0x414E4342; // "BCNA"
const SAVE_VER:   u32 = 1;
const SAVE_HDR_BYTES = 32;

fn fnv1a(data: const u8[:]) -> u64 {
    var h: u64 = 14695981039346656037;
    for b in data {
        h ^= b;
        h *= 1099511628211;
    }
    return h;
}

fn encode_save(pool: const slot_pool<Entity>&, hdr: const SimHeader&) -> u8[>..] {
    var payload: u8[>..] = [];
    to_bytes(pool.items, payload);

    var out: u8[>..] = [];
    binary::write_u32le(out, SAVE_MAGIC);
    binary::write_u32le(out, SAVE_VER);
    binary::write_u32le(out, hdr.tick);
    binary::write_i32le(out, hdr.score);
    binary::write_u32le(out, hdr.spawned);
    binary::write_u32le(out, hdr.recycled);
    binary::write_u64le(out, fnv1a(payload));
    out.append(payload);
    return out;
}

fn decode_save(
    blob: const u8[:],
    pool: slot_pool<Entity>&,
    hdr: SimHeader&,
) -> bool {
    if blob.len < SAVE_HDR_BYTES { return false; }
    var r = binary::reader(blob);
    let magic, ok1    = binary::read_u32le(r);
    let ver, ok2      = binary::read_u32le(r);
    let tick, ok3     = binary::read_u32le(r);
    let score, ok4    = binary::read_i32le(r);
    let spawned, ok5  = binary::read_u32le(r);
    let recycled, ok6 = binary::read_u32le(r);
    let want, ok7     = binary::read_u64le(r);
    if !ok1 || !ok2 || !ok3 || !ok4 || !ok5 || !ok6 || !ok7 { return false; }
    if magic != SAVE_MAGIC || ver != SAVE_VER { return false; }

    let payload = blob[SAVE_HDR_BYTES..];
    if fnv1a(payload) != want { return false; }

    let loaded, ok = from_bytes<Entity[>..]>(payload);
    if !ok { return false; }

    // Replace pool items and reconstruct the LIFO freelist from dead slots.
    pool.items.clear();
    pool.items.append(loaded);
    pool.free_slots.clear();
    var i = pool.items.len;
    while i > 0 {
        i--;
        if !pool.items[i].alive {
            pool.free_slots.push(i);
        }
    }
    hdr = SimHeader { tick: tick, score: score, spawned: spawned, recycled: recycled };
    return true;
}

fn count_alive(pool: const slot_pool<Entity>&) -> i64 {
    var n = 0;
    for e in pool.items {
        if e.alive { n += 1; }
    }
    return n;
}

fn main() {
    var entities = slot_pool<Entity> {};
    var hdr = SimHeader {};
    var log = ArenaLog {};

    // Spawn 3 orbiting beacons.
    let b0 = spawn_entity(entities, hdr, KIND_BEACON,
        float3 { 2.0, 0.0, 0.0 }, float3 { 0.0, 0.0, 1.2 }, 0.0);
    let _ = spawn_entity(entities, hdr, KIND_BEACON,
        float3 { -2.0, 0.0, 0.5 }, float3 { 0.2, 0.0, -1.1 }, 0.0);
    let _ = spawn_entity(entities, hdr, KIND_BEACON,
        float3 { 0.0, 0.0, -2.2 }, float3 { 1.0, 0.0, 0.0 }, 0.0);

    // Fire a short-lived pulse and record its handle to verify generation safety.
    let first_pulse = spawn_entity(entities, hdr, KIND_PULSE,
        float3 { 0.0, 0.0, 0.0 }, float3 { 2.2, 0.0, 0.8 }, 0.35);

    // Simulate 180 fixed ticks (3.0 seconds), firing a new pulse every 25 ticks.
    for t in 180 {
        if t > 0 && t % 25 == 0 {
            let a = (t as f32) * 0.17;
            let _ = spawn_entity(entities, hdr, KIND_PULSE,
                float3 { 0.0, 0.0, 0.0 },
                float3 { cos(a) * 3.0, 0.0, sin(a) * 3.0 },
                0.75);
        }
        step_world(entities, hdr, log);
    }

    print("tick=", hdr.tick, " score=", hdr.score, " hits=", log.hit_ticks.len);
    print("slots=", entities.items.len, " alive=", count_alive(entities),
          " spawned=", hdr.spawned, " recycled=", hdr.recycled);
    print("b0 still live? ", is_live(entities, b0),
          " | stale first_pulse rejected? ", !is_live(entities, first_pulse));

    // Save validated binary image (in a real app: write_file_atomic("arena.sav", blob)).
    let save_blob = encode_save(entities, hdr);
    print("save image bytes: ", save_blob.len);

    // Reload into a brand-new world and step both 60 more ticks!
    var restored = slot_pool<Entity> {};
    var restored_hdr = SimHeader {};
    var restored_log = ArenaLog {};
    let loaded_ok = decode_save(save_blob, restored, restored_hdr);
    assert(loaded_ok);

    for _ in 60 {
        step_world(entities, hdr, log);
        step_world(restored, restored_hdr, restored_log);
    }

    let exact_match = bytes_of(entities.items) == bytes_of(restored.items)
                   && hdr.tick == restored_hdr.tick
                   && hdr.score == restored_hdr.score;
    print("after +60 ticks: tick=", hdr.tick, " bit-exact reload match? ", exact_match);

    // Tamper check: flip one byte in the save image.
    var bad = copy(save_blob);
    bad[SAVE_HDR_BYTES] ^= 0x01;
    let tamper_ok = decode_save(bad, restored, restored_hdr);
    print("tampered save rejected? ", !tamper_ok);
}
```

```text
tick=180 score=200 hits=2
slots=5 alive=4 spawned=11 recycled=6
b0 still live? true | stale first_pulse rejected? true
save image bytes: 204
after +60 ticks: tick=240 bit-exact reload match? true
tampered save rejected? true
```

Look at those numbers:
- Over 180 ticks, 11 entities were spawned, yet `entities.items` only ever grew to **5 slots** because `slot_pool` recycled 6 dead slots in $O(1)$!
- `first_pulse` expired and its slot was reused by a newer pulse, yet `is_live(entities, first_pulse)` returned `false` because its generation counter `gen` no longer matches!
- The entire world state serialized into **204 bytes**, reloaded into a fresh `slot_pool<Entity>`, and after 60 more physics ticks matched the live simulation **bit for bit** (`bytes_of(entities.items) == bytes_of(restored.items)`).

---

## 2. The Interactive GPU, UI, and Audio Shell

Now let's wrap that simulation core in a real-time **SDL3 GPU (`gfx`) + Nuklear HUD (`ui`) + Procedural PCM (`audio`)** presentation shell.

Notice four key Goose idioms in the interactive shell below:

1. **Strict LIFO cleanup with `defer`:**
   <!-- fragment -->
   ```goose
   guard gfx::open("Beacon Arena", 960, 600, gfx::WINDOW_RESIZABLE) else { return; }
   defer gfx::close();

   var ctx = ui::create(13.0);
   guard ctx.id != 0 else { return; }
   defer ui::destroy(ctx);

   let audio_ok = audio::open();
   defer if audio_ok { audio::close(); }
   ```
   Because `defer` runs in reverse declaration order on every exit path, `audio::close()` runs first, `ui::destroy(ctx)` releases Nuklear's GPU font textures while `gfx` is still active, and `gfx::close()` tears down the window and GPU device last.
2. **Inline cross-platform shaders via `embed_shader`:**
   Writing GLSL directly inside `"""` strings passed to `embed_shader("vert", ...)` / `embed_shader("frag", ...)` compiles every shader to SPIR-V (Vulkan), MSL (Metal), and HLSL (Direct3D 12) at Goose compile time—zero runtime shader compilation, zero external files to ship!
3. **Clean input arbitration (`ui::wants_mouse` / `ui::wants_keyboard`):**
   Pass `gfx` input to Nuklear first with `ui::input_from_gfx(ctx)`, then guard 3D viewport actions behind `!ui::wants_mouse(ctx)` and `!ui::wants_keyboard(ctx)` so clicking a HUD button never fires a stray projectile.
4. **Fixed-timestep accumulator in `while gfx::frame()`:**
   Compute `frame_dt = min(clock() - prev_time, 0.1)`, add it to `accum`, and run `while accum >= DT { step_world(...); accum -= DT; }` so gameplay speed is identical on 60 Hz, 144 Hz, and 240 Hz monitors.

Because opening a GPU window requires a live display, the complete interactive shell below is verified with `goose check` (zero errors, zero warnings). Run it on any machine with a display using `goose beacon_arena.goose` (or `goose build beacon_arena.goose`):

<!-- check-only -->
```goose
import std;
import vec;
import math;
import os;
import gfx;
import ui;
import audio;

const DT: f32 = 1.0 / 60.0;

struct Vertex {
    pos:    float3,
    normal: float3,
    color:  u8[4],
}

// Matches std140 uniform block layout verbatim (two 4x4 matrices = 128 bytes).
struct Uniforms {
    model:     gfx::mat4,
    view_proj: gfx::mat4,
}

struct Orbiter {
    angle:  f32,
    radius: f32,
    speed:  f32,
    height: f32,
}

fn build_unit_cube(verts: Vertex[>..]&, indices: u16[>..]&) {
    let axes: float3[3] = [
        float3 { 1.0, 0.0, 0.0 },
        float3 { 0.0, 1.0, 0.0 },
        float3 { 0.0, 0.0, 1.0 },
    ];
    let colors: u8[4][6] = [
        [235, 95, 70, 255],  [245, 185, 65, 255], [90, 210, 120, 255],
        [75, 165, 235, 255], [175, 110, 230, 255], [225, 230, 240, 255],
    ];
    for f in 6 {
        let sign: f32 = if f % 2 == 0 { 1.0 } else { -1.0 };
        let n = axes[f / 2] * float3 { sign, sign, sign };
        let u = axes[(f / 2 + 1) % 3] * float3 { sign, sign, sign };
        let v = cross(n, u);
        let base = verts.len as u16;
        let corners: float2[4] = [
            float2 { -1.0, -1.0 }, float2 { 1.0, -1.0 },
            float2 { 1.0, 1.0 },   float2 { -1.0, 1.0 },
        ];
        for c in corners {
            let p = n + u * float3 { c.x, c.x, c.x } + v * float3 { c.y, c.y, c.y };
            verts.push(Vertex {
                pos: p * float3 { 0.35, 0.35, 0.35 },
                normal: n,
                color: colors[f],
            });
        }
        indices.append([base, base + 1, base + 2, base, base + 2, base + 3]);
    }
}

// Synthesize a short 220 ms pleasant chime as mono 48 kHz PCM floats.
fn synth_chime(freq: f64) -> f32[>..] {
    var pcm: f32[>..] = [];
    let samples = 10560; // 0.22 s at 48 kHz
    for i in samples {
        let t = i / 48000.0;
        let env = (1.0 - i / 10560.0) * (1.0 - i / 10560.0);
        let wave = sin(TAU * freq * t) + 0.35 * sin(TAU * freq * 2.0 * t);
        pcm.push((wave * 0.22 * env) as f32);
    }
    return pcm;
}

fn main() {
    guard gfx::open("Goose: Beacon Arena", 960, 600, gfx::WINDOW_RESIZABLE) else {
        write_stderr(str("gfx: ", gfx::error(), "\n"));
        return;
    }
    defer gfx::close();

    let ctx = ui::create(13.0);
    guard ctx.id != 0 else {
        write_stderr(str("ui: ", ui::error(), "\n"));
        return;
    }
    defer ui::destroy(ctx);

    // Optional audio: play silently if no sound device is available.
    let audio_ok = audio::open();
    defer if audio_ok { audio::close(); }
    var chime = audio::Sound { id: 0 };
    if audio_ok {
        let pcm = synth_chime(660.0);
        chime = audio::create_sound(pcm);
    }

    // Upload cube geometry once using zero-copy bytes_of().
    var verts: Vertex[>..] = [];
    var indices: u16[>..] = [];
    build_unit_cube(verts, indices);
    let vbuf = gfx::vertex_buffer(bytes_of(verts));
    let ibuf = gfx::index_buffer(bytes_of(indices));

    let vs = embed_shader("vert", """
        layout(location = 0) in vec3 a_pos;
        layout(location = 1) in vec3 a_normal;
        layout(location = 2) in vec4 a_color;
        layout(set = 1, binding = 0) uniform Uniforms {
            mat4 model;
            mat4 view_proj;
        };
        layout(location = 0) out vec3 v_normal;
        layout(location = 1) out vec4 v_color;
        void main() {
            v_normal = (model * vec4(a_normal, 0.0)).xyz;
            v_color = a_color;
            gl_Position = view_proj * model * vec4(a_pos, 1.0);
        }
        """);

    let fs = embed_shader("frag", """
        layout(location = 0) in vec3 v_normal;
        layout(location = 1) in vec4 v_color;
        layout(location = 0) out vec4 out_color;
        void main() {
            float ndl = max(dot(normalize(v_normal), normalize(vec3(0.4, 0.8, 0.6))), 0.0);
            out_color = vec4(v_color.rgb * (0.28 + 0.72 * ndl), 1.0);
        }
        """);

    var desc = gfx::PipelineDesc {
        cull: gfx::CULL_BACK,
        depth_test: true,
        depth_write: true,
    };
    desc.formats[2] = gfx::VERTEX_UBYTE4_NORM;
    let pipe = gfx::pipeline(vs, fs, desc);

    var orbiters: Orbiter[>..] = [
        Orbiter { angle: 0.0, radius: 2.2, speed: 1.1, height: 0.2 },
        Orbiter { angle: 2.1, radius: 1.5, speed: -1.5, height: -0.3 },
        Orbiter { angle: 4.2, radius: 2.8, speed: 0.8, height: 0.5 },
    ];
    var paused = false;
    var time_scale: f32 = 1.0;
    var cam_yaw: f32 = 0.4;
    var accum: f32 = 0.0;
    var prev_time = clock();
    var ticks: i64 = 0;

    let panel_flags = ui::WINDOW_BORDER | ui::WINDOW_MOVABLE | ui::WINDOW_TITLE;

    while gfx::frame() {
        if gfx::key_pressed("Escape") { break; }
        ui::input_from_gfx(ctx);

        // 1. Fixed-timestep simulation update.
        let now = clock();
        let frame_dt = min((now - prev_time) as f32, 0.1);
        prev_time = now;
        if !paused {
            accum += frame_dt * time_scale;
            while accum >= DT {
                for i in orbiters.len {
                    orbiters[i].angle += orbiters[i].speed * DT;
                }
                ticks += 1;
                accum -= DT;
            }
        }

        // Only orbit the camera when the UI isn't capturing keyboard/mouse input.
        if !ui::wants_keyboard(ctx) {
            if gfx::key_down("Left")  { cam_yaw -= 0.03; }
            if gfx::key_down("Right") { cam_yaw += 0.03; }
        }

        // 2. Build immediate-mode HUD inspector.
        if ui::begin(ctx, "Beacon Arena", ui::rect(16, 16, 250, 195), panel_flags) {
            ui::layout_row_dynamic(ctx, 22.0, 1);
            ui::label(ctx, str("Beacons: ", orbiters.len, "  Ticks: ", ticks), ui::TEXT_LEFT);
            ui::checkbox_label(ctx, "Paused", paused);
            ui::property_float(ctx, "Speed", 0.25, time_scale, 3.0, 0.05, 0.01);

            ui::layout_row_dynamic(ctx, 26.0, 2);
            if ui::button_label(ctx, "Spawn") && orbiters.len < 24 {
                let n = orbiters.len as f32;
                orbiters.push(Orbiter {
                    angle: n * 0.9,
                    radius: 1.4 + (orbiters.len % 4) * 0.55,
                    speed: if orbiters.len % 2 == 0 { 1.2 } else { -1.0 },
                    height: ((orbiters.len % 5) - 2) * 0.25,
                });
                if audio_ok && chime.id != 0 {
                    let _ = audio::play(chime, 0.8);
                }
            }
            if ui::button_label(ctx, "Clear") && orbiters.len > 1 {
                orbiters.resize(1);
            }
        }
        ui::end(ctx);

        // 3. Render 3D scene pass, then composite Nuklear HUD on top.
        let size = gfx::screen_size();
        let aspect = size.x as f32 / max(size.y, 1);
        let eye = float3 { sin(cam_yaw) * 6.5, 2.8, cos(cam_yaw) * 6.5 };
        let vp = gfx::mul(
            gfx::perspective(0.8, aspect, 0.1, 40.0),
            gfx::look_at(eye, float3_0, float3 { 0.0, 1.0, 0.0 }),
        );

        gfx::begin_screen(float4 { 0.08, 0.10, 0.15, 1.0 });
        gfx::bind(pipe);
        gfx::bind_vertex_buffer(vbuf);
        gfx::bind_index_buffer(ibuf, 2);

        for orb in orbiters {
            let pos = float3 {
                cos(orb.angle) * orb.radius,
                orb.height,
                sin(orb.angle) * orb.radius,
            };
            let model = gfx::mul(
                gfx::translate(pos),
                gfx::rotate(float3 { 0.3, 1.0, 0.2 }, orb.angle * 1.5),
            );
            gfx::uniforms(gfx::VERTEX, 0, Uniforms { model: model, view_proj: vp });
            gfx::draw_indexed(indices.len);
        }
        gfx::end_pass();

        // Draw HUD over the rendered 3D frame (must be outside gfx render pass).
        ui::render(ctx);
    }
}
```

---

## 3. Architectural Lessons from `30_mini_minecraft` and `31_mini_doom`

Goose ships two full playable 3D games right inside `samples/` (`30_mini_minecraft.goose` at ~1,640 lines and `31_mini_doom.goose` at ~1,550 lines). Reading them highlights four practical patterns that scale effortlessly to real projects:

1. **Separate small save headers from bulk world data:** In `30_mini_minecraft`, `struct Game` holds only the player transform, inventory, seed, and clock, while the 80×40×80 voxel grid lives in its own grow-only array. Passing `Game` around never touches bulk terrain data, and saving packs `Game` followed by the voxel slice atomically with `write_file_atomic`.
2. **Use `resource_dir()` for shipped assets, working directory for user saves:** `str(resource_dir(), "data/freedoom1-e1m1.wad")` finds read-only assets beside the source file under JIT and beside the executable (or inside `Contents/Resources/` on macOS `.app` bundles) under AOT builds. Never write user save files into `resource_dir()`, which may be read-only after installation.
3. **Reuse caller-owned scratch in per-frame recursive traversals:** `31_mini_doom` walks a recursive BSP tree every frame to clip floor polygons without a fixed corner limit. Rather than allocating per polygon, it passes a grow-shrink scratch buffer (`scratch: float2[>..<]&`) and restores `scratch.resize(mark)` on return (Chapter 11's balanced-call pattern)—giving zero allocations per frame and $O(1)$ reset!
4. **Build `--test` and `--frames N` flags into your app on day one:** Headless `--test` verifies gameplay logic, physics, and save/load invariants in milliseconds in CI, while `--frames 30` + `gfx::screenshot("smoke.png")` lets you verify the full GPU rendering pipeline automatically!

---

## Exercises

1. **Wire save/load buttons into the HUD:** Combine Section 1's `slot_pool<Entity>`, `encode_save`, and `decode_save` with Section 2's interactive shell. Add `"Save (F5)"` and `"Load (F9)"` buttons to the Nuklear panel that call `write_file_atomic("beacon_arena.sav", blob)` and `read_file(...)`, playing a high chime on save and a low chime on load error!
2. **Add a deterministic `--frames N` screenshot mode:** Follow `samples/27_gfx_cube.goose`'s pattern: parse `--frames N` from `args()`, stop the `while gfx::frame()` loop after `N` frames with a fixed `DT`, and call `gfx::screenshot("arena.png")`.
3. **Test `audio::open_offline()` headlessly:** Write a small runnable program (no window needed!) that calls `audio::open_offline()`, creates `synth_chime(440.0)`, plays it, renders 1,024 stereo samples into a `f32[>..]` buffer via `audio::render(buf)`, and prints the peak amplitude!
4. **Add an entity type enum:** Replace `kind: u8` in `Entity` with a fixed-mode `enum EntityKind { Beacon, Pulse, Debris }` and add a `Debris` burst of 4 short-lived particles whenever a pulse hits a beacon—watching `slot_pool` recycle all debris slots automatically once their `ttl` reaches zero!

---

## Key Takeaways

- **Split simulation from presentation:** Keep world state (`slot_pool<T>`, `root struct`), fixed-timestep updates, and validated saves completely headless so they run and test deterministically without a GPU or window.
- **Pair `slot_pool<T>` with generation counters** for $O(1)$ entity spawning and recycling with stale-handle detection and zero heap fragmentation.
- **Stack `defer` calls in acquisition order** (`gfx::open` → `ui::create` → `audio::open`) so resources automatically release in exact reverse LIFO order on every exit path.
- **Use `write_file_atomic` + `binary` headers + `from_bytes`** for crash-safe, tamper-evident, memory-safe game saves with minimal code.
- **Compile shaders inline with `embed_shader`** and resolve shipped assets via `resource_dir()` so your application deploys cleanly across platforms.
