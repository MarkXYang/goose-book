# 第 22 章：综合项目 3 —— 构建实时交互式应用

实时游戏、仿真程序与创意工具会同时考验一门语言的每一个层面：非嵌套的实体生命周期（对象以任意顺序生成与销毁）、确定性的固定时间步物理模拟、零停顿的帧渲染、即时模式 UI 叠加层、程序化音频合成、防崩溃的存档文件，以及窗口关闭时严格的析构清理顺序。

Goose 的内置包——`gfx`（SDL3 GPU）、`ui`（Nuklear 即时模式控件与 HUD）、`audio`（PCM 混音器）以及 `physics`（Box3D）——与根结构体（`root struct`）、`slot_pool`（槽位池）、`defer`、`embed_shader` 和 `from_bytes` 结合在一起，让你能够用简洁、扁平的 Goose 代码写出完整的交互式应用，而且完全没有任何垃圾回收停顿。

在这个第三个综合项目中，我们将架构 **Beacon Arena**——一个实时 3D 实体沙盒，并将其清晰地划分为两个层次：

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

### 本项目综合运用的核心概念

| 特性 | 所属章节 | 在本项目中的作用 |
|---|---|---|
| `slot_pool<T>` 与根结构体（`root struct`） | 第 8 章 | 管理非嵌套的实体生命周期（`Beacon`、`Pulse`），提供 $O(1)$ 槽位复用以及带代数校验的句柄。 |
| `guard` 与后进先出（LIFO）`defer` | 第 10 章 | 保证 `audio::close()`、`ui::destroy(ctx)` 与 `gfx::close()` 在每一条退出路径上都严格按相反顺序展开清理。 |
| `to_bytes` / `from_bytes` + `binary` + `write_file_atomic` | 第 13、17 章 | 将完整世界状态保存在带有魔数、版本号与校验和的二进制头部之后，并通过防崩溃的原子文件替换完成存档与重载。 |
| `embed_shader`（GLSL → SPIR-V / MSL / HLSL） | 第 16 章 | 在 Goose 编译期编译内联 GLSL 着色器，使运行时无需任何外部着色器工具链。 |
| `vec`、`math`、`gfx`、`ui`、`audio` | 第 17 章 | 实现 3D 变换、开启深度测试的网格渲染、带 `wants_mouse`/`wants_keyboard` 仲裁的即时模式控件，以及程序合成的提示音。 |

---

## 1. 无头仿真核心与受验证原子存档

在 Goose 游戏开发中，最优秀的一条架构习惯——同时也是 `samples/30_mini_minecraft.goose`（`--test`）与 `samples/31_mini_doom.goose`（`--frames N`）所采用的模式——就是让仿真状态**完全独立于 GPU 句柄与窗口状态**：

1. **用 `slot_pool<Entity>` 管理实体：** 实体以任意顺序生成并消亡。每个 `Entity` 都携带 `alive: bool` 与 `gen: u32`。当一个脉冲（pulse）到期时，我们递增 `gen`、将 `alive` 设为 `false`，并调用 `free(entities, idx)`。由于 `slot_pool` 会从其 LIFO 空闲列表中复用已释放的索引，在长时间游戏过程中 `entities.items` 绝不会无限制地增长。
2. **句柄携带 `(index, gen)`：** 通过保存 `struct Handle { idx: i32, gen: u32 }`，即使对应槽位已经被复用，我们依然能够校验一个实体是否还是*同一个*逻辑对象。
3. **使用独立的根结构体 `root struct ArenaLog` 记录多数组遥测数据：** 回忆第 8 章的内容，根结构体（`root struct`）的所有者只能是局部变量或全局变量（一个 `root struct` 目前还不能将另一个 `root struct` 作为字段拥有）。将 `var entities = slot_pool<Entity> {};` 与 `var log = ArenaLog {};` 并列放置，可以让它们各自拥有清晰的所属根。
4. **固定时间步仿真（`DT = 1.0 / 60.0`）：** 每一步仿真都以精确常量 `DT` 推进位置、轨道运动、脉冲剩余寿命以及信标碰撞检测。这使仿真具备 100% 确定性——也就是说，你可以在第 180 刻（tick）存档、重载到一个全新世界中，将两者都继续推进到第 240 刻，然后断言它们的位置和得分逐比特完全一致！
5. **受验证的原子存档：** 我们的存档格式打包了以下内容：
   - 一个 32 字节的小端序头部（魔数 `"BCNA"`、`version`、`tick`、`score`、`spawned`、`recycled`、`payload_hash`）。
   - `to_bytes(entities.items)` 生成的二进制映像（原样保存每一个实体槽位——完全无需手工逐字段序列化！）。
   写入磁盘时，`write_file_atomic(path, save_blob)` 会在 `path` 旁写入临时文件、刷盘后再原子重命名覆盖 `path`，因此写入中途发生崩溃也绝不会损坏玩家存档。加载时，`from_bytes<Entity[>..]>` 会先校验每一个字节，再恢复所有实体槽位，并根据所有 `!alive` 的槽位重建 `free_slots`。

现在就运行下面这个完整的无头仿真核心，亲眼观察实体生成、槽位复用、固定时间步物理、存档/重载往返验证以及篡改检测的实际效果：

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

来看看这些数字：
- 在 180 刻的仿真过程中，一共生成了 11 个实体，但 `entities.items` 最多只增长到了 **5 个槽位**，因为 `slot_pool` 以 $O(1)$ 时间复用了 6 个已死亡槽位！
- `first_pulse` 到期后其槽位被新的脉冲复用，但 `is_live(entities, first_pulse)` 仍然返回 `false`，因为它的代数计数器 `gen` 已不再匹配！
- 整个世界状态被序列化为 **204 字节**，重载进一个全新的 `slot_pool<Entity>` 后，继续推进 60 刻物理模拟，结果与实时仿真**逐比特完全一致**（`bytes_of(entities.items) == bytes_of(restored.items)`）。

---

## 2. 交互式 GPU、UI 与音频表现层外壳

现在，我们用一套实时的 **SDL3 GPU（`gfx`）+ Nuklear HUD（`ui`）+ 程序化 PCM（`audio`）** 表现层外壳将该仿真核心包裹起来。

请注意下面交互式外壳中的四个关键 Goose 惯用法：

1. **利用 `defer` 实现严格的后进先出（LIFO）清理：**
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
   由于 `defer` 在每一条退出路径上都会严格按声明的相反顺序执行，因此 `audio::close()` 会最先运行；随后 `ui::destroy(ctx)` 会在 `gfx` 仍处于活跃状态时释放 Nuklear 的 GPU 字体纹理；最后才由 `gfx::close()` 销毁窗口与 GPU 设备。
2. **通过 `embed_shader` 内联跨平台着色器：**
   直接将 GLSL 源码写在传给 `embed_shader("vert", ...)` / `embed_shader("frag", ...)` 的 `"""` 多行字符串中，Goose 编译器就会在编译期将每个着色器编译为 SPIR-V（Vulkan）、MSL（Metal）和 HLSL（Direct3D 12）——运行时零着色器编译开销，也无需分发外部着色器文件！
3. **清晰的输入仲裁（`ui::wants_mouse` / `ui::wants_keyboard`）：**
   先通过 `ui::input_from_gfx(ctx)` 将 `gfx` 输入传递给 Nuklear，再把 3D 视口操作保护在 `!ui::wants_mouse(ctx)` 与 `!ui::wants_keyboard(ctx)` 条件之后，这样点击 HUD 按钮时就绝不会误发一颗子弹。
4. **在 `while gfx::frame()` 中使用固定时间步累加器：**
   计算 `frame_dt = min(clock() - prev_time, 0.1)`，将其累加到 `accum` 中，并执行 `while accum >= DT { step_world(...); accum -= DT; }`，从而确保在 60 Hz、144 Hz 和 240 Hz 显示器上的游戏运行速度完全一致。

由于打开 GPU 窗口需要连接真实显示器，下面这个完整的交互式外壳使用 `goose check` 进行了静态验证（零错误、零警告）。在任何带显示器的机器上，都可以直接通过 `goose beacon_arena.goose`（或 `goose build beacon_arena.goose`）运行它：

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

## 3. 来自 `30_mini_minecraft` 与 `31_mini_doom` 的架构经验

Goose 在 `samples/` 目录下直接随附了两款完整可玩的 3D 游戏（约 1,640 行的 `30_mini_minecraft.goose` 与约 1,550 行的 `31_mini_doom.goose`）。阅读它们的源码可以总结出四条能够轻松扩展到真实项目的实用模式：

1. **将小型存档头与大块世界数据分离：** 在 `30_mini_minecraft` 中，`struct Game` 仅保存玩家变换、物品栏、随机种子与时钟，而 80×40×80 的体素网格则存放在独立的只增数组中。这样一来，到处传递 `Game` 绝不会触碰庞杂的地形数据，存档时只需将 `Game` 与体素切片打包并用 `write_file_atomic` 原子写入即可。
2. **随包分发的资产使用 `resource_dir()`，用户存档使用工作目录：** `str(resource_dir(), "data/freedoom1-e1m1.wad")` 在 JIT 模式下会在源文件旁查找只读资源，在 AOT 编译产物中则会在可执行文件旁（或 macOS `.app` 包的 `Contents/Resources/` 内）查找。切勿将用户存档写入 `resource_dir()`，因为应用安装后该目录可能是只读的。
3. **在逐帧递归遍历中复用调用方持有的临时缓冲区：** `31_mini_doom` 每一帧都会递归遍历 BSP 树，以裁剪没有固定顶点数上限的地面多边形。它并没有为每个多边形单独分配内存，而是传入一个增缩数组临时缓冲区（`scratch: float2[>..<]&`），并在返回时通过 `scratch.resize(mark)` 恢复水位（即第 11 章的平衡调用模式）——由此实现了每帧零分配与 $O(1)$ 重置！
4. **从第一天就为应用内置 `--test` 与 `--frames N` 标志：** 无头模式的 `--test` 能在 CI 中以毫秒级速度验证游戏逻辑、物理与存档/重载不变量，而 `--frames 30` 配合 `gfx::screenshot("smoke.png")` 则能让你自动化验证完整的 GPU 渲染管线！

---

## 练习

1. **为 HUD 接入存档/读档按钮：** 将第 1 节中的 `slot_pool<Entity>`、`encode_save` 和 `decode_save` 与第 2 节的交互式外壳结合起来。在 Nuklear 面板中添加 `"Save (F5)"` 和 `"Load (F9)"` 按钮，分别调用 `write_file_atomic("beacon_arena.sav", blob)` 与 `read_file(...)`，并在存档成功时播放高音提示音、读档出错时播放低音提示音！
2. **添加确定性的 `--frames N` 截图模式：** 参照 `samples/27_gfx_cube.goose` 的做法：从 `args()` 中解析 `--frames N`，使用固定时间步 `DT` 运行 `N` 帧后退出 `while gfx::frame()` 循环，并调用 `gfx::screenshot("arena.png")`。
3. **以无头方式测试 `audio::open_offline()`：** 编写一个可独立运行的小程序（无需窗口！），调用 `audio::open_offline()`，创建 `synth_chime(440.0)` 并播放它，通过 `audio::render(buf)` 将 1,024 个立体声采样渲染进 `f32[>..]` 缓冲区，然后打印峰值振幅！
4. **引入实体类型枚举：** 将 `Entity` 中的 `kind: u8` 替换为固定模式枚举 `enum EntityKind { Beacon, Pulse, Debris }`，并在脉冲击中信标时新增 4 个短寿命的 `Debris` 粒子爆发效果——观察当它们的 `ttl` 归零后，`slot_pool` 如何自动回收全部碎片槽位！

---

## 本章要点

- **将仿真与表现层解耦：** 让世界状态（`slot_pool<T>`、根结构体 `root struct`）、固定时间步更新和受验证存档保持完全无头化，使其能够在没有 GPU 或窗口的环境下确定性地运行与测试。
- **将 `slot_pool<T>` 与代数计数器配对使用：** 获得 $O(1)$ 的实体生成与回收能力、过期句柄检测以及零堆碎片。
- **按资源获取顺序依次堆叠 `defer` 调用**（`gfx::open` → `ui::create` → `audio::open`）：确保资源在每一条退出路径上都严格按相反的 LIFO 顺序自动释放。
- **结合 `write_file_atomic` + `binary` 头部 + `from_bytes`：** 用极少的代码即可构建防崩溃、防篡改且内存安全的游戏存档机制。
- **用 `embed_shader` 内联编译着色器，并通过 `resource_dir()` 解析随包资产：** 让你的应用能够干净利落地跨平台部署。
