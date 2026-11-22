# 第 12 章：相对引用与无指针数据结构

普通的机器引用 `T&` 占用 8 字节，存储原始虚拟地址。对于局部变量和函数参数，这种方式非常高效；但在大型树、图或链表中，8 字节指针会带来两项切实的开销：

1. **缓存膨胀：** 一个包含 `i32` 键和两个 8 字节指针的二叉树节点需要占用 20–24 字节——每条缓存行中超过 75% 的空间都耗费在指针而非数据上。
2. **绑定进程的地址：** 原始虚拟地址在当前进程之外毫无意义，因此将指针图保存到磁盘必须自行编写定制的序列化器和指针重定位加载器。

Goose 通过**相对引用**同时解决了这两个问题——它们是带类型检查、带越界检查的透明引用，以紧凑的 **1、2 或 4 字节偏移量**形式存储（`T&<u8>`、`T&<u16>`、`T&<u32>`、`T&<u64>` 或 `T&<varint>`）。读取相对引用会直接得到普通的 `T&`；存储相对引用时则会自动编码偏移量。

读完本章后，你将能够：

- 在**自相对引用**（`T&<u32>`）和**池相对引用**（`T&<u32 in pool>`）之间做出合适选择。
- 在单个只增数组内构建紧凑、无指针的二叉搜索树，并自如应对自相对引用的不可复制规则。
- 使用 `slot_pool<T>`（槽位池）、`alloc_with` 和 **`self`** 初始化器构造不含空指针的循环双向链表。
- 结合 `slot_pool`、`slice_pool`（切片池）与 4 字节的 `in pool.items` 链接，以零堆分配实现可变多叉目录树。

---

## 12.1 自相对引用与池相对引用

相对引用分为两种形式，唯一的区别在于偏移量的计算基准点不同：

```text
1. Self-relative:  T&<u32>  /  T&<u32>?
   Offset = target_address - field_address   (0 means null)
   [ node 0 ] ... [ node 1: left = +16 ] ---> [ node 2 ]
                               ^                   ^
                          field_addr          target_addr
   -> Position-independent! The entire array means the same thing anywhere in RAM or on disk.

2. Pool-relative:  T&<u32 in pool>  /  T&<u32 in pool.items>?
   Stored value = (target_address - pool_base) + 1   (0 means null)
   pool_base + 0        + 16          + 32
   [ node 0 ]           [ node 1 ]    [ node 2 ]
   Stored in ANY array (even another array!): offset = 32 + 1 = 33 -> points to node 2!
```

两种形式都支持可选引用后缀 `?`（`T&<u32>?`），用 `0` 表示 `null`，因此一个可空的 4 字节链接总共仅占 **4 字节**（零额外标记字节）。

| 特性 | 自相对引用（`T&<u32>`） | 池相对引用（`T&<u32 in pool>`） |
|---|---|---|
| 可用于局部数组或参数数组？ | **可以**（无需全局名称） | **不可以**（`pool` 必须是全局只增 `var` 数组或 `root.items` 字段） |
| 支持从*其他*数组指向池内元素？ | **不可以**（目标必须位于同一数组中） | **可以**（例如外部索引或保存子节点链接的 `slice_pool`） |
| 位置无关 / 支持 `to_bytes` 序列化？ | **支持**（见第 13 章！） | **不支持**（依赖于外部 `pool` 的基址） |
| 值可复制？ | **不可复制**（偏移量取决于字段自身地址） | **可复制**（偏移量仅取决于 `pool` 的基址） |
| 从容器读取时的根精度 | 与外层数组完全一致 | **精确根**（`pool` 是该类型身份的一部分！） |

注意，函数的参数或返回类型从不写成相对引用——只需写普通的 `T&` 或 `T?`，相对引用字段会在读取时自动解码，并在 `.=` 赋值存储时自动重新编码。

---

## 12.2 自相对树与不可复制性

下面是一棵完整的二叉搜索树，它完全驻留在**单个只增数组**中，每个节点仅占 **16 字节**（两个 `i32` 加上两个可空的 4 字节自相对链接——如果去掉 `count` 则仅占 12 字节）：

```goose
struct Node {
    key: i32,
    count: i32 = 1,
    left: Node&<u32>?,
    right: Node&<u32>?,
}

fn insert(pool: Node[>..]&, key: i32) {
    if pool.len == 0 {
        pool.push(Node { key: key });
        return;
    }
    var cur .= pool[0];              // `.=` binds a reference (`=` would try to copy!)
    loop {
        if key == cur.key {
            cur.count += 1;
            return;
        }
        let next = if key < cur.key { cur.left } else { cur.right };
        if next {
            cur .= next;             // walk down via narrowed reference
            continue;
        }
        // `pool.push(...)` builds the new node in place and never invalidates `cur`!
        if key < cur.key { cur.left  .= pool.push(Node { key: key }); }
        else             { cur.right .= pool.push(Node { key: key }); }
        return;
    }
}

recursive fn in_order(n: Node&, out: i32[>..]&) {
    let l = n.left;
    if l { in_order(l, out); }
    out.push(n.key);
    let r = n.right;
    if r { in_order(r, out); }
}

fn main() {
    var pool: Node[>..] = [];
    for k in [50, 30, 70, 20, 40, 60, 80, 30] {
        insert(pool, k as i32);
    }
    var keys: i32[>..] = [];
    in_order(pool[0], keys);
    print("sorted=", keys);
    print(pool.len, " nodes in ", bytes_of(pool).len, " bytes; key 30 count=", pool[1].count);
}
```

```
sorted=[20, 30, 40, 50, 60, 70, 80]
7 nodes in 112 bytes; key 30 count=2
```

### 为什么包含自相对引用的结构体不可复制

由于自相对偏移量是以字段*自身的地址*为基准计算的，如果将一个 `Node` 复制到不同的地址（例如局部变量或另一个数组），其偏移量就会悄无声息地指向错误的位置！因此，编译器禁止复制任何包含自相对链接的值：

<!-- compile-fail -->
```goose
struct Node { key: i32, left: Node&<u32>? }

fn main() {
    var pool: Node[>..] = [Node { key: 1 }];
    let bad = pool[0];               // error: cannot copy value containing self-relative link
    print(bad.key);
}
```

处理包含自相对引用的结构体有三种地道的方式：
- **目标处直接构造：** `pool.push(Node { ... })`（若一个函数或代码块返回一个新的 `Node` 并直接传入 `pool.push(...)`，它同样会在 `pool` 的目标处直接构造！）。
- **按引用绑定：** 使用 `let n .= pool[0];`，而不是 `let n = pool[0];`。
- **按引用传递和匹配：** 使用 `fn walk(n: Node&)` 以及 `match e { Add &a => ... }`。

---

## 12.3 池相对链接、`self` 与无空指针双向链表

带有哨兵节点（sentinel node）的循环双向链表以简洁著称：整个结构中**没有任何 `null` 链接**，因此每次插入和删除都执行完全相同的四次链接更新，不存在任何边界分支。

但问题在于：哨兵节点尚未创建完成之前，如何把它的非可选 `prev` 和 `next` 链接初始化为指向*它自己*？

答案就是 **`self`**！在目标处直接构造的结构体或变体字面量中（例如通过 `slot_pool.alloc_with()`），写下 `prev: self, next: self` 就能将这些非可选相对链接直接初始化为指向正在构造的节点本身！

```goose
import slot_pool;

struct ListNode {
    val: i64,
    prev: ListNode&<u32 in list_pool.items>,
    next: ListNode&<u32 in list_pool.items>,
}

var list_pool = slot_pool<ListNode> {};
// Sentinel allocated once before `main`: `self` points directly to slot 0!
let sentinel .= list_pool.alloc_with() {
    ListNode { val: 0, prev: self, next: self }
};

fn insert_after(p: ListNode&, v: i64) -> ListNode& {
    let n .= list_pool.alloc_with() { ListNode { val: v, prev: p, next: p.next } };
    n.next.prev .= n;
    p.next .= n;
    return n;
}

fn unlink_and_free(n: ListNode&) {
    n.prev.next .= n.next;
    n.next.prev .= n.prev;
    list_pool.free_ref(n);
}

fn push_back(v: i64) -> ListNode& { insert_after(sentinel.prev, v) }
fn is_end(n: ListNode&) -> bool   { n .== sentinel }

fn each_node<F>() {
    var cur .= sentinel.next;
    while !is_end(cur) {
        let next .= cur.next;        // read `next` before `F` in case `F` removes `cur`!
        F(cur);
        cur .= next;
    }
}

fn main() {
    for v in [10, 20, 30, 40, 50] { push_back(v); }

    // Remove multiples of 20 during iteration, returning their slots to `list_pool`:
    each_node() { if it.val % 20 == 0 { unlink_and_free(it); } };

    // Push two new values — they reuse the two freed slots!
    push_back(77);
    push_back(88);

    var forward: i64[>..] = [];
    each_node() { forward.push(it.val); };

    var backward: i64[>..] = [];
    var cur .= sentinel.prev;
    while !is_end(cur) {
        backward.push(cur.val);
        cur .= cur.prev;
    }

    print("forward:  ", forward);
    print("backward: ", backward);
    print("slots used: ", list_pool.items.len, " (16 bytes/node)");
}
```

```
forward:  [10, 30, 50, 77, 88]
backward: [88, 77, 50, 30, 10]
slots used: 6 (16 bytes/node)
```

请注意以下三点如何天衣无缝地协同工作：
- 每个 `ListNode` 仅占 **16 字节**（1 个 `i64` 加上 2 个 4 字节的 `in list_pool.items` 链接），且整个程序中没有任何 `null` 检查。
- `is_end(n)` 使用 **`.==`**（引用同一性比较），而不是 `==`（后者会比较节点的内容）。
- 即使删除了 2 个节点又插入了 2 个新节点，`list_pool.items.len` 仍然保持在 `6`（1 个哨兵 + 5 个节点）！

### 用 4 字节链接索引变长模式顺序数组

池相对链接还能为任意全局变长模式顺序数组（`Mark..[>..]`）提供紧凑的 4 字节随机访问索引：

```goose
enum Mark { Circle { r: f64 }, Label { text: u8[varint] }, Dot }

var marks:   Mark..[>..]                  = [];
var mark_at: (Mark..&<u32 in marks>)[>..] = [];

fn main() {
    mark_at.push(marks.push(Mark.Label { text: "hello, goose" }));
    mark_at.push(marks.push(Mark.Circle { r: 1.5 }));
    mark_at.push(marks.push(Mark.Dot));

    for idx in [2, 0, 1] {
        match mark_at[idx] {
            Label l  => print(idx, ": label \"", l.text, "\""),
            Circle c => print(idx, ": circle r=", c.r),
            Dot      => print(idx, ": dot"),
        }
    }
    print(bytes_of(marks).len, " bytes of marks, ", mark_at.len, " links of 4 bytes");
}
```

```
2: dot
0: label "hello, goose"
1: circle r=1.5
24 bytes of marks, 3 links of 4 bytes
```

---

## 12.4 实战案例：内存目录树（`slot_pool` + `slice_pool`）

现在，我们将第 8 章、第 11 章和第 12 章学到的所有工具组合起来，构建一棵支持 `mkdir`/`touch`、`mv` 以及递归 `rm -r` 的可变多叉**目录树**——全程**零堆分配**：

- 节点存放在 `var dir_nodes = slot_pool<DirNode> {};` 中。
- 每个目录的子节点作为一段连续的 4 字节 `DirNode&<u32 in dir_nodes.items>?` 链接序列，存放在 `var dir_runs = slice_pool<DirLink> {};` 中。
- 删除子树（`rm_tree`）会释放其中的每一个节点存储槽位*以及*每一段子节点链接切片；随后创建的新文件会立即复用这两类空间！

```goose
import slot_pool;
import slice_pool;

type DirLink = DirNode&<u32 in dir_nodes.items>?;

struct DirNode {
    name: u8[..15],                  // fixed inline name
    children: DirLink[:],            // contiguous run of 4-byte links in `dir_runs`
}

var dir_nodes = slot_pool<DirNode> {};
var dir_runs  = slice_pool<DirLink> {};

fn make_node(name: const u8[:]) -> DirNode& {
    return dir_nodes.alloc_ref(DirNode { name: name, children: dir_runs.alloc_slice(0) });
}

let fs_root .= make_node("/");

fn attach(dir: DirNode&, child: DirNode&) {
    let k = dir.children.len;
    dir.children = dir_runs.realloc_slice(dir.children, k + 1);
    dir.children[k] .= child;
}

fn add_entry(dir: DirNode&, name: const u8[:]) -> DirNode& {
    let n .= make_node(name);
    attach(dir, n);
    return n;
}

fn find_child_index(dir: DirNode&, target: DirNode&) -> i64 {
    for i in dir.children.len {
        let e = dir.children[i];
        assert(e);
        if e .== target { return i; }
    }
    abort("child not found");
}

fn detach(dir: DirNode&, idx: i64) -> DirNode& {
    let n = dir.children[idx];
    assert(n);
    for j in idx..dir.children.len - 1 {
        dir.children[j] .= dir.children[j + 1];
    }
    dir.children = dir_runs.realloc_slice(dir.children, dir.children.len - 1);
    return n;
}

recursive fn release_subtree(n: DirNode&) {
    for e in n.children {
        assert(e);
        release_subtree(e);
    }
    dir_runs.free_slice(n.children);
    dir_nodes.free_ref(n);
}

fn rm_entry(dir: DirNode&, child: DirNode&) {
    detach(dir, find_child_index(dir, child));
    release_subtree(child);
}

fn mv_entry(from_dir: DirNode&, child: DirNode&, to_dir: DirNode&) {
    attach(to_dir, detach(from_dir, find_child_index(from_dir, child)));
}

recursive fn dump_tree(n: DirNode&, depth: i64) -> i64 {
    {
        var line: u8[>..] = [];
        for i in depth { line.append("  "); }
        format(line, n.name);
        print(line);
    }
    var count = 1;
    for e in n.children {
        assert(e);
        count += dump_tree(e, depth + 1);
    }
    return count;
}

fn show_state(label: const u8[:]) {
    print("=== ", label, " ===");
    let live = dump_tree(fs_root, 0);
    print("live=", live, " | node slots=", dir_nodes.items.len,
          " | link cells=", dir_runs.items.len);
}

fn main() {
    let home .= add_entry(fs_root, "home/");
    let ann  .= add_entry(home, "ann/");
    let todo .= add_entry(ann, "todo.txt");
    let bob  .= add_entry(home, "bob/");
    add_entry(bob, "save.dat");
    let etc  .= add_entry(fs_root, "etc/");
    add_entry(etc, "hosts");
    show_state("initial");

    // Delete /home/bob/ and its child — 2 node slots + runs freed:
    rm_entry(home, bob);

    // Move /home/ann/todo.txt -> /etc/todo.txt:
    mv_entry(ann, todo, etc);

    // Create two new entries — reuses both freed node slots without growing!
    let carl .= add_entry(home, "carl/");
    add_entry(carl, "notes.md");
    show_state("after rm, mv, and reuse");
}
```

```
=== initial ===
/
  home/
    ann/
      todo.txt
    bob/
      save.dat
  etc/
    hosts
live=8 | node slots=8 | link cells=7
=== after rm, mv, and reuse ===
/
  home/
    ann/
    carl/
      notes.md
  etc/
    hosts
    todo.txt
live=8 | node slots=8 | link cells=7
```

看看这个结果：删除 `/home/bob` + `save.dat`、移动 `todo.txt`、再添加 `/home/carl` + `notes.md` 之后，**两个池**的规模纹丝不动，始终保持在 **8 个节点存储槽位和 7 个链接单元**——全程使用带类型检查的引用、4 字节子节点链接，且零堆分配！

---

## 练习

1. **将二叉搜索树压缩为 2 字节链接：** 在第 12.2 节中，将 `left` 和 `right` 从 `Node&<u32>?` 改为 `Node&<u16>?`（并删除 `count` 字段）。点击 **▶ Run**，验证每个节点现在仅占 **8 字节**（`4 + 2 + 2`）。
2. **为双向链表添加 `move_to_front`：** 在第 12.3 节中，编写 `fn move_to_front(n: ListNode&)`，在不触碰 `list_pool` 的前提下将 `n` 从当前位置摘下并直接拼接到 `sentinel` 之后（0 次分配、6 次指针赋值），然后将 `77` 移到链表头部。
3. **为目录树添加 `find_path`：** 编写辅助函数 `fn find_by_name(dir: DirNode&, name: const u8[:]) -> DirNode?`，返回与 `name` 匹配的、经过类型收窄的子节点引用（若不存在则返回 `null`），并测试从 `fs_root` 出发查找 `"etc/"` 和 `"hosts"`。

---

## 本章要点

- **相对引用**（`T&<u8>`、`T&<u16>`、`T&<u32>`，以及以 `0` 表示 `null` 的可选引用 `T&<u32>?`）将带类型检查和越界检查的链接压缩至 1–4 字节，并在读取时透明解码为 `T&`。
- **自相对引用（`T&<u32>`）** 以字段自身地址为基准计算偏移量——非常适合局部数组和位置无关的序列化数据块（见第 13 章），且持有自相对引用的值只能在目标处直接构造，不可复制。
- **池相对引用（`T&<u32 in pool>`）** 以全局只增数组（或 `root.items`）为基准计算偏移量——非常适合跨数组索引、频繁重连的 `slot_pool` 结构，并且拥有精确根。
- 结构体字面量中的 **`self`** 可借助 `slot_pool.alloc_with()` 优雅地初始化非可选循环链接（`prev: self, next: self`），彻底消除哨兵节点中的 `null` 边界情况。

既然你已经能够使用自相对链接构建无指针、位置无关的树和图，接下来的**第 13 章：零拷贝持久化与受验证序列化**将向你展示：将整棵树保存到磁盘只需一次字节写入，而从不受信任的文件中安全加载回来，也只需一次经过验证的 `from_bytes` 调用！
