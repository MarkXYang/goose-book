#!/usr/bin/env python3
"""Hardened remote HTTP runner API for the Programming in Goose playground.

Designed to be deployed in a locked-down container (Fly.io, Render, Google
Cloud Run, Railway, or any Docker host) so readers browsing the static book
from anywhere can edit and execute live Goose programs.

Environment variables:
    PORT               Listen port (default: 8080)
    HOST               Bind address (default: 0.0.0.0)
    GOOSE_EXE          Path to goose binary (default: auto-detected on PATH)
    GOOSE_HOME         Writable cache dir for runtime object (default: /tmp/goose_home)
    ALLOWED_ORIGINS    CORS Allow-Origin value (default: *)
    MAX_CONCURRENT     Max simultaneous executions (default: 4)
    RATE_LIMIT_PER_MIN Max POST /api/run requests per client IP per minute (default: 30)
    RUN_TIMEOUT_SEC    Max wall-clock seconds for JIT run/check (default: 6)
    BUILD_TIMEOUT_SEC  Max wall-clock seconds for native build (default: 20)
    ALLOW_EXTERN       Set to 1 to allow `extern`/`embed_file` in submitted code (default: 0)
"""
import collections
import http.server
import json
import os
import re
import resource
import shutil
import signal
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

MAX_CODE_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 32 * 1024
BLOCKED_PATTERNS = [
    (re.compile(r"\bextern\b"), "`extern` declarations are disabled on the public playground server for security."),
    (re.compile(r"\bembed_file\s*\("), "`embed_file(...)` is disabled on the public playground server for security."),
]


def env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


HOST = os.environ.get("HOST", "0.0.0.0")
PORT = env_int("PORT", 8080)
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")
MAX_CONCURRENT = env_int("MAX_CONCURRENT", 4)
RATE_LIMIT_PER_MIN = env_int("RATE_LIMIT_PER_MIN", 30)
RUN_TIMEOUT_SEC = env_int("RUN_TIMEOUT_SEC", 6)
BUILD_TIMEOUT_SEC = env_int("BUILD_TIMEOUT_SEC", 20)
ALLOW_EXTERN = os.environ.get("ALLOW_EXTERN", "0") == "1"
GOOSE_HOME = os.environ.get("GOOSE_HOME", "/tmp/goose_home")

SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT)
RATE_LOCK = threading.Lock()
RATE_BUCKETS = collections.defaultdict(collections.deque)


def find_goose():
    explicit = os.environ.get("GOOSE_EXE")
    if explicit and os.path.exists(explicit):
        return explicit
    on_path = shutil.which("goose")
    if on_path:
        return on_path
    raise SystemExit("goose binary not found on PATH; set GOOSE_EXE")


def get_version(goose_exe):
    try:
        p = subprocess.run([goose_exe, "version"], capture_output=True, text=True, timeout=5)
        return (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception:
        return "goose"


def check_rate_limit(client_ip):
    now = time.monotonic()
    cutoff = now - 60.0
    with RATE_LOCK:
        q = RATE_BUCKETS[client_ip]
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= RATE_LIMIT_PER_MIN:
            return False
        q.append(now)
        # Keep memory bounded if many distinct IPs connect
        if len(RATE_BUCKETS) > 4096:
            stale = [k for k, v in RATE_BUCKETS.items() if not v or v[-1] < cutoff]
            for k in stale:
                del RATE_BUCKETS[k]
    return True


def _apply_child_limits(cpu_sec):
    """Apply per-process POSIX limits inside the spawned child.

    Note: We intentionally do NOT cap RLIMIT_AS to a small value because
    Goose reserves 2 GB of virtual address space per data stack (committed
    lazily by the OS). Physical RAM is bounded at the container/cgroup level.
    """
    os.setsid()
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_sec, cpu_sec))
        resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    except (ValueError, OSError):
        pass


def _run_sandboxed(cmd, cwd, timeout_sec, cpu_sec, env):
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=lambda: _apply_child_limits(cpu_sec),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_sec)
        return proc.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        stdout, stderr = proc.communicate()
        return 124, stdout, stderr, True


def _clean_output(text, tmpdir):
    if not text:
        return ""
    text = text.replace(tmpdir + os.sep, "").replace(tmpdir + "/", "")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"
    return text


def _strip_comments_and_strings(code):
    """Remove line/block comments and string literals before static safety checks."""
    out = []
    i = 0
    n = len(code)
    while i < n:
        if code[i:i + 2] == "//":
            while i < n and code[i] != "\n":
                i += 1
        elif code[i:i + 2] == "/*":
            depth = 1
            i += 2
            while i < n and depth > 0:
                if code[i:i + 2] == "/*":
                    depth += 1
                    i += 2
                elif code[i:i + 2] == "*/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
        elif code[i:i + 3] == '"""':
            i += 3
            end = code.find('"""', i)
            i = n if end == -1 else end + 3
        elif code[i] == '"':
            i += 1
            while i < n:
                if code[i] == "\\":
                    i += 2
                elif code[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
        else:
            out.append(code[i])
            i += 1
    return "".join(out)


def execute_goose(goose_exe, code, mode="run"):
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Empty code snippet.", "duration_ms": 0, "mode": mode}
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Snippet exceeds 64 KB limit.", "duration_ms": 0, "mode": mode}

    if mode not in ("run", "check", "stacks", "build"):
        mode = "run"

    if not ALLOW_EXTERN:
        stripped = _strip_comments_and_strings(code)
        for pat, msg in BLOCKED_PATTERNS:
            if pat.search(stripped):
                return {"ok": False, "exit_code": 1, "stdout": "", "stderr": msg, "duration_ms": 0, "mode": mode}

    has_main = re.search(r"^\s*fn\s+(?:::)?main\s*\(", code, re.M) is not None
    uses_threads = re.search(r"\bthread_fn\b", code) is not None
    source = code
    wrapped = False
    if not has_main and mode in ("run", "build", "stacks"):
        if not re.search(r"^\s*(?:struct|enum|fn|export|extern|namespace|type|root\s+struct)\b", code, re.M):
            source = "import std;\nfn main() {\n" + code + "\n}\n"
            wrapped = True

    effective_mode = mode
    if effective_mode == "run" and uses_threads:
        effective_mode = "build"
    elif effective_mode == "run" and not has_main and not wrapped:
        effective_mode = "check"

    os.makedirs(GOOSE_HOME, exist_ok=True)
    safe_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "GOOSE_HOME": GOOSE_HOME,
    }

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="goose_rem_") as tmpdir:
        src_path = os.path.join(tmpdir, "main.goose")
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(source)

        if effective_mode == "check":
            rc, out, err, timed_out = _run_sandboxed(
                [goose_exe, "check", src_path], tmpdir, RUN_TIMEOUT_SEC, RUN_TIMEOUT_SEC, safe_env
            )
        elif effective_mode == "stacks":
            rc, out, err, timed_out = _run_sandboxed(
                [goose_exe, "check", "--stacks", src_path], tmpdir, RUN_TIMEOUT_SEC, RUN_TIMEOUT_SEC, safe_env
            )
        elif effective_mode == "build":
            exe_path = os.path.join(tmpdir, "main")
            rc, out, err, timed_out = _run_sandboxed(
                [goose_exe, "build", "-o", exe_path, src_path], tmpdir, BUILD_TIMEOUT_SEC, BUILD_TIMEOUT_SEC, safe_env
            )
            if not timed_out and rc == 0:
                rc, out, err, timed_out = _run_sandboxed(
                    [exe_path], tmpdir, RUN_TIMEOUT_SEC, RUN_TIMEOUT_SEC, safe_env
                )
        else:
            rc, out, err, timed_out = _run_sandboxed(
                [goose_exe, "run", src_path], tmpdir, RUN_TIMEOUT_SEC, RUN_TIMEOUT_SEC, safe_env
            )

        duration_ms = int((time.monotonic() - t0) * 1000)
        if timed_out:
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": _clean_output(out, tmpdir),
                "stderr": f"Execution timed out after {RUN_TIMEOUT_SEC}s.",
                "duration_ms": duration_ms,
                "mode": effective_mode,
            }
        return {
            "ok": rc == 0,
            "exit_code": rc,
            "stdout": _clean_output(out, tmpdir),
            "stderr": _clean_output(err, tmpdir),
            "duration_ms": duration_ms,
            "mode": effective_mode,
        }


def make_handler(goose_exe, version):
    class RemoteRunnerHandler(http.server.BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGINS)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _json(self, status, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _client_ip(self):
            fwd = self.headers.get("Fly-Client-IP") or self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
            return self.client_address[0]

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/api/health"):
                self._json(200, {
                    "ok": True,
                    "service": "goose-book-playground",
                    "mode": "remote",
                    "version": version,
                    "limits": {
                        "max_concurrent": MAX_CONCURRENT,
                        "rate_limit_per_min": RATE_LIMIT_PER_MIN,
                        "run_timeout_sec": RUN_TIMEOUT_SEC,
                    },
                })
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path != "/api/run":
                self._json(404, {"ok": False, "error": "not found"})
                return

            ip = self._client_ip()
            if not check_rate_limit(ip):
                self._json(429, {
                    "ok": False,
                    "exit_code": 429,
                    "stdout": "",
                    "stderr": f"Rate limit exceeded ({RATE_LIMIT_PER_MIN} requests/min). Please wait a moment.",
                    "duration_ms": 0,
                })
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_CODE_BYTES + 4096:
                self._json(400, {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Invalid request size.", "duration_ms": 0})
                return

            try:
                req = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._json(400, {"ok": False, "exit_code": 1, "stdout": "", "stderr": f"Invalid JSON: {e}", "duration_ms": 0})
                return

            if not SEMAPHORE.acquire(timeout=3.0):
                self._json(503, {
                    "ok": False,
                    "exit_code": 503,
                    "stdout": "",
                    "stderr": "Playground runner is busy. Please retry in a second.",
                    "duration_ms": 0,
                })
                return
            try:
                result = execute_goose(goose_exe, req.get("code", ""), req.get("mode", "run"))
            finally:
                SEMAPHORE.release()
            self._json(200, result)

        def log_message(self, fmt, *args):
            sys.stdout.write(f"[runner] {self._client_ip()} - {fmt % args}\n")
            sys.stdout.flush()

    return RemoteRunnerHandler


class ReusableThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    goose_exe = find_goose()
    version = get_version(goose_exe)
    server = ReusableThreadingHTTPServer((HOST, PORT), make_handler(goose_exe, version))
    print(f"Goose remote playground runner listening on http://{HOST}:{PORT}")
    print(f"Compiler: {version} ({goose_exe})")
    print(f"Limits: concurrency={MAX_CONCURRENT}, rate={RATE_LIMIT_PER_MIN}/min, timeout={RUN_TIMEOUT_SEC}s")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
