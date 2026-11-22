# 第 20 章：综合实战 1——紧凑型 JSON 解析器、查询引擎与二进制缓存

在传统的系统编程语言中，构建 JSON AST 往往意味着在两个令人不快的极端之间做出取舍：要么为每个节点单独进行堆分配（`std::unique_ptr` / `Box` 加上 `std::string` 和 `std::vector`），承担数百次零碎分配以及 $O(n)$ 销毁遍历的开销；要么手写一套庞大繁杂的自定义字节内存池，充斥着原始偏移量计算和不安全的类型转换。

而在 Goose 中，最地道的表示方式*本身就是*紧凑、零额外分配且可直接序列化的表示。在这第一个综合实战项目中，我们将用一个结构紧凑、浑然一体的程序，构建完整的 **JSON 解析器、点分路径查询引擎、格式化/紧凑输出器以及受验证的二进制缓存**。

### 本项目综合运用的核心概念

| 特性 | 对应章节 | 在本项目中的作用 |
|---|---|---|
| 变长模式 ADT（`Json..`） | 第 7 章 | 每个 JSON 节点仅占用其当前活跃变体所需的精确字节数——包括内联存储的 `u8[varint]` 键名与字符串。 |
| 4 字节自相对链接（`Node&<u32>?`） | 第 12 章 | 在单个扁平的只增池中串联对象成员和数组元素，零指针体积膨胀，零重定位开销。 |
| 嵌套递归下降驱动函数 | 第 11 章 | 非递归的 `parse_json` 持有游标与临时键名缓冲区；嵌套的 `recursive fn` 解析函数直接借用它们，跨递归调用时数据栈上零局部变量。 |
| 跨帧返回（`return … from`） | 第 10 章 | 任意嵌套深度的语法错误都会惰性计算 `(line, col)`，并直接从 `parse_json` 返回，无需任何 `Result` 层的繁琐传递。 |
| 特化期 `const for` | 第 9 章 | 基于 `variants<Json..>` 自动按变体统计 AST 节点数量——新增变体时统计分析器会自动同步更新。 |
| 受验证持久化（`to_bytes` / `from_bytes`） | 第 13 章 | 将解析后的 AST 原样保存在“魔数 + 版本号 + FNV-1a 校验和”头部之后，无需重新解析 JSON 即可重新加载为可实时查询的树。 |

---

## 1. 设计扁平的自相对 AST

文档中的每一个 JSON 值都存放在同一个只增数组中：`Node[>..]`。

```goose
struct Node {
    next: Node&<u32>?,   // 4-byte self-relative link to next sibling (or null)
    key:  u8[varint],    // member name (empty for array elements / root)
    val:  Json..,        // variable-mode payload: exact size of active variant
}

enum Json {
    Null,
    Bool { v: bool },
    Num  { v: f64 },
    Str  { s: u8[varint] },
    Arr  { first: Node&<u32>?, count: varint },
    Obj  { first: Node&<u32>?, count: varint },
}
```

仔细观察这一内存布局所达成的效果：

1. **零逐节点堆分配：** 数组中的一个 `Null` 元素总共仅占 6 字节（4 字节的 `next` + 1 字节的零长度 `key` + 1 字节的 `Null` 标签）。一个 `Num` 仅占 14 字节。字符串则紧跟在它的 varint 长度之后内联存储字节内容。
2. **后序构造与兄弟节点回填：** 由于 `Node` 是变长的，子节点会*先于*包含它们的 `Arr` 或 `Obj` 被解析并压入数组。每当一个兄弟节点解析完成，我们就回填 `last.next .= child`——这是完全合法的，因为 `next` 是一个固定宽度的相对引用字段！
3. **$O(1)$ 哨兵根节点技巧：** 由于变长模式数组（`Node[>..]`）不支持随机索引（无法写 `pool[pool.len - 1]`），那么在通过 `from_bytes<Node[>..]>` 从磁盘重新加载后，我们如何瞬间定位到根节点呢？
   在解析开始之前，我们先**在位置 0 压入一个哨兵 `Node`**（`Node { next: null, key: "", val: Json.Null }`）。等 `parse_value` 解析完毕并返回文档的 `root` 后，我们回填 `sentinel.next .= root`。日后重新加载二进制镜像时，只需执行一步 `for n in pool { return n.next; }`，即可在 **$O(1)$ 时间内**恢复活跃的根节点——无需扫描任何一个节点！

```text
Node[>..] contiguous memory image:
┌─────────────────────────┬──────────────┬──────────────┬─────────────────────────┐
│ [0] Sentinel            │ child 0      │ child 1      │ Root Obj                │
│ next ───────────────┐   │ next ──►     │ next: null   │ first ──► child 0       │
│ key:""  val:Null    │   │ key:"a" 1.0  │ key:"b" "hi" │ count:2                 │
└─────────────────────┼───┴──────────────┴──────────────┴▲────────────────────────┘
                      └──────────────────────────────────┘ (4-byte relative offset)
```

---

## 2. 嵌套递归下降与惰性 `(line, col)` 错误诊断

回想第 11 章中关于 Goose 递归的黄金法则：**处于递归环中的函数不能跨越递归调用持有数据栈上的局部变量**。将递归解析函数嵌套放在非递归驱动函数 `parse_json(pool, text)` *内部*，便可轻松满足这一规则：

- `parse_json` 在自己的栈帧上持有 `var pos = 0;` 和 `var keys: u8[>..] = [];`（用于存放反转义后对象键名的只增临时缓冲区），该栈帧的生命周期长于每一次递归调用。
- `parse_string()` *不在*递归环内（字符串内部绝不会嵌套 JSON 值），因此它可以安全地拥有局部的 `var s: u8[>..] = [];`，并通过目标处直接构造将其直接返回到 `Json.Str { s: parse_string() }` 中！
- `parse_child` 和 `parse_value` 构成递归环（`recursive fn`），它们只持有固定大小的局部变量（`Node?`、`i64`、`bool`、`u8`），同时共享外部的 `pos`、`keys` 和 `pool`。
- 一旦遇到任意语法错误，`fail(what)` *仅在错误路径上*扫描 `text[0..pos]` 以计算从 1 开始的 `line` 和 `col`，随后执行 `return null, fail(...) from parse_json;`——瞬间从任意嵌套深度中展开返回，对合法 JSON 不产生任何运行时开销。

---

## 3. 点分路径查询、`const for` 统计分析与格式化

一旦解析完成（或从二进制缓存重新加载），遍历 AST 只需使用普通的可选引用（`const Node?`）：

- **`member(n, name)`** 沿着 `Obj` 的 `first` → `next` 链表查找满足 `cur.key == name` 的节点。
- **`element(n, index)`** 沿着 `Arr` 的 `first` → `next` 链表步进到第 `index` 个位置。
- **`query_path(root, path)`** 切分点分查询路径（如 `"cluster.nodes.1.ports.0"`）——在 `Obj` 节点上匹配对象键名，在 `Arr` 节点上匹配十进制下标——最终返回 `const Node?`。
- **`print_variant_stats(pool)`** 在 `match` 中使用 `const for <N, V> in variants<Json..>`，自动汇总每个变体的名称与数量（跳过索引 0 处的哨兵节点）。
- **`render_json(n, out, indent)`** 遍历任意子树，将紧凑（`indent < 0`）或带缩进（`indent >= 0`）的 JSON 直接写入 `u8[>..]&` 构建器中。

---

## 4. 基于 `to_bytes` 与 `from_bytes` 的受验证二进制缓存

由于 `Node[>..]` 内部的每一条链接都是自相对引用（`Node&<u32>?`），且每一个字符串都内联存储（`u8[varint]`），因此 **`Node[>..]` 无需任何转换代码即可直接序列化**。

为了让这个二进制缓存达到生产级水准，我们借助标准库的 `binary` 模块，在 `to_bytes(pool)` 载荷外包裹一层 16 字节的容器头部：

```text
Byte offset:   0..4          4..8            8..16               16..end
             ┌─────────────┬───────────────┬───────────────────┬────────────────────┐
             │ Magic "GJSN"│ Version = 1   │ FNV-1a u64 hash   │ to_bytes(pool)     │
             └─────────────┴───────────────┴───────────────────┴────────────────────┘
```

回顾第 13 章介绍的双层防线：
1. **完整性校验层（`unwrap_cache`）：** 检查魔数字节、版本号以及 FNV-1a 校验和，从而在第一时间捕获意外截断、版本不匹配或比特翻转，并给出清晰的错误提示。
2. **内存安全验证层（`from_bytes<Node[>..]>`）：** 哪怕攻击者针对恶意构造的载荷伪造了合法的校验和，Goose 内置的验证器仍会在任何一个字节变成活跃的 `Node[>..]` 之前，逐项检查每一个 varint、枚举标签、布尔字节、数组边界以及自相对引用 `Node&<u32>?` 的偏移量（确保每一个非空链接都精确落在池内某个实际元素的起始位置上）。

注意第 13 章提到的一个微妙的 Goose 细节：由于 `Node` 包含自相对引用，`Node[>..]` 不能在 `return` 语句的不同分支间被复制。将 `unwrap_cache(blob)` 保持为一个纯粹剥离头部的函数、仅返回 `(const u8[:], u8[>..])`，可以让调用方直接执行 `let loaded, ok = from_bytes<Node[>..]>(payload);`，将结果直接构造进自己的目标存储位置！

---

## 5. 完整可运行实现

以下是综合实战 1 的完整程序——包含解析器、查询引擎、`const for` 变体统计分析器、格式化器以及受验证二进制缓存——可直接运行与实验：

```goose
import std;
import binary;

// --- 1. Compact, self-relative AST ------------------------------------

struct Node {
    next: Node&<u32>?,
    key:  u8[varint],
    val:  Json..,
}

enum Json {
    Null,
    Bool { v: bool },
    Num  { v: f64 },
    Str  { s: u8[varint] },
    Arr  { first: Node&<u32>?, count: varint },
    Obj  { first: Node&<u32>?, count: varint },
}

// Recover the document root from sentinel node 0 in O(1) time.
fn doc_root(pool: const Node[>..]& ) -> const Node? {
    for n in pool {
        return n.next;
    }
    return null;
}

// --- 2. Nested recursive-descent parser -------------------------------

fn parse_json(pool: Node[>..]&, text: const u8[:]) -> Node?, u8[>..] {
    var pos = 0;
    var keys: u8[>..] = [];

    fn skip_space() {
        while pos < text.len
              && (text[pos] == ' ' || text[pos] == '\n'
                  || text[pos] == '\r' || text[pos] == '\t') {
            pos++;
        }
    }

    // Compute 1-based line and column lazily on the error path only.
    fn fail(what: const u8[:]) -> u8[>..] {
        var line = 1;
        var col = 1;
        for i in pos {
            if text[i] == '\n' {
                line += 1;
                col = 1;
            } else {
                col += 1;
            }
        }
        return str(what, " at line ", line, ", col ", col);
    }

    fn expect(c: u8) {
        skip_space();
        if pos < text.len && text[pos] == c {
            pos++;
            return;
        }
        var msg: u8[>..] = [];
        format(msg, "expected '");
        msg.push(c);
        msg.push('\'');
        return null, fail(msg) from parse_json;
    }

    // Non-recursive: may own a local builder constructed directly into the AST.
    fn parse_string() -> u8[>..] {
        expect('"');
        var s: u8[>..] = [];
        loop {
            guard pos < text.len else {
                return null, fail("unterminated string") from parse_json;
            }
            let c = text[pos];
            pos++;
            if c == '"' { break; }
            if c != '\\' {
                s.push(c);
                continue;
            }
            guard pos < text.len else {
                return null, fail("unterminated escape") from parse_json;
            }
            let e = text[pos];
            pos++;
            if e == 'n' { s.push('\n'); }
            else if e == 't' { s.push('\t'); }
            else if e == 'r' { s.push('\r'); }
            else if e == '"' || e == '\\' || e == '/' { s.push(e); }
            else { return null, fail("unsupported escape") from parse_json; }
        }
        return s;
    }

    fn parse_number() -> f64 {
        let start = pos;
        while pos < text.len
              && (ascii::is_digit(text[pos]) || text[pos] == '-'
                  || text[pos] == '+' || text[pos] == '.'
                  || text[pos] == 'e' || text[pos] == 'E') {
            pos++;
        }
        let v, ok = parse_flt(text[start..pos]);
        guard ok else {
            return null, fail("invalid number") from parse_json;
        }
        return v;
    }

    fn parse_word(w: const u8[:]) {
        guard pos + w.len <= text.len && text[pos..pos + w.len] == w else {
            return null, fail("unexpected token") from parse_json;
        }
        pos += w.len;
    }

    recursive fn parse_child(is_obj: bool) -> Node& {
        if is_obj {
            let start = keys.len;
            keys.append(parse_string());
            expect(':');
            return parse_value(keys[start..]);
        }
        return parse_value("");
    }

    recursive fn parse_value(key: const u8[:]) -> Node& {
        skip_space();
        guard pos < text.len else {
            return null, fail("unexpected end of input") from parse_json;
        }
        let c = text[pos];
        if c == '{' || c == '[' {
            let is_obj = c == '{';
            let close = if is_obj { '}' } else { ']' };
            pos++;
            skip_space();
            guard pos < text.len else {
                return null, fail("unclosed container") from parse_json;
            }
            var first: Node? = null;
            var count = 0;
            if text[pos] != close {
                let head .= parse_child(is_obj);
                first .= head;
                var last .= head;
                count = 1;
                loop {
                    skip_space();
                    guard pos < text.len else {
                        return null, fail("unclosed container") from parse_json;
                    }
                    if text[pos] == close { break; }
                    expect(',');
                    let child .= parse_child(is_obj);
                    last.next .= child;
                    last .= child;
                    count++;
                }
            }
            pos++;
            if is_obj {
                return pool.push(Node {
                    next: null,
                    key: key,
                    val: Json.Obj { first: first, count: count },
                });
            }
            return pool.push(Node {
                next: null,
                key: key,
                val: Json.Arr { first: first, count: count },
            });
        }
        if c == '"' {
            return pool.push(Node { next: null, key: key, val: Json.Str { s: parse_string() } });
        }
        if c == 't' {
            parse_word("true");
            return pool.push(Node { next: null, key: key, val: Json.Bool { v: true } });
        }
        if c == 'f' {
            parse_word("false");
            return pool.push(Node { next: null, key: key, val: Json.Bool { v: false } });
        }
        if c == 'n' {
            parse_word("null");
            return pool.push(Node { next: null, key: key, val: Json.Null });
        }
        return pool.push(Node { next: null, key: key, val: Json.Num { v: parse_number() } });
    }

    // Push sentinel node at position 0 so doc_root() is O(1) after reload.
    let sentinel .= pool.push(Node { next: null, key: "", val: Json.Null });
    let root .= parse_value("");
    skip_space();
    guard pos == text.len else {
        return null, fail("trailing input") from parse_json;
    }
    sentinel.next .= root;
    return root, str("");
}

// --- 3. Query engine & compact/pretty formatter -----------------------

fn member(n: const Node&, name: const u8[:]) -> const Node? {
    match n.val {
        Obj &o => {
            var cur = o.first;
            while cur {
                if cur.key == name { return cur; }
                cur .= cur.next;
            }
        },
        _ => {},
    }
    return null;
}

fn element(n: const Node&, target: i64) -> const Node? {
    if target < 0 { return null; }
    match n.val {
        Arr &a => {
            var cur = a.first;
            var idx = 0;
            while cur {
                if idx == target { return cur; }
                cur .= cur.next;
                idx++;
            }
        },
        _ => {},
    }
    return null;
}

fn query_path(root: const Node&, path: const u8[:]) -> const Node? {
    var cur: const Node? = root;
    var start = 0;
    while start <= path.len {
        var end = start;
        while end < path.len && path[end] != '.' { end++; }
        let seg = path[start..end];
        guard cur else { return null; }
        match cur.val {
            Obj &o => {
                // Suppress unused binding warning by delegating to member().
                let _ = o.count;
                cur .= member(cur, seg);
            },
            Arr &a => {
                let _ = a.count;
                let idx, ok = parse_int(seg);
                if !ok { return null; }
                cur .= element(cur, idx);
            },
            _ => { return null; },
        }
        if end == path.len { break; }
        start = end + 1;
    }
    return cur;
}

fn write_quoted(out: u8[>..]&, s: const u8[:]) {
    out.push('"');
    for c in s {
        if c == '"' || c == '\\' {
            out.push('\\');
            out.push(c);
        } else if c == '\n' {
            out.append("\\n");
        } else if c == '\t' {
            out.append("\\t");
        } else {
            out.push(c);
        }
    }
    out.push('"');
}

recursive fn render_json(n: const Node&, out: u8[>..]&) {
    match n.val {
        Null => out.append("null"),
        Bool &b => out.append(if b.v { "true" } else { "false" }),
        Num &x => {
            let iv = x.v as! i64;
            if iv == x.v { format(out, iv); } else { format(out, x.v); }
        },
        Str &s => write_quoted(out, s.s),
        Arr &a => {
            out.push('[');
            var cur = a.first;
            var first = true;
            while cur {
                if !first { out.push(','); }
                first = false;
                render_json(cur, out);
                cur .= cur.next;
            }
            out.push(']');
        },
        Obj &o => {
            out.push('{');
            var cur = o.first;
            var first = true;
            while cur {
                if !first { out.push(','); }
                first = false;
                write_quoted(out, cur.key);
                out.push(':');
                render_json(cur, out);
                cur .= cur.next;
            }
            out.push('}');
        },
    }
}

fn node_to_str(n: const Node&) -> u8[>..] {
    var out: u8[>..] = [];
    render_json(n, out);
    return out;
}

// Compile-time variant profiler using `const for` over `variants<Json..>`.
fn print_variant_stats(pool: const Node[>..]&) {
    var counts: i64[6] = [0; 6];
    var skip_sentinel = true;
    for n in pool {
        if skip_sentinel {
            skip_sentinel = false;
            continue;
        }
        match n.val {
            const for <N, V> in variants<Json..> {
                V => {
                    var slot = 0;
                    const for <M, W> in variants<Json..> {
                        const if same<V, W> {
                            counts[slot] += 1;
                        }
                        slot += 1;
                    }
                },
            }
        }
    }
    var idx = 0;
    var summary: u8[>..] = [];
    const for <N, V> in variants<Json..> {
        if idx > 0 { summary.append(", "); }
        format(summary, N, "=", counts[idx]);
        idx += 1;
    }
    print("variants: ", summary);
}

// --- 4. Verified binary cache container -------------------------------

const CACHE_MAGIC: u32 = 0x4E534A47; // "GJSN" little-endian
const CACHE_VER:   u32 = 1;
const HEADER_LEN = 16;

fn fnv1a(data: const u8[:]) -> u64 {
    var h: u64 = 14695981039346656037;
    for b in data {
        h ^= b;
        h *= 1099511628211;
    }
    return h;
}

fn pack_cache(pool: const Node[>..]&) -> u8[>..] {
    var payload: u8[>..] = [];
    to_bytes(pool, payload);
    let checksum = fnv1a(payload);

    var out: u8[>..] = [];
    binary::write_u32le(out, CACHE_MAGIC);
    binary::write_u32le(out, CACHE_VER);
    binary::write_u64le(out, checksum);
    out.append(payload);
    return out;
}

fn unwrap_cache(blob: const u8[:]) -> const u8[:], u8[>..] {
    if blob.len < HEADER_LEN {
        return blob[..0], str("truncated cache header");
    }
    let magic, ok1 = binary::read_u32le(blob, 0);
    let ver, ok2 = binary::read_u32le(blob, 4);
    let want_hash, ok3 = binary::read_u64le(blob, 8);
    if !ok1 || !ok2 || !ok3 || magic != CACHE_MAGIC {
        return blob[..0], str("bad cache magic");
    }
    if ver != CACHE_VER {
        return blob[..0], str("unsupported cache version ", ver);
    }
    let payload = blob[HEADER_LEN..];
    if fnv1a(payload) != want_hash {
        return blob[..0], str("checksum mismatch");
    }
    return payload, str("");
}

// --- 5. Demo driver ---------------------------------------------------

fn main() {
    let sample = """
{
  "cluster": {
    "name": "north-eu",
    "replicas": 3,
    "tls": true,
    "nodes": [
      { "host": "10.0.1.10", "ports": [80, 443], "note": "primary\tready" },
      { "host": "10.0.1.11", "ports": [8080, 8443], "note": null }
    ]
  }
}
""";

    var pool: Node[>..] = [];
    let root, err = parse_json(pool, sample);
    guard root else {
        print("parse error: ", err);
        return;
    }

    print("input bytes: ", sample.len, ", AST bytes: ", bytes_of(pool).len);
    print_variant_stats(pool);

    // Run dot-path queries on the freshly parsed AST.
    let q1 = query_path(root, "cluster.name");
    let q2 = query_path(root, "cluster.nodes.1.host");
    let q3 = query_path(root, "cluster.nodes.1.ports.0");
    if q1 && q2 && q3 {
        print("query cluster.name           = ", node_to_str(q1));
        print("query cluster.nodes.1.host   = ", node_to_str(q2));
        print("query cluster.nodes.1.ports.0 = ", node_to_str(q3));
    }

    // Demonstrate deep syntax error with line/col via `return ... from`.
    var bad_pool: Node[>..] = [];
    let bad_src = "{\n  \"ok\": [1, 2, ]\n}";
    let bad_root, bad_err = parse_json(bad_pool, bad_src);
    if !bad_root {
        print("error report: ", bad_err);
    }

    // Pack AST to verified binary cache, reload with ZERO JSON parsing.
    let cache_blob = pack_cache(pool);
    print("cache image bytes: ", cache_blob.len);

    let payload, uerr = unwrap_cache(cache_blob);
    guard uerr.len == 0 else {
        print("cache error: ", uerr);
        return;
    }
    let loaded, ok = from_bytes<Node[>..]>(payload);
    assert(ok);

    let loaded_root = doc_root(loaded);
    if loaded_root {
        let same = node_to_str(loaded_root) == node_to_str(root);
        print("reloaded round-trip exact: ", same);
        let rq = query_path(loaded_root, "cluster.nodes.0.note");
        if rq {
            print("reloaded query note: ", node_to_str(rq));
        }
    }

    // Tamper test 1: flip a payload byte -> caught by FNV-1a checksum.
    var tampered = copy(cache_blob);
    tampered[HEADER_LEN + 2] ^= 0xFF;
    let _, terr = unwrap_cache(tampered);
    print("tamper checksum test: ", terr);

    // Tamper test 2: bypass checksum with corrupted raw payload -> caught by from_bytes!
    var raw_bad: u8[>..] = [];
    raw_bad.append(payload);
    raw_bad[raw_bad.len - 1] = 250; // invalid tag / truncated structure
    let _, bad_ok = from_bytes<Node[>..]>(raw_bad);
    print("structural verify rejected hostile payload: ", !bad_ok);
}
```

```text
input bytes: 247, AST bytes: 286
variants: Null=1, Bool=1, Num=5, Str=4, Arr=3, Obj=4
query cluster.name           = "north-eu"
query cluster.nodes.1.host   = "10.0.1.11"
query cluster.nodes.1.ports.0 = 8080
error report: invalid number at line 2, col 16
cache image bytes: 304
reloaded round-trip exact: true
reloaded query note: "primary\tready"
tamper checksum test: checksum mismatch
structural verify rejected hostile payload: true
```

停下来欣赏一下第一行输出：**整棵已解析、带类型、已链接且可直接查询的 AST——包含 18 个节点外加 1 个哨兵节点，以及全部兄弟链接、键名、字符串、IEEE-754 双精度浮点数和容器——在内存中仅占用 286 字节（仅为 247 字节原始 JSON 文本的 1.16 倍）**，而在典型语言中，堆分配的 DOM 树开销通常高达 5 倍至 10 倍。并且，从 304 字节的二进制缓存重新加载它时，不仅会验证全部结构不变量，还能在不解析任何一个 JSON 字符的情况下恢复 $O(1)$ 的根节点查找。

---

## 练习

1. **添加带缩进的美化输出：** 为 `render_json` 增加一个 `indent: i64` 参数（当 `indent < 0` 时输出紧凑 JSON，当 `indent >= 0` 时输出以 2 个空格缩进、并在非空数组与对象前后换行的格式化 JSON）。
2. **添加 `\uXXXX` Unicode 转义解码：** 实现 `push_utf8(out: u8[>..]&, cp: i64)`，并在 `parse_string()` 中借助 `ascii::hex_value(c)` 处理 `\u` 转义序列。使用 `"caf\u00e9"` 对其进行测试。
3. **通过 `os` 添加命令行磁盘缓存：** 编写一个包装版 `main()`，导入 `os`，通过 `args()` 接收一个 `.json` 文件路径，使用 `read_file` 检查 `<path>.gjsn` 是否已存在；若缓存有效则直接加载二进制缓存，否则解析 JSON 并用 `write_file` 写出 `<path>.gjsn`。
4. **添加通配符数组查询：** 扩展 `query_path`（或新增 `collect_field(node, key, out)`），使你能够遍历 `"cluster.nodes"` 中的所有元素，将每个 `"host"` 字符串收集到一份 `u8[>..]` 报告中。

---

## 本章要点

- **单个只增数组 + `Json..` + `Node&<u32>?`** 将充斥指针的 AST 变为比源文本更小的单个扁平内存块，实现零堆分配与 $O(1)$ 销毁。
- **哨兵节点 0** 使顺序存储的变长模式数组在经过 `from_bytes<Node[>..]>` 重新加载后，依然具备 $O(1)$ 的根节点恢复能力。
- **嵌套递归下降函数**置于非递归驱动函数内部，可轻松遵守递归栈规则，同时借助跨帧返回（`return … from`）提供详尽的 `(line, col)` 错误诊断，而无需在每个解析函数中层层传递错误类型。
- **双层二进制缓存**将应用层封包与校验和（`binary` + FNV-1a）同 Goose 内置的 `from_bytes` 结构验证器相结合，让持久化后的 AST 既防篡改又内存安全。
