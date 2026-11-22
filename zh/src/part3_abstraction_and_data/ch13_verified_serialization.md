# 第 13 章：零拷贝持久化与受验证序列化

在大多数编程语言中，把一个复杂的内存数据结构保存到磁盘，往往意味着手写序列化器，或者引入外部 Schema 编译器（Protobuf、Cap'n Proto、FlatBuffers、Serde），以便将指针图转换为线缆格式（wire format），并在加载时重新解析还原。

在 Goose 中，**由扁平数据与自相对链接构成的结构本身就是自己的文件格式。** 由于每个字符串都是内联存储的，每条链接都是自相对偏移量（第 12 章），内存里的字节无论被复制到哪里都表达完全相同的含义——无需 Schema、无需代码生成器、无需指针重定位，重新加载时也无需逐节点分配内存。

唯一的注意事项是：来自磁盘文件或网络套接字的字节，无法像编译器构造的值那样被直接信任。Goose 在语言层面解决了这一问题：**`from_bytes` 会在不受信任的字节成为 Goose 值之前，运行由编译器生成的验证器。**

学完本章后，你将能够：

- 使用 `bytes_of`、`to_bytes` 和 `from_bytes<T[>..]>` 持久化并重新加载扁平数组与自相对数组。
- 解释 `from_bytes` 执行的每一项安全检查——以及**内存安全**与**数据完整性**之间的关键区别。
- 使用 `varint` 字符串与 `varint` 自相对链接，序列化并验证紧凑的**变长模式**树结构。
- 结合 `binary` 模块和 `std` 中的 `hash`，将 `to_bytes` 镜像封装到带版本号与校验和的容器格式中。

---

## 13.1 三个序列化内置函数

Goose 提供了三个内置函数，用于将符合条件的数组转换为字节并还原回来：

| 内置函数 | 签名 | 含义 |
|---|---|---|
| `bytes_of(a)` | `-> const u8[:]` | `a` 的元素区域的零拷贝只读字节视图（不含帧头前缀） |
| `to_bytes(a)` / `to_bytes(a, out)` | `-> u8[>..]` / 追加到 `out` | 带帧头的镜像：一个 ULEB128 字节计数，紧跟元素区域 |
| `from_bytes<A>(b)` | `-> A, bool` | 验证带帧头的镜像 `b`，成功时返回普通的存活数组（`A` 为 `T[>..]`、`T[]`，或不含链接的 `T[>..<]`）与 `true`，否则返回空数组与 `false` |

```text
Framed image produced by to_bytes(arr) and expected by from_bytes<T[>..]>(bytes):

  +------------------------+------------------------------------------------+
  | ULEB128(payload_bytes) |  exact contiguous element region (little-endian)|
  +------------------------+------------------------------------------------+
       1..10 bytes                      exact payload_bytes bytes
```

`bytes_of(a)` 始终返回只读视图（`const u8[:]`），因此代码永远无法绕过检查器篡改底层的链接偏移量。另外请留意，`to_bytes(a, out)` 会把带帧头的镜像直接追加到已有的 `u8[>..]` 缓冲区中——让你只需一次拷贝就能在前面拼接自定义头部标签！

```goose
import std;

struct Node {
    word: u8[..12],
    count: i32,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

fn insert(pool: Node[>..]&, word: const u8[:]) {
    if pool.len == 0 {
        pool.push(Node { word: word, count: 1 });
        return;
    }
    var cur .= pool[0];
    loop {
        let c = compare(word, cur.word[..]);
        if c == 0 { cur.count += 1; return; }
        let next = if c < 0 { cur.left } else { cur.right };
        if next { cur .= next; continue; }
        if c < 0 { cur.left  .= pool.push(Node { word: word, count: 1 }); }
        else     { cur.right .= pool.push(Node { word: word, count: 1 }); }
        return;
    }
}

fn lookup(pool: Node[>..]&, word: const u8[:]) -> i32 {
    guard pool.len > 0 else { return 0; }
    var cur .= pool[0];
    loop {
        let c = compare(word, cur.word[..]);
        if c == 0 { return cur.count; }
        let next = if c < 0 { cur.left } else { cur.right };
        guard next else { return 0; }
        cur .= next;
    }
}

fn main() {
    var index: Node[>..] = [];
    each_word("the quick fox and the lazy dog and the fox") {
        insert(index, it);
    };

    // Build a framed image and reload it into a brand-new independent tree:
    let image = index.to_bytes();
    var loaded, ok = from_bytes<Node[>..]>(image);

    // `loaded` is an ordinary grow-only array: we can query AND keep growing it!
    insert(loaded, "goose");
    print("ok=", ok, " original=", index.len, " loaded=", loaded.len);
    print("raw payload=", bytes_of(index).len, " framed=", image.len, " bytes");
    print("counts: the=", lookup(loaded, "the"), " fox=", lookup(loaded, "fox"),
          " goose=", lookup(loaded, "goose"));
}
```

```
ok=true original=6 loaded=7
raw payload=150 framed=152 bytes
counts: the=3 fox=2 goose=1
```

看看这有多自然：一旦 `from_bytes` 返回 `true`，`loaded` 就与你在内存中直接构造出的树别无二致——以 `loaded` 为根、可索引、可增长，并且遵守所有常规生命周期规则。

---

## 13.2 `from_bytes` 验证什么（安全性 vs. 完整性）

外部字节是 Goose 中*唯一*可能出现恶意或损坏偏移量、试图伪造引用的地方。因此，`from_bytes` 在交回值之前会验证以下四项属性：

1. **精确帧封装：** ULEB128 字节计数必须采用规范编码，并且覆盖切片中**剩余的每一个字节**（从而捕获截断、尾部垃圾字节与空切片）。对于固定大小的 `T`，有效载荷大小必须是 `sizeof(T)` 的精确整数倍；对于变长的 `T`，顺序遍历必须恰好落在末尾。
2. **标签、长度与索引：** 每个枚举标签都必须在合法范围内，每个嵌套内联数组的长度都必须容纳于剩余字节之中，每个索引类型（`uN in a`）都必须在其所属数组的合法范围内。
3. **自相对链接：** 偏移量 `0` 仅允许出现在可选（`?`）链接上；每个非零偏移量 `field_offset + offset` 都必须**精准落在同一镜像内的某个元素起始位置**（对于变体类型的链接，还必须匹配该变体的标签）。
4. **编译期资格检查：** 包含原始地址（`T&`、`T[:]`）或外部池链接（`T&<u32 in pool>`）的类型会在编译期被拒绝——将含自相对链接的数据加载到增缩数组 `T[>..<]` 中同样会被拒绝（因为后续的 `.pop()` 会导致内部链接悬垂）。

尝试序列化包含普通引用的结构体会在编译期被捕获：

<!-- compile-fail -->
```goose
struct Bad { v: i32, next: Bad? }

fn main() {
    var xs: Bad[>..] = [Bad { v: 1 }];
    let img = xs.to_bytes();         // error: plain references are addresses, not serializable
    print(img.len);
}
```

### 安全性不等于完整性

理解 `from_bytes` 承诺了什么——以及它**没有**承诺什么——至关重要：

- **篡改链接偏移量、长度、标签或文件大小**会威胁内存安全 → **`from_bytes` 会拒绝它（返回 `false` 和空数组）。**
- **翻转普通整数标量（如 `count`）中的某一位**仍然会产生一棵 100% 内存安全、结构合法但数值不同的树 → **`from_bytes` 会接受它（返回 `true`）。**

```goose
struct Node {
    val: i32,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

fn main() {
    var tree: Node[>..] = [];
    let root .= tree.push(Node { val: 10 });
    root.left .= tree.push(Node { val: 5 });
    root.right .= tree.push(Node { val: 20 });

    let img = tree.to_bytes();
    let pfx = img.len - bytes_of(tree).len;

    // 1. Corrupt a byte inside `root.left` (offset 4..8 of node 0) -> REJECTED!
    var bad_link = copy(img);
    bad_link[pfx + 5] = bad_link[pfx + 5] ^ 0x40;
    let t1, ok1 = from_bytes<Node[>..]>(bad_link);

    // 2. Truncate by 1 byte -> REJECTED!
    let t2, ok2 = from_bytes<Node[>..]>(img[..^1]);

    // 3. Append 1 trailing junk byte -> REJECTED!
    var extended = copy(img);
    extended.push(0);
    let t3, ok3 = from_bytes<Node[>..]>(extended);

    // 4. Flip a bit inside `root.val` (offset 0 of node 0) -> ACCEPTED (memory-safe, wrong value)!
    var edited_val = copy(img);
    edited_val[pfx] = edited_val[pfx] ^ 0x01;
    let t4, ok4 = from_bytes<Node[>..]>(edited_val);

    // 5. Empty framed image vs. unframed empty slice:
    var empty: Node[>..] = [];
    let t5, ok5 = from_bytes<Node[>..]>(empty.to_bytes());
    let t6, ok6 = from_bytes<Node[>..]>("");

    print("bad_link=", ok1, " (len=", t1.len, ") truncated=", ok2, " extended=", ok3);
    print("edited_val=", ok4, " (new val=", t4[0].val, ")");
    print("valid_empty=", ok5, " (len=", t5.len, ") unframed_empty=", ok6);
}
```

```
bad_link=false (len=0) truncated=false extended=false
edited_val=true (new val=11)
valid_empty=true (len=0) unframed_empty=false
```

正因如此，生产级容器格式会将 `from_bytes`（用于保证内存安全）与文件头 + 校验和或密码学哈希（用于保证数据完整性）结合使用——我们将在 13.4 节亲手构建这样的格式！

---

## 13.3 序列化变长模式的自相对树

`from_bytes` 并不仅限于固定大小的节点！它同样能够验证带有内联 `u8[varint]` 字符串和 `varint` 自相对链接的**变长模式枚举（`Tiny..`）**：

- 在验证过程中，第一遍扫描会顺序遍历字节流，校验每个标签、varint 与字符串长度，同时记录所有合法的元素起始偏移量。
- 第二遍扫描会验证每条非零的 `varint` 链接是否恰好落在这些元素起始位置之一。

来看看当一棵词树以自底向上的方式打包进变长模式的 `Tiny..[>..]` 中时，体积会缩小多少（先压入子节点再压入父节点，这样向后指向的偏移量就是极小的 1–2 字节 varint！）：

```goose
import std;

enum Tiny {
    Leaf { word: u8[varint], count: varint },
    Fork {
        word: u8[varint],
        count: varint,
        left: Tiny..&<varint>?,
        right: Tiny..&<varint>?,
    },
}

fn total_count(pool: Tiny..[>..]&) -> i64 {
    var sum = 0;
    for node in pool {
        match node {
            Leaf &l => { sum += l.count; },
            Fork &f => { sum += f.count; },
        }
    }
    return sum;
}

fn main() {
    var tiny: Tiny..[>..] = [];
    let a = tiny.push(Tiny.Leaf { word: "and", count: 2 });
    let d = tiny.push(Tiny.Leaf { word: "dog", count: 1 });
    let f = tiny.push(Tiny.Fork { word: "fox", count: 2, left: a, right: d });
    let q = tiny.push(Tiny.Leaf { word: "quick", count: 1 });
    let root = tiny.push(Tiny.Fork { word: "the", count: 3, left: f, right: q });

    let img = tiny.to_bytes();
    var reloaded, ok = from_bytes<Tiny..[>..]>(img);

    print("nodes=", tiny.len, " framed=", img.len, " bytes");
    print("reloaded ok=", ok, " total_count=", total_count(reloaded),
          " matches=", total_count(reloaded) == total_count(tiny));
    match root {
        Fork &fk => print("root=", fk.word, " x", fk.count),
        Leaf &lf => print("root=", lf.word, " x", lf.count),
    }
}
```

```
nodes=5 framed=37 bytes
reloaded ok=true total_count=9 matches=true
root=the x3
```

五个树节点——包含五个内联字符串、计数值、枚举标签和四条树链接——序列化、验证并重新加载总共只占 **37 字节**！

---

## 13.4 实战案例：带校验和的二进制容器（`"GSX1"`）

现在让我们把 `to_bytes`、`from_bytes`、`std` 中的 `hash` 以及 `binary` 模块组合起来，打造一个生产级的二进制容器格式：

```text
+------------+--------------+------------------+-----------------------------+
| "GSX1" (4B)| version (4B) | hash64 (8B LE)   | to_bytes(tree) framed blob  |
+------------+--------------+------------------+-----------------------------+
```

- **魔数 + 版本号：** 立即拒绝错误的文件类型或不兼容的格式版本。
- **64 位校验和（`hash(payload)`）：** 捕获*任何*意外损坏，包括标量计数中的 1 位翻转！
- **受验证的 `from_bytes<Node[>..]>`：** 即使面对校验和被伪造匹配的恶意构造文件，也能保证结构与指针安全！

*（在真实的桌面或服务端 CLI 应用中，可以 `import os;`，用 `write_file_atomic("index.gsx", blob)` 将生成的 `u8[>..]` 原子写入磁盘，并用 `read_file("index.gsx", blob)` 读回——这正是 Goose 的 `30_mini_minecraft.goose` 存档系统采用的模式！）*

```goose
import std;
import binary;

let MAGIC = "GSX1";
let FORMAT_VERSION: u32 = 1;

struct WordNode {
    word: u8[..16],
    count: i32,
    left: WordNode&<u32>?,
    right: WordNode&<u32>?,
}

fn insert_word(pool: WordNode[>..]&, word: const u8[:]) {
    if pool.len == 0 { pool.push(WordNode { word: word, count: 1 }); return; }
    var cur .= pool[0];
    loop {
        let c = compare(word, cur.word[..]);
        if c == 0 { cur.count += 1; return; }
        let next = if c < 0 { cur.left } else { cur.right };
        if next { cur .= next; continue; }
        if c < 0 { cur.left  .= pool.push(WordNode { word: word, count: 1 }); }
        else     { cur.right .= pool.push(WordNode { word: word, count: 1 }); }
        return;
    }
}

fn save_container(pool: WordNode[>..]&) -> u8[>..] {
    let payload = pool.to_bytes();
    let checksum = hash(payload);

    var out: u8[>..] = [];
    out.append(MAGIC);
    binary::write_u32le(out, FORMAT_VERSION);
    binary::write_u64le(out, checksum);
    out.append(payload);
    return out;
}

// Validates the container header and integrity checksum for ANY serialized type,
// returning a zero-copy slice of the framed payload ready for `from_bytes<T>`.
fn unwrap_container(blob: const u8[:]) -> const u8[:], const u8[:] {
    var r = binary::reader(blob);
    let magic, ok_m = binary::read_bytes(r, 4);
    guard ok_m && magic == MAGIC else { return "", "bad magic"; }

    let ver, ok_v = binary::read_u32le(r);
    guard ok_v && ver == FORMAT_VERSION else { return "", "unsupported version"; }

    let expected_hash, ok_h = binary::read_u64le(r);
    guard ok_h else { return "", "truncated header"; }

    let payload = blob[r.pos..];
    guard hash(payload) == expected_hash else { return "", "checksum mismatch"; }
    return payload, "";
}

fn try_load(label: const u8[:], blob: const u8[:]) {
    let payload, err = unwrap_container(blob);
    if err.len > 0 {
        print(label, ": err='", err, "'");
        return;
    }
    let loaded, ok = from_bytes<WordNode[>..]>(payload);
    if !ok {
        print(label, ": err='corrupt tree structure'");
        return;
    }
    print(label, ": ok! nodes=", loaded.len, " root=", loaded[0].word, " x", loaded[0].count);
}

fn main() {
    var tree: WordNode[>..] = [];
    each_word("goose runs fast and goose stays safe") { insert_word(tree, it); };

    let blob = save_container(tree);
    print("container size=", blob.len, " bytes (header=16 + payload=", blob.len - 16, ")");

    // 1. Clean reload:
    try_load("clean reload", blob);

    // 2. Wrong magic:
    var bad_magic = copy(blob);
    bad_magic[0] = 'X';
    try_load("bad magic   ", bad_magic);

    // 3. 1-bit flip inside a scalar count — NOW caught by checksum!
    var flipped_scalar = copy(blob);
    flipped_scalar[flipped_scalar.len - 1] = flipped_scalar[flipped_scalar.len - 1] ^ 0x01;
    try_load("bit flip    ", flipped_scalar);

    // 4. Forged checksum + corrupted link — caught by `from_bytes` verifier!
    var forged_payload: u8[>..] = [];
    forged_payload.append(blob[16..]);
    let pfx = forged_payload.len - bytes_of(tree).len;
    forged_payload[pfx + 21] = forged_payload[pfx + 21] ^ 0x40; // inside root.left
    var forged: u8[>..] = [];
    forged.append(MAGIC);
    binary::write_u32le(forged, FORMAT_VERSION);
    binary::write_u64le(forged, hash(forged_payload));          // attacker recomputes hash!
    forged.append(forged_payload);
    try_load("forged link ", forged);
}
```

```
container size=192 bytes (header=16 + payload=176)
clean reload: ok! nodes=6 root=goose x2
bad magic   : err='bad magic'
bit flip    : err='checksum mismatch'
forged link : err='corrupt tree structure'
```

请注意第 4 项测试（`forged link`）：即使攻击者破坏了一条链接*并且*为篡改后的有效载荷重新计算了合法校验和，`from_bytes` 依然会捕获非法偏移量并安全地拒绝该镜像！

---

## 练习

1. **测试零拷贝落盘保存（`bytes_of` + `format_uleb`）：** 不调用 `pool.to_bytes()`，改为使用 `var head: u8[>..] = []; format_uleb(head, bytes_of(pool).len);` 手动构造帧头，将 `head` 与 `bytes_of(pool)` 拼接起来，并验证 `from_bytes<WordNode[>..]>` 能够以完全相同的方式将其加载。
2. **测试 `from_bytes` 拒绝带相对链接的 `T[>..<]`：** 在 13.4 节中尝试调用 `from_bytes<WordNode[>..<]>(payload)`，查看 Goose 在编译期给出的具体报错信息，了解为什么增缩数组不能加载自相对链接。
3. **为 `Tiny` 添加查找遍历：** 为 13.3 节的变长模式树编写递归函数 `fn count_word(n: Tiny..&, target: const u8[:]) -> i64`，验证在 `reloaded` 上查询 `"fox"` 和 `"the"` 分别返回 `2` 和 `3`。

---

## 本章要点

- 凡是链接均为**自相对**（`T&<uN>`、`T&<varint>`）的结构都完全与内存位置无关：**`bytes_of`** 零拷贝查看其原始字节，**`to_bytes`** 生成带 ULEB128 帧头的镜像，**`from_bytes<T[>..]>`** 对其进行验证并加载回普通数组。
- `from_bytes` 保证**内存安全**（帧封装、规范 varint 编码、合法枚举标签、内联长度，以及每条链接精确指向元素起始位置），而容器头部 + 校验和则保证**数据完整性**。
- 固定大小结构体与带 `varint` 字符串和 `varint` 链接的变长模式枚举（`Tiny..`），都能无缝完成序列化与验证。

至此，**第三部分：抽象、控制流与数据结构**圆满结束！在**第四部分：并发、互操作与生态**中，我们将从**第 14 章：无共享并发**开始，探索 Goose 如何通过 `thread_fn`、类型化队列和受限工作线程彻底消除数据竞争、锁与原子操作。
