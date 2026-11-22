#!/usr/bin/env python3
"""Pre-compute verified Goose outputs for every code block in the book.

Generates `theme/goose-precomputed.js` so readers browsing the static book
from anywhere (GitHub Pages, offline file://, mobile, or when a remote runner
is offline/sleeping) can click Run or Stacks on any original book listing and
see real compiler-verified output instantaneously.

Usage:
    GOOSE_HOME=/tmp/goose_home python3 scripts/build_precomputed.py [--exe goose]
"""
import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "theme", "goose-precomputed.js")

FENCE_RE = re.compile(r"^```goose\s*$")
END_RE = re.compile(r"^```\s*$")
MARKER_RE = re.compile(r"^\s*<!--\s*(fragment|check-only|build|compile-fail)\s*-->\s*$")


def hash_code(code: str) -> str:
    """Deterministic 64-bit hex key matching theme/goose-playground.js."""
    data = code.encode("utf-8")
    h1 = 0x811C9DC5
    h2 = 0x9E3779B9
    for b in data:
        h1 ^= b
        h1 = (h1 * 0x01000193) & 0xFFFFFFFF
        h2 ^= b
        h2 = (h2 * 0x01000193) & 0xFFFFFFFF
        h2 = ((h2 << 7) | (h2 >> 25)) & 0xFFFFFFFF
    return f"{h1:08x}{h2:08x}"


def find_compiler(explicit=None):
    if explicit:
        return explicit
    on_path = shutil.which("goose")
    if on_path:
        return on_path
    exe = "goose.exe" if sys.platform == "win32" else "goose"
    sibling = os.path.join(os.path.dirname(ROOT), "goose")
    for d in ("build/Release", "build/RelWithDebInfo", "build", "build/Debug"):
        p = os.path.join(sibling, d, exe)
        if os.path.exists(p):
            return p
    raise SystemExit("no goose compiler found on PATH; pass --exe")


def get_version(goose_exe):
    try:
        p = subprocess.run([goose_exe, "version"], capture_output=True, text=True, timeout=5)
        return (p.stdout or p.stderr).strip().splitlines()[0]
    except Exception:
        return "goose"


def extract_blocks(md_path):
    with open(md_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    blocks = []
    i = 0
    while i < len(lines):
        if FENCE_RE.match(lines[i]):
            marker = ""
            j = i - 1
            while j >= 0 and not lines[j].strip():
                j -= 1
            if j >= 0:
                m = MARKER_RE.match(lines[j])
                if m:
                    marker = m.group(1)
            body = []
            i += 1
            while i < len(lines) and not END_RE.match(lines[i]):
                body.append(lines[i])
                i += 1
            code = "".join(body).rstrip("\n")
            blocks.append((marker, code))
        i += 1
    return blocks


def clean_paths(text, tmpdir):
    if not text:
        return ""
    return text.replace(tmpdir + os.sep, "").replace(tmpdir + "/", "")


def prepare_source(code, mode):
    has_main = re.search(r"^\s*fn\s+(?:::)?main\s*\(", code, re.M) is not None
    if not has_main and mode in ("run", "build", "stacks"):
        if not re.search(r"^\s*(?:struct|enum|fn|export|extern|namespace|type|root\s+struct)\b", code, re.M):
            return "import std;\nfn main() {\n" + code + "\n}\n", True
    return code, False


def run_one(goose_exe, code, mode):
    source, wrapped = prepare_source(code, mode)
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="goose_pre_") as tmpdir:
        src_path = os.path.join(tmpdir, "main.goose")
        with open(src_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(source)

        if mode == "check":
            p = subprocess.run([goose_exe, "check", src_path], cwd=tmpdir, capture_output=True, text=True, timeout=15)
        elif mode == "stacks":
            p = subprocess.run([goose_exe, "check", "--stacks", src_path], cwd=tmpdir, capture_output=True, text=True, timeout=15)
        elif mode == "build":
            exe_path = os.path.join(tmpdir, "main.exe" if sys.platform == "win32" else "main")
            bp = subprocess.run([goose_exe, "build", "-o", exe_path, src_path], cwd=tmpdir, capture_output=True, text=True, timeout=30)
            if bp.returncode != 0:
                return {
                    "ok": False,
                    "exit_code": bp.returncode,
                    "stdout": clean_paths(bp.stdout, tmpdir),
                    "stderr": clean_paths(bp.stderr, tmpdir),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "mode": "build",
                }
            p = subprocess.run([exe_path], cwd=tmpdir, capture_output=True, text=True, timeout=15)
        else:
            p = subprocess.run([goose_exe, "run", src_path], cwd=tmpdir, capture_output=True, text=True, timeout=15)

        return {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": clean_paths(p.stdout, tmpdir),
            "stderr": clean_paths(p.stderr, tmpdir),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "mode": mode,
        }


def main():
    ap = argparse.ArgumentParser(description="Build static pre-computed Goose outputs for the book.")
    ap.add_argument("--exe", default=None, help="Path to goose compiler")
    args = ap.parse_args()

    goose_exe = find_compiler(args.exe)
    version = get_version(goose_exe)

    md_files = sorted(
        glob.glob(os.path.join(ROOT, "src", "**", "*.md"), recursive=True)
        + glob.glob(os.path.join(ROOT, "zh", "src", "**", "*.md"), recursive=True)
    )
    entries = {}
    total = 0

    for md in md_files:
        for marker, code in extract_blocks(md):
            if marker == "fragment":
                continue
            key = hash_code(code)
            if key in entries:
                continue
            total += 1
            default_mode = "check" if marker == "check-only" else ("build" if marker == "build" else "run")
            # For snippets that only declare functions/structs (no fn main), `run` still checks cleanly if we use `check` when no main and not wrappable.
            has_main = re.search(r"^\s*fn\s+(?:::)?main\s*\(", code, re.M) is not None
            _, wrapped = prepare_source(code, default_mode)
            eff_mode = default_mode
            if eff_mode == "run" and not has_main and not wrapped:
                eff_mode = "check"

            run_res = run_one(goose_exe, code, eff_mode)
            stacks_res = run_one(goose_exe, code, "stacks")
            entries[key] = {
                "run": run_res,
                "stacks": stacks_res,
            }

    payload = {
        "version": version,
        "count": len(entries),
        "entries": entries,
    }
    js = (
        "/* Auto-generated by scripts/build_precomputed.py — verified Goose outputs.\n"
        " * Allows readers to run unedited book examples instantly from any static host.\n"
        " */\n"
        "window.GOOSE_PRECOMPUTED = "
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + ";\n"
    )
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write(js)
    size_kb = os.path.getsize(OUT_PATH) / 1024.0
    print(f"Wrote {OUT_PATH} ({len(entries)} blocks, {size_kb:.1f} KB, compiler: {version})")


if __name__ == "__main__":
    main()
