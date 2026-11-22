#!/usr/bin/env python3
"""Serve the built mdBook with a live local Goose compiler playground.

Usage:
    python3 scripts/serve_playground.py [--open] [--port 3000] [--watch] [--exe goose]

Features:
    - Builds the mdBook on startup (unless --no-build is passed).
    - Serves static HTML from book/ and exposes:
        GET  /api/health  -> compiler version and status
        POST /api/run     -> compiles/runs Goose code via local `goose`
    - Watches src/, theme/, and book.toml for edits and auto-rebuilds book/
      (enabled by default; disable with --no-watch).
    - Uses `goose run` (TinyCC JIT) for instant execution, and automatically
      uses `goose build` for `thread_fn` programs or when mode == "build".
"""
import argparse
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOOK_DIR = os.path.join(ROOT, "book")
MAX_CODE_BYTES = 128 * 1024
MAX_OUTPUT_CHARS = 64 * 1024


def find_compiler():
    on_path = shutil.which("goose")
    if on_path:
        return on_path
    exe = "goose.exe" if sys.platform == "win32" else "goose"
    sibling = os.path.join(os.path.dirname(ROOT), "goose")
    for d in ("build/Release", "build/RelWithDebInfo", "build", "build/Debug"):
        p = os.path.join(sibling, d, exe)
        if os.path.exists(p):
            return p
    raise SystemExit("no goose compiler found on PATH or under ../goose/build/; pass --exe")


def get_goose_version(goose_exe):
    try:
        p = subprocess.run([goose_exe, "version"], capture_output=True, text=True, timeout=5)
        return (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception:
        return "goose"


def build_book(quiet=False):
    mdbook = shutil.which("mdbook")
    if not mdbook:
        print("warning: mdbook not found on PATH; skipping build", file=sys.stderr)
        return False
    p = subprocess.run([mdbook, "build"], cwd=ROOT, capture_output=True, text=True)
    if p.returncode != 0:
        print("mdbook build failed:\n" + (p.stderr or p.stdout), file=sys.stderr)
        return False
    if not quiet:
        print("Built mdBook into book/")
    return True


def _clean_paths(text, tmpdir):
    if not text:
        return ""
    text = text.replace(tmpdir + os.sep, "").replace(tmpdir + "/", "")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n... (output truncated)"
    return text


def execute_goose(goose_exe, code, mode="run"):
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Empty code snippet.", "duration_ms": 0, "mode": mode}
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Snippet exceeds 128 KB limit.", "duration_ms": 0, "mode": mode}

    has_main = re.search(r"^\s*fn\s+(?:::)?main\s*\(", code, re.M) is not None
    uses_threads = re.search(r"\bthread_fn\b", code) is not None
    wrapped = False
    source = code
    if not has_main and mode in ("run", "build", "stacks"):
        # Wrap bare statement snippets in fn main() for convenience.
        if not re.search(r"^\s*(?:struct|enum|fn|export|extern|namespace|type|root\s+struct)\b", code, re.M):
            source = "import std;\nfn main() {\n" + code + "\n}\n"
            wrapped = True

    effective_mode = mode
    if effective_mode == "run" and uses_threads:
        effective_mode = "build"

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="goose_pg_") as tmpdir:
        src_path = os.path.join(tmpdir, "main.goose")
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(source)

        try:
            if effective_mode == "check":
                p = subprocess.run([goose_exe, "check", src_path], cwd=tmpdir, capture_output=True, text=True, timeout=10)
                return {
                    "ok": p.returncode == 0,
                    "exit_code": p.returncode,
                    "stdout": _clean_paths(p.stdout, tmpdir),
                    "stderr": _clean_paths(p.stderr, tmpdir),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "mode": "check",
                    "wrapped": wrapped,
                }

            if effective_mode == "stacks":
                p = subprocess.run([goose_exe, "check", "--stacks", src_path], cwd=tmpdir, capture_output=True, text=True, timeout=10)
                return {
                    "ok": p.returncode == 0,
                    "exit_code": p.returncode,
                    "stdout": _clean_paths(p.stdout, tmpdir),
                    "stderr": _clean_paths(p.stderr, tmpdir),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "mode": "stacks",
                    "wrapped": wrapped,
                }

            if effective_mode == "build":
                exe_path = os.path.join(tmpdir, "main.exe" if sys.platform == "win32" else "main")
                bp = subprocess.run([goose_exe, "build", "-o", exe_path, src_path], cwd=tmpdir, capture_output=True, text=True, timeout=25)
                if bp.returncode != 0:
                    return {
                        "ok": False,
                        "exit_code": bp.returncode,
                        "stdout": _clean_paths(bp.stdout, tmpdir),
                        "stderr": _clean_paths(bp.stderr, tmpdir),
                        "duration_ms": int((time.monotonic() - t0) * 1000),
                        "mode": "build",
                        "wrapped": wrapped,
                    }
                rp = subprocess.run([exe_path], cwd=tmpdir, capture_output=True, text=True, timeout=10)
                return {
                    "ok": rp.returncode == 0,
                    "exit_code": rp.returncode,
                    "stdout": _clean_paths(rp.stdout, tmpdir),
                    "stderr": _clean_paths(rp.stderr, tmpdir),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "mode": "build",
                    "wrapped": wrapped,
                }

            # Default: fast in-process JIT via `goose run`
            p = subprocess.run([goose_exe, "run", src_path], cwd=tmpdir, capture_output=True, text=True, timeout=10)
            return {
                "ok": p.returncode == 0,
                "exit_code": p.returncode,
                "stdout": _clean_paths(p.stdout, tmpdir),
                "stderr": _clean_paths(p.stderr, tmpdir),
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "mode": "run",
                "wrapped": wrapped,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": "",
                "stderr": "Execution timed out.",
                "duration_ms": int((time.monotonic() - t0) * 1000),
                "mode": effective_mode,
                "wrapped": wrapped,
            }


def make_handler(goose_exe, goose_version, serve_static=True):
    class PlaygroundHandler(http.server.SimpleHTTPRequestHandler):
        def _send_cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send_json(self, status, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self._send_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_OPTIONS(self):
            self.send_response(204)
            self._send_cors()
            self.end_headers()

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/api/health":
                self._send_json(200, {"ok": True, "version": goose_version, "backend": "local"})
                return
            if not serve_static:
                self._send_json(404, {"ok": False, "error": "API-only mode"})
                return
            super().do_GET()

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/api/run":
                self._send_json(404, {"ok": False, "error": "Not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_CODE_BYTES * 2:
                    self._send_json(400, {"ok": False, "stderr": "Invalid request size."})
                    return
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"ok": False, "stderr": f"Invalid JSON: {e}"})
                return

            code = data.get("code", "")
            mode = data.get("mode", "run")
            if mode not in ("run", "check", "stacks", "build"):
                mode = "run"
            result = execute_goose(goose_exe, code, mode=mode)
            self._send_json(200, result)

        def log_message(self, fmt, *args):
            if self.path.startswith("/api/"):
                sys.stderr.write("[playground] " + (fmt % args) + "\n")

    return functools.partial(PlaygroundHandler, directory=BOOK_DIR)


def _snapshot_mtimes():
    snap = {}
    targets = [
        os.path.join(ROOT, "book.toml"),
        os.path.join(ROOT, "src"),
        os.path.join(ROOT, "theme"),
    ]
    for t in targets:
        if os.path.isfile(t):
            snap[t] = os.path.getmtime(t)
        elif os.path.isdir(t):
            for dp, _, fns in os.walk(t):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    try:
                        snap[full] = os.path.getmtime(full)
                    except OSError:
                        pass
    return snap


def start_watcher():
    def loop():
        prev = _snapshot_mtimes()
        while True:
            time.sleep(1.0)
            curr = _snapshot_mtimes()
            if curr != prev:
                prev = curr
                print("[watch] Source changed — rebuilding book...")
                build_book(quiet=False)

    t = threading.Thread(target=loop, daemon=True)
    t.start()


class ReusableThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


def bind_server(host, port, handler, explicit_port):
    attempts = [port] if explicit_port else list(range(port, port + 20))
    last_err = None
    for candidate in attempts:
        try:
            return ReusableThreadingHTTPServer((host, candidate), handler), candidate
        except OSError as err:
            last_err = err
            if candidate == port and not explicit_port:
                print(f"Port {port} is already in use; trying next available port...")
    raise SystemExit(f"Could not bind to {host}:{port}: {last_err}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    ap.add_argument("--port", type=int, default=None, help="bind port (default: 3000, auto-increments if busy)")
    ap.add_argument("--exe", help="path to goose compiler (default: PATH or ../goose/build/)")
    ap.add_argument("--open", action="store_true", help="open the book in your default browser")
    ap.add_argument("--no-build", action="store_true", help="skip initial mdbook build")
    ap.add_argument("--no-watch", action="store_true", help="do not auto-rebuild on file edits")
    ap.add_argument("--api-only", action="store_true", help="only serve /api/* (use alongside mdbook serve)")
    a = ap.parse_args()

    goose_exe = os.path.abspath(a.exe) if a.exe else find_compiler()
    goose_version = get_goose_version(goose_exe)

    if not a.api_only and not a.no_build:
        build_book()
    if not a.api_only and not a.no_watch:
        start_watcher()

    handler = make_handler(goose_exe, goose_version, serve_static=not a.api_only)
    requested_port = a.port if a.port is not None else 3000
    server, actual_port = bind_server(a.host, requested_port, handler, explicit_port=a.port is not None)
    url = f"http://{a.host}:{actual_port}/"
    print(f"Goose Book Playground running at {url}")
    print(f"Compiler: {goose_version} ({goose_exe})")
    print("Press Ctrl+C to stop.")

    if a.open and not a.api_only:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
