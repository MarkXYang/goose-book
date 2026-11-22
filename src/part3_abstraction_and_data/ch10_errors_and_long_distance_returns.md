# Chapter 10: Error Handling and Long-Distance Returns

Most systems languages pick one of two error-handling worlds. C and Rust return error values at every call site—explicit and fast, yet every intermediate function along a deep call stack must wrap its return type (`Result<T, E>`) and sprinkle `?` or `if (err)` checks on every line. C++ and Java throw runtime exceptions—clean intermediate signatures, yet backed by heavy unwinding tables, hidden control flow, and runtime uncaught-exception hazards.

Goose offers a third path: **no built-in `Result` type and no exception unwinder**, combining lightweight local idioms with **statically checked long-distance returns (`return E from f`)**.

By the end of this chapter, you will be able to:

- Choose cleanly between Goose's three error idioms: trailing `bool`, optional `T?`, and `return E from f`.
- Propagate deep failures across arbitrary call frames with `return ... from`, keeping intermediate signatures plain while running every `defer`.
- Use the standard library's `exception` module (`exception::try` / `exception::throw`) for typed try/catch/finally flows.
- Distinguish recoverable errors from hard stops (`assert`, `abort`, `exit`) and capture caller locations without macros via `src_file()` and `src_line()`.

---

## 10.1 Choosing the Right Idiom

Goose leaves error representation in your hands and gives you four focused tools:

| Situation | Idiom | Example |
|---|---|---|
| Local two-outcome operation | Trailing `bool` | `let port, ok = parse_port(s);` |
| Lookup / search ("found or absent") | Optional `T?` | `let svc = find_service(list, "db");` |
| Deep failure across nested calls | `return E from f` (or `exception`) | `return [], str("line ", n, ": bad key") from load;` |
| Broken invariant / unrecoverable bug | `assert`, `abort`, `exit` | `assert(idx < buf.len);` |

For local failures, trailing `bool` and optional `T?` need zero extra syntax or wrapper types:

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

## 10.2 Deep Errors: `return E from f`

When a parser, loader, or validator fails four call frames deep, you rarely want the three functions in between to return `Result` wrappers just to forward an error string upward.

In Goose, any function can write **`return E from f;`**: it immediately returns `E` as the result of the innermost active call to `f`, unwinding every frame in between!

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

Look closely at the four properties that make `return ... from` work so cleanly in a systems language:

1. **Checked statically:** Because Goose inspects the whole call graph, **every call path** reaching `parse_name` must lie inside an active call of `load_users`. Calling `parse_name` from `main` directly is a compile error—nothing can ever go uncaught at runtime!
2. **Zero unwinder:** Each frame on the path simply checks a hidden discriminant flag after the call and returns immediately when propagating. Normal function epilogues execute, so every data stack rewinds in O(1) and every `defer` runs (`active_frames` returns to `0` on every error!).
3. **In-place error construction:** `str("line ", lineno, ": empty name")` builds the error string directly in `load_users`'s return destination—bypassing every frame being unwound.
4. **Owned values on exit:** A long-distance return from a named function may carry owned values (like `u8[]`) or references to globals/static data—never references into local variables whose scopes are being unwound.

Watch the compiler enforce static call-graph safety if someone calls `parse_name` without `load_users` on the call stack:

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

> **Tip:** If a target function lives in another namespace, qualify it explicitly as `return err from ns::load;`.

---

## 10.3 Structured Try/Catch with the `exception` Module

Sometimes you want reusable error helpers that do *not* hard-code the name of a specific caller like `load_users`—so any caller can catch them with a `try` block.

Import **`exception`** from the standard library. Remarkably, `exception` is **not** a special runtime feature: it is written in pure Goose on top of `return ... from`, static function values, and `defer`!

| Function | Meaning |
|---|---|
| `exception::try<E>(body) { err => ... }` | Runs `body()`; if it throws an `E`, passes `err` to the catch block |
| `exception::try<E>(body)` | Runs `body()` and discards any thrown `E` |
| `exception::try<E>(body, catch, finally)` | Runs `body()`, then `catch(err)` on throw, and always runs `finally()` via `defer` |
| `exception::throw(value)` | Exits directly to the nearest enclosing `try<E>` |

Because Goose permits one trailing block per call, name at least one of the callbacks (e.g., pass a nested `fn` for `body` and a trailing block for `catch`):

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

Notice two nice details about `exception::throw`:
- **Static safety still holds:** Every `exception::throw` must have a matching enclosing `exception::try<E>` on every compile-time call path, and throwing an incompatible type is a compile error.
- **Divergence propagation:** The compiler knows `exception::throw` never returns to its caller, so a branch ending in `exception::throw(...)` does not need a dummy return value. (To rethrow an owned variable-size error such as `u8[]` from inside a `catch` block to an outer `try`, pass `copy(err)`.)

---

## 10.4 Hard Stops and Caller Locations (`src_file`, `src_line`)

### `assert`, `abort`, and `exit`

When a bug violates a program invariant (impossible state, out-of-bounds index, corrupted internal invariant), fail fast:

- **`assert(cond)`** — aborts with the source line if `cond` is `false` (or `null` for an optional, which `assert` narrows to `T&` for the rest of the block!).
- **`abort(msg)`** — immediately ends the program with `msg` and the source location.
- **`exit(code)`** — immediately exits the process with integer exit status `code`.

Both `abort` and `exit` **never return**, and the typechecker propagates that fact through helper functions—so an `if` branch calling a helper that always calls `abort` needs no dummy value. Note that hard stops do **not** run `defer` blocks and cannot be caught by `exception::try` (though Chapter 14 shows how `thread_spawn_contained` isolates worker aborts cleanly).

### Macro-Free Caller Locations: `src_file()` and `src_line()`

In C and C++, logging or test assertions need `__FILE__` and `__LINE__` preprocessor macros to report *where the caller called from*. Goose gives you two clean builtins—**`src_file()`** (the base file name as `const u8[:]`) and **`src_line()`** (the 1-based line number as `i64`)—with one clever rule:

> When `src_file()` or `src_line()` appears as a **default parameter value**, any call that omits that argument fills in the **call site's** file and line instead of the callee's!

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

## 10.5 Worked Example: A Multi-Section Config Validator

Let us put all three error idioms (`bool`, `T?`, `return ... from`), `defer` cleanup, and `src_line()` diagnostics together in a realistic `key=value` configuration loader supporting `[section]` headers, duplicate-key detection, and range validation:

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

Notice how clean `parse_kv` and `parse_setting_value` stay: ordinary signatures (`-> Setting`, `-> i64`), in-place construction straight into `settings.push(...)`, zero heap allocations, static verification that every error is caught by `load_config`, and `open_parsers` guaranteed back at `0` by `defer`.

---

## Exercises

1. **Test static verification of `return ... from`:** In Section 10.5, try calling `parse_setting_value("42", 1)` directly inside `main()`. Click **▶ Run** and inspect Goose's exact compile-time diagnostic proving that uncaught long-distance returns are impossible.
2. **Refactor Section 10.5 to use `exception::try<u8[]>`:** Replace `return [], ... from load_config` in `parse_setting_value` with `exception::throw(str(...))` and wrap the parsing body in `exception::try<u8[]>(...)`. Notice how `parse_setting_value` becomes reusable across *any* caller that wraps it in `try<u8[]>`.
3. **Write a macro-free `expect_eq` test helper:** Using `line: i64 = src_line()` and `file: const u8[:] = src_file()`, write `fn expect_eq<T>(got: T, want: T, line: i64 = src_line(), file: const u8[:] = src_file())` that prints `"PASS"` or `"FAIL at file:line: got ... want ..."`, and call it from three different lines in `main()`.

---

## Key Takeaways

- Goose matches error handling to scope: **trailing `bool`** for local two-outcome calls, **optional `T?`** for zero-copy lookups, and **`return E from f`** for deep multi-frame errors.
- `return E from f` is **statically checked** across the whole call graph, runs without an unwinder (using a hidden discriminant), executes every intermediate `defer`, and builds error values directly in `f`'s destination.
- The standard library's **`exception`** module layers typed `try`/`throw`/`finally` on top of `return ... from` and `defer` with the same static guarantees.
- `assert`, `abort`, and `exit` stop execution immediately (without running `defer`), and `src_file()` / `src_line()` as default parameters capture caller locations with zero macros.

In **Chapter 11: Recursion Under the Stack Model**, we see how Goose handles recursive functions (`recursive fn`) cleanly—and why combining nested recursive functions with `return ... from` makes recursive-descent parsers remarkably concise.
