# 第 10 章：错误处理与跨帧返回

大多数系统编程语言都在两种错误处理世界之间二选一。C 和 Rust 在每个调用点返回错误值——显式且高效，但深层调用栈上的每一个中间函数都必须包装自己的返回类型（如 `Result<T, E>`），并在几乎每一行穿插 `?` 或 `if (err)` 检查。C++ 和 Java 则抛出运行时异常——中间函数签名保持整洁，却以沉重的栈展开表、隐蔽的控制流以及运行时未捕获异常的风险为代价。

Goose 提供了第三条道路：**既没有内置的 `Result` 类型，也没有异常展开器**，而是将轻量级的局部惯用法与**静态检查的跨帧返回（`return E from f`）**结合在一起。

学完本章后，你将能够：

- 在 Goose 的三种错误惯用法——尾部 `bool`、可选引用 `T?` 以及 `return E from f`——之间做出清晰的选择。
- 使用 `return ... from` 跨越多层调用帧传播深层错误，同时保持中间函数签名简洁，并确保每一个 `defer` 都会执行。
- 使用标准库的 `exception` 模块（`exception::try` / `exception::throw`）编写带类型的 try/catch/finally 流程。
- 区分可恢复错误与强制终止（`assert`、`abort`、`exit`），并借助 `src_file()` 与 `src_line()` 在不使用宏的情况下捕获调用者位置。

---

## 10.1 选择合适的惯用法

Goose 将错误的表示方式交由你掌控，并提供四种定位明确的工具：

| 场景 | 惯用法 | 示例 |
|---|---|---|
| 局部二元结果操作 | 尾部 `bool` | `let port, ok = parse_port(s);` |
| 查找 / 搜索（“找到或缺失”） | 可选引用 `T?` | `let svc = find_service(list, "db");` |
| 跨越嵌套调用的深层错误 | `return E from f`（或 `exception`） | `return [], str("line ", n, ": bad key") from load;` |
| 不变量被破坏 / 不可恢复的缺陷 | `assert`、`abort`、`exit` | `assert(idx < buf.len);` |

对于局部失败，尾部 `bool` 与可选引用 `T?` 无需任何额外语法或包装类型：

```goose
import std;

struct Service { name: u8[..16], port: i32 }

fn parse_port(s: const u8[:]) -> i32, bool {
    let n, ok = parse_int(trim(s));
    guard ok && n >= 1 && n <= 65535 else { return 0, false; }
    return n as i32, true;
}

fn find_service(svcs: Service[:], name: const u8[:]) -> Service? {
    for &s in svcs {
        if s.name == name { return s; }
    }
    return null;
}

fn main() {
    for raw in ["8080", " 443 ", "0", "99999", "abc"] {
        let p, ok = parse_port(raw);
        if ok { print("'", raw, "' -> port ", p); }
        else  { print("'", raw, "' -> invalid"); }
    }

    var svcs: Service[>..] = [
        Service { name: "api", port: 8080 },
        Service { name: "db",  port: 5432 },
    ];
    let hit = find_service(svcs, "db");
    if hit { print("found ", hit.name, " on ", hit.port); }
}
```

```
'8080' -> port 8080
' 443 ' -> port 443
'0' -> invalid
'99999' -> invalid
'abc' -> invalid
found db on 5432
```

---

## 10.2 深层错误：`return E from f`

当解析器、加载器或校验器在四层调用帧深处失败时，你通常并不希望中间的那三个函数仅仅为了向上转发一条错误字符串，就都不得不改写为返回 `Result` 包装类型。

在 Goose 中，任何函数都可以直接写出 **`return E from f;`** ：它会立即将 `E` 作为当前最内层活跃的 `f` 调用的返回值返回，同时逐层展开中间的所有调用帧！

```goose
import std;

struct User { name: u8[..16], age: i32 }

var active_frames = 0;

fn load_users(text: const u8[:]) -> User[>..], u8[] {
    active_frames++;
    defer active_frames--;           // runs on BOTH normal return and long-distance return!
    var users: User[>..] = [];
    var lineno = 0;
    each_split(text, '\n') {
        lineno++;
        guard it.len > 0;
        users.push(parse_record(it, lineno)); // plain `User` return, built in place!
    };
    return users, "";
}

fn parse_record(line: const u8[:], lineno: i64) -> User {
    active_frames++;
    defer active_frames--;           // still runs when `parse_name` returns from `load_users`!
    let comma = find(line, ",");
    guard comma >= 0 else {
        return [], str("line ", lineno, ": expected name,age") from load_users;
    }
    let name = parse_name(line[..comma], lineno);
    let age, ok = parse_int(trim(line[comma + 1..]));
    guard ok && age >= 0 && age <= 150 else {
        return [], str("line ", lineno, ": invalid age") from load_users;
    }
    return User { name: name, age: age as i32 };
}

fn parse_name(raw: const u8[:], lineno: i64) -> u8[..16] {
    let t = trim(raw);
    guard t.len > 0 else {
        return [], str("line ", lineno, ": empty name") from load_users;
    }
    guard t.len <= 16 else {
        return [], str("line ", lineno, ": name too long") from load_users;
    }
    return t;
}

fn try_load(label: const u8[:], text: const u8[:]) {
    let users, err = load_users(text);
    if err.len > 0 {
        print(label, " -> ERROR: ", err, " (frames=", active_frames, ")");
    } else {
        print(label, " -> OK: ", users.len, " users (frames=", active_frames, ")");
    }
}

fn main() {
    try_load("good", "ada, 36\nlinus, 20\n");
    try_load("missing comma", "ada, 36\nlinus\n");
    try_load("empty name", "ada, 36\n, 20\n");
    try_load("bad age", "ada, -5\n");
}
```

```
good -> OK: 2 users (frames=0)
missing comma -> ERROR: line 2: expected name,age (frames=0)
empty name -> ERROR: line 2: empty name (frames=0)
bad age -> ERROR: line 1: invalid age (frames=0)
```

仔细观察以下四项特性，正是它们让 `return ... from` 在系统编程语言中运转得如此干净利落：

1. **静态检查：** 由于 Goose 会检查整个调用图，**每一条到达 `parse_name` 的调用路径**都必须处于某个活跃的 `load_users` 调用之内。直接从 `main` 调用 `parse_name` 会触发编译错误——运行时永远不可能出现未捕获的跨帧返回！
2. **零展开器：** 路径上的每个调用帧只需在调用返回后检查一个隐藏的判别标志，如果正在向上传播，就直接返回。常规的函数尾声（epilogue）会照常执行，因此每个数据栈都能以 O(1) 复杂度回退，并且每一个 `defer` 都会运行（每次出错后 `active_frames` 都会归 `0`！）。
3. **目标处直接构造错误值：** `str("line ", lineno, ": empty name")` 直接在 `load_users` 的返回目标处构造错误字符串——完全绕过所有正在被展开的中间帧。
4. **退出时携带所有权值：** 从具名函数跨帧返回时，可以携带拥有所有权的值（如 `u8[]`）或指向全局/静态数据的引用，但绝不能携带指向正在被展开作用域中局部变量的引用。

下面看看如果有人在调用栈上没有 `load_users` 时调用 `parse_name`，编译器会如何强制保证静态调用图安全：

<!-- compile-fail -->
```goose
import std;

fn load_users(text: const u8[:]) -> u8[] {
    let _ = parse_name(text);
    return "";
}

fn parse_name(raw: const u8[:]) -> u8[..16] {
    let t = trim(raw);
    guard t.len > 0 else { return "empty name" from load_users; }
    return t;
}

fn main() {
    print(parse_name("ada"));        // error: `load_users` is not active on this call path!
}
```

> **提示：** 如果目标函数位于另一个命名空间中，请显式加上命名空间限定，例如 `return err from ns::load;`。

---

## 10.3 使用 `exception` 模块实现结构化 Try/Catch

有时你希望编写可复用的错误处理辅助函数，而不把 `load_users` 这样特定调用者的名字写死在代码里——这样任何调用者都可以用 `try` 代码块捕获它们。

此时可以从标准库导入 **`exception`**。值得注意的是，`exception` **并非**什么特殊的运行时特性：它完全是用纯 Goose 代码构建在 `return ... from`、静态函数值和 `defer` 之上的！

| 函数 | 含义 |
|---|---|
| `exception::try<E>(body) { err => ... }` | 执行 `body()`；如果它抛出类型为 `E` 的值，则将 `err` 传给捕获块 |
| `exception::try<E>(body)` | 执行 `body()` 并丢弃任何抛出的 `E` |
| `exception::try<E>(body, catch, finally)` | 执行 `body()`，抛出时调用 `catch(err)`，并始终通过 `defer` 执行 `finally()` |
| `exception::throw(value)` | 直接退出至最近的外层 `try<E>` |

由于 Goose 每次调用只允许一个尾随代码块，因此至少需要具名传入其中一个回调（例如，传入嵌套 `fn` 作为 `body`，再将尾随代码块用作 `catch`）：

```goose
import exception;

enum LoadErr {
    Empty,
    TooLarge { got: i64, limit: i64 },
}

fn validate_size(n: i64) -> i64 {
    if n <= 0 { exception::throw(LoadErr.Empty); }
    if n > 100 { exception::throw(LoadErr.TooLarge { got: n, limit: 100 }); }
    return n;
}

fn process(n: i64) -> i64 {
    return validate_size(n) * 2;     // plain signature, no Result wrapper
}

fn run_case(n: i64) {
    var result = -1;
    var cleaned = false;

    fn body() { result = process(n); }
    fn on_err(e: LoadErr) {
        match e {
            Empty      => print("caught: empty input"),
            TooLarge t => print("caught: ", t.got, " exceeds ", t.limit),
        }
    }
    fn on_finally() { cleaned = true; }

    exception::try<LoadErr>(body, on_err, on_finally);
    print("  n=", n, " -> result=", result, " cleaned=", cleaned);
}

fn main() {
    run_case(25);
    run_case(0);
    run_case(250);
}
```

```
  n=25 -> result=50 cleaned=true
caught: empty input
  n=0 -> result=-1 cleaned=true
caught: 250 exceeds 100
  n=250 -> result=-1 cleaned=true
```

请注意关于 `exception::throw` 的两个精妙细节：
- **静态安全性依然成立：** 每一处 `exception::throw` 在每一条编译期调用路径上都必须存在与之匹配的外层 `exception::try<E>`，抛出不兼容的类型同样会导致编译错误。
- **发散传播：** 编译器知道 `exception::throw` 永远不会返回到其调用者，因此以 `exception::throw(...)` 结尾的分支不需要补写占位的返回值。（若要从 `catch` 块内部将诸如 `u8[]` 这样的拥有所有权的变长错误重新抛出到更外层的 `try`，请传入 `copy(err)`。）

---

## 10.4 强制终止与调用者位置（`src_file`、`src_line`）

### `assert`、`abort` 与 `exit`

当缺陷破坏了程序不变量（不可能出现的状态、索引越界、内部不变量损坏）时，应当尽早失败：

- **`assert(cond)`** —— 当 `cond` 为 `false`（对于可选引用则是 `null`，此时 `assert` 会在当前代码块的其余部分将其类型收窄为 `T&`！）时，附带源码行号中止程序。
- **`abort(msg)`** —— 立即以消息 `msg` 和源码位置终止程序。
- **`exit(code)`** —— 立即以整数退出状态码 `code` 退出进程。

`abort` 和 `exit` 都**永不返回**，而且类型检查器会将这一性质沿辅助函数向上传播——因此，如果某个 `if` 分支调用了一个总是会执行 `abort` 的辅助函数，就无需再提供占位返回值。需要注意的是，强制终止**不会**运行 `defer` 代码块，也无法被 `exception::try` 捕获（不过第 14 章会展示 `thread_spawn_contained` 如何干净地隔离工作线程的中止）。

### 无需宏的调用者位置：`src_file()` 与 `src_line()`

在 C 和 C++ 中，日志或测试断言必须借助 `__FILE__` 和 `__LINE__` 预处理器宏来报告*调用者是在哪里发起调用的*。Goose 为你提供了两个简洁的内置函数——**`src_file()`**（类型为 `const u8[:]` 的基础文件名）和 **`src_line()`**（类型为 `i64` 的从 1 开始的行号）——并配有一条巧妙的规则：

> 当 `src_file()` 或 `src_line()` 作为**默认参数值**出现时，任何省略该实参的调用都会填入**调用点**的文件名和行号，而不是被调函数处的文件名和行号！

```goose
import std;

fn die(reason: const u8[:]) {
    abort(str("fatal: ", reason));
}

fn require_positive(n: i64) -> i64 {
    if n > 0 { n } else { die("expected positive") } // `die` never returns!
}

fn trace(msg: const u8[:], line: i64 = src_line(), file: const u8[:] = src_file()) {
    print("[goose_file=", ends_with(file, ".goose"), " line=", line, "] ", msg);
}

fn main() {
    let ok = require_positive(7);
    trace("computed value");
    let line_here = src_line();
    trace("explicit override", 999, "custom.goose");
    print("ok=", ok, " line_here=", line_here);
}
```

```
[goose_file=true line=17] computed value
[goose_file=true line=999] explicit override
ok=7 line_here=18
```

---

## 10.5 实战示例：多段配置校验器

让我们把三种错误惯用法（`bool`、`T?`、`return ... from`）、`defer` 清理机制以及 `src_line()` 诊断信息整合到一个贴近实际的 `key=value` 配置加载器中，支持 `[section]` 分节标题、重复键检测以及取值范围校验：

```goose
import std;

struct Setting {
    section: u8[..16],
    key: u8[..16],
    value: i64,
}

var open_parsers = 0;

fn find_setting(settings: Setting[:], sec: const u8[:], key: const u8[:]) -> Setting? {
    for &s in settings {
        if s.section == sec && s.key == key { return s; }
    }
    return null;
}

fn parse_setting_value(raw: const u8[:], lineno: i64) -> i64 {
    let v, ok = parse_int(trim(raw));
    guard ok else {
        return [], str("line ", lineno, ": expected integer, got '", trim(raw), "'") from load_config;
    }
    guard v >= 0 && v <= 65535 else {
        return [], str("line ", lineno, ": value ", v, " out of range 0..65535") from load_config;
    }
    return v;
}

fn parse_kv(line: const u8[:], lineno: i64, sec: const u8[:], settings: Setting[>..]&) -> Setting {
    let eq = find(line, "=");
    guard eq > 0 else {
        return [], str("line ", lineno, ": expected key=value") from load_config;
    }
    let key = trim(line[..eq]);
    guard key.len > 0 && key.len <= 16 else {
        return [], str("line ", lineno, ": invalid key length") from load_config;
    }
    if find_setting(settings, sec, key) != null {
        return [], str("line ", lineno, ": duplicate key '", sec, ".", key, "'") from load_config;
    }
    let val = parse_setting_value(line[eq + 1..], lineno);
    return Setting { section: sec, key: key, value: val };
}

fn load_config(text: const u8[:]) -> Setting[>..], u8[] {
    open_parsers++;
    defer open_parsers--;
    var settings: Setting[>..] = [];
    var section: u8[..16] = "global";
    var lineno = 0;

    each_split(text, '\n') {
        lineno++;
        let line = trim(it);
        guard line.len > 0 && line[0] != '#';
        if line[0] == '[' {
            guard line.len >= 3 && line[^1..][0] == ']' else {
                return [], str("line ", lineno, ": malformed section header") from load_config;
            }
            let name = trim(line[1..^1]);
            guard name.len > 0 && name.len <= 16 else {
                return [], str("line ", lineno, ": bad section name") from load_config;
            }
            section.clear();
            format(section, name);
        } else {
            settings.push(parse_kv(line, lineno, section, settings));
        }
    };
    return settings, "";
}

fn check_cfg(label: const u8[:], text: const u8[:]) {
    let cfg, err = load_config(text);
    if err.len > 0 {
        print(label, ": ERROR -> ", err, " (open=", open_parsers, ")");
        return;
    }
    print(label, ": OK (", cfg.len, " settings, open=", open_parsers, ")");
    for s in cfg {
        print("  ", s.section, ".", s.key, " = ", s.value);
    }
}

fn main() {
    let valid = """
        # Server settings
        [http]
        port = 8080
        workers = 4

        [cache]
        max_mb = 512
        """;

    let dup_key = """
        [http]
        port = 8080
        port = 9090
        """;

    let bad_range = """
        [http]
        port = 70000
        """;

    check_cfg("valid", valid);
    check_cfg("dup_key", dup_key);
    check_cfg("bad_range", bad_range);
}
```

```
valid: OK (3 settings, open=0)
  http.port = 8080
  http.workers = 4
  cache.max_mb = 512
dup_key: ERROR -> line 3: duplicate key 'http.port' (open=0)
bad_range: ERROR -> line 2: value 70000 out of range 0..65535 (open=0)
```

注意 `parse_kv` 和 `parse_setting_value` 保持得多么简洁：普通的返回签名（`-> Setting`、`-> i64`）、直接向 `settings.push(...)` 中执行目标处直接构造、零堆分配、静态验证每一个错误都被 `load_config` 捕获，并且 `defer` 确保 `open_parsers` 始终归 `0`。

---

## 练习

1. **验证 `return ... from` 的静态检查：** 在第 10.5 节的示例中，尝试直接在 `main()` 中调用 `parse_setting_value("42", 1)`。点击 **▶ Run**，观察 Goose 给出的精确编译期诊断信息，切身体会未捕获的跨帧返回在编译期即被杜绝。
2. **使用 `exception::try<u8[]>` 重构第 10.5 节示例：** 将 `parse_setting_value` 中的 `return [], ... from load_config` 替换为 `exception::throw(str(...))`，并用 `exception::try<u8[]>(...)` 包裹解析主体。观察此时 `parse_setting_value` 如何变得可以被*任意*将其包裹在 `try<u8[]>` 中的调用者复用。
3. **编写无需宏的 `expect_eq` 测试辅助函数：** 利用 `line: i64 = src_line()` 和 `file: const u8[:] = src_file()`，编写 `fn expect_eq<T>(got: T, want: T, line: i64 = src_line(), file: const u8[:] = src_file())`，使其打印 `"PASS"` 或 `"FAIL at file:line: got ... want ..."`，并在 `main()` 中从三处不同的行调用它。

---

## 本章要点

- Goose 将错误处理与作用域精准匹配：**尾部 `bool`** 用于局部二元结果调用，**可选引用 `T?`** 用于零拷贝查找，而 **`return E from f`** 用于跨越多层调用帧的深层错误。
- `return E from f` 会在整个调用图上接受**静态检查**，无需异常展开器（仅使用隐藏的判别标志），会执行每一个中间帧的 `defer`，并直接在 `f` 的返回目标处构造错误值。
- 标准库的 **`exception`** 模块在 `return ... from` 与 `defer` 之上构建了带类型的 `try`/`throw`/`finally`，并具备同等的静态安全保证。
- `assert`、`abort` 和 `exit` 会立即停止执行（不运行 `defer`），而作为默认参数的 `src_file()` / `src_line()` 无需宏即可捕获调用者位置。

在**第 11 章：栈模型下的递归**中，我们将看到 Goose 如何优雅地处理递归函数（`recursive fn`）——以及为什么将嵌套递归函数与 `return ... from` 结合起来，能让递归下降解析器变得格外精炼。
