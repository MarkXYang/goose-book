#!/usr/bin/env python3
"""Verify every ```goose code block in the book against the Goose compiler.

Usage:
    python3 scripts/check_examples.py [paths...] [--run] [--build] [--exe goose] [-v]

Defaults to checking all Markdown files under src/.

Markers (HTML comment on the line immediately before a ```goose fence):
    <!-- fragment -->      Skip block (syntax snippet, not meant to compile).
    <!-- check-only -->    Typecheck with `goose check`, do not run.
    <!-- build -->         Typecheck; with --run --build, compile natively via
                           `goose build` and run (needed for thread_fn examples).
    <!-- compile-fail -->  Block must fail to compile in every wrapper form.
                           A live line containing `// error` also marks compile-fail.

A bare ``` block immediately after a complete `fn main()` program is treated as
its expected stdout when --run is enabled. Compiler warnings fail a block.
"""
import argparse
import concurrent.futures
import hashlib
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def collect_markdown_files(paths):
    if not paths:
        paths = [os.path.join(ROOT, "src")]
    files = []
    for p in paths:
        full = os.path.abspath(p)
        if os.path.isdir(full):
            for dirpath, dirnames, filenames in os.walk(full):
                dirnames.sort()
                for name in sorted(filenames):
                    if name.endswith(".md"):
                        files.append(os.path.join(dirpath, name))
        elif os.path.isfile(full):
            files.append(full)
        else:
            raise SystemExit(f"path not found: {p}")
    return files


def read_blocks(path):
    """Return list of (path, first_line_no, body_lines, expected_lines_or_None, marker)."""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")
    blocks = []
    i = 0
    while i < len(lines):
        m = re.match(r"^```(\w*)", lines[i])
        if not (m and m.group(1) == "goose"):
            i += 1
            continue
        marker = ""
        if i > 0:
            mm = re.match(r"^<!--\s*(fragment|check-only|build|compile-fail)\s*-->", lines[i - 1].strip())
            if mm:
                marker = mm.group(1)
        start = i + 1
        body = []
        i += 1
        while i < len(lines) and not lines[i].startswith("```"):
            body.append(lines[i])
            i += 1
        i += 1

        expected = None
        j = i
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j < len(lines) and lines[j].strip() == "```":
            expected = []
            j += 1
            while j < len(lines) and not lines[j].startswith("```"):
                expected.append(lines[j])
                j += 1

        blocks.append((path, start, body, expected, marker))
    return blocks


class Checker:
    def __init__(self, goose, out_dir, run, build, verbose):
        self.goose = goose
        self.out_dir = out_dir
        self.run = run
        self.build = build
        self.verbose = verbose

    def _stem(self, md_path, start, label):
        rel = os.path.relpath(md_path, ROOT).replace(os.sep, "/")
        h = hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]
        base = os.path.splitext(os.path.basename(md_path))[0]
        return f"{base}_{h}_L{start:04d}_{label}"

    def compile_text(self, text, stem):
        path = os.path.join(self.out_dir, stem + ".goose")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        p = subprocess.run(
            [self.goose, "check", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        msg = "\n".join(
            l for l in (p.stdout + p.stderr).split("\n")
            if not l.startswith(("parsed ok", "typechecked ok", "bce:"))
        )
        return p.returncode, msg.strip(), path

    def check(self, block):
        md_path, start, body, expected, marker = block
        rel = os.path.relpath(md_path, ROOT).replace(os.sep, "/")
        loc = f"{rel}:{start}"
        text = "\n".join(body) + "\n"
        expect_error = marker == "compile-fail" or any(
            not l.lstrip().startswith("//") and re.search(r"//\s*error", l)
            for l in body
        )
        has_main = re.search(r"^\s*fn\s+(?:::)?main\s*\(", text, re.M) is not None

        forms = [("as-is", text), ("std", "import std;\n" + text)]
        if not has_main:
            forms += [
                ("main-wrap", "import std;\nfn main() {\n" + text + "}\n"),
                ("main-after", "import std;\n" + text + "\nfn main() {}\n"),
            ]

        lines = []
        results = []
        for label, t in forms:
            stem = self._stem(md_path, start, label)
            rc, msg, path = self.compile_text(t, stem)
            results.append((label, rc, msg, path))
            if rc == 0 and not expect_error:
                break

        ok_forms = [r for r in results if r[1] == 0]
        if expect_error:
            if ok_forms:
                lines.append(f"FAIL {loc}: expected compile error, but compiled as {ok_forms[0][0]}")
                return lines, 1, False
            if self.verbose:
                best = next((r for r in results if "error" in r[2]), results[-1])
                first = [l for l in best[2].split("\n") if "error" in l or "warning" in l][:2]
                lines.append(f"ok   {loc} (expected error, {best[0]}): {' | '.join(first)}")
            return lines, 0, False

        if not ok_forms:
            lines.append(f"FAIL {loc}: no form compiles")
            for label, rc, msg, path in results:
                first = [l for l in msg.split("\n") if l.strip()][:3]
                lines.append(f"   [{label}] {' | '.join(first)}")
            return lines, 1, False

        label, rc, msg, path = ok_forms[0]
        warn = [l for l in msg.split("\n") if "warning" in l]
        if warn:
            lines.append(f"FAIL {loc} ({label}): compiler warning: {' | '.join(warn[:2])}")
            return lines, 1, False

        if self.run and label in ("as-is", "std") and has_main and marker != "check-only":
            if marker == "build":
                if not self.build:
                    if self.verbose:
                        lines.append(f"ok   {loc} ({label}, checked; pass --build to run native)")
                    return lines, 0, False
                exe_path = os.path.splitext(path)[0] + (".exe" if sys.platform == "win32" else "")
                bp = subprocess.run(
                    [self.goose, "build", "-o", exe_path, path],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300,
                )
                if bp.returncode != 0:
                    tail = (bp.stderr or bp.stdout).strip().split("\n")[-3:]
                    lines.append(f"FAIL {loc}: native build failed: {' | '.join(tail)}")
                    return lines, 1, False
                cmd = [exe_path]
                mode = "built+ran"
            else:
                cmd = [self.goose, path]
                mode = "ran"

            p = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
            )
            if p.returncode != 0:
                tail = p.stderr.strip().split("\n")[-1] if p.stderr.strip() else p.stdout.strip()[-200:]
                lines.append(f"FAIL {loc}: program exited {p.returncode}: {tail}")
                return lines, 1, True
            if expected is not None:
                got = p.stdout.replace("\r\n", "\n").rstrip("\n").split("\n")
                exp = "\n".join(expected).rstrip("\n").split("\n")
                if got != exp:
                    lines.append(f"FAIL {loc}: output differs from expected block")
                    lines.append("   expected: " + " / ".join(exp)[:400])
                    lines.append("   got:      " + " / ".join(got)[:400])
                    return lines, 1, True
                if self.verbose:
                    lines.append(f"ok   {loc} ({label}, {mode}, output matched)")
            elif self.verbose:
                lines.append(f"ok   {loc} ({label}, {mode}):")
                lines += ["   " + l for l in p.stdout.rstrip().split("\n")]
            return lines, 0, True

        if self.verbose:
            lines.append(f"ok   {loc} ({label})")
        return lines, 0, False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="markdown files or directories (default: src/)")
    ap.add_argument("--exe", help="path to goose compiler (default: PATH or ../goose/build/)")
    ap.add_argument("--run", action="store_true", help="also run complete programs and verify output")
    ap.add_argument("--build", action="store_true", help="with --run, also build and run <!-- build --> examples")
    ap.add_argument("--out-dir", help="scratch directory (default: build/examples)")
    ap.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 1, 16), help="parallel worker count")
    ap.add_argument("--line", type=int, help="check only the block starting at this line")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every checked block")
    a = ap.parse_args()

    goose = os.path.abspath(a.exe) if a.exe else find_compiler()
    out_dir = os.path.abspath(a.out_dir) if a.out_dir else os.path.join(ROOT, "build", "examples")
    os.makedirs(out_dir, exist_ok=True)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    files = collect_markdown_files(a.paths)
    all_blocks = []
    for f in files:
        all_blocks.extend(read_blocks(f))
    if a.line is not None:
        all_blocks = [b for b in all_blocks if b[1] == a.line]

    fragments = [b for b in all_blocks if b[4] == "fragment"]
    todo = [b for b in all_blocks if b[4] != "fragment"]

    checker = Checker(goose, out_dir, a.run, a.build, a.verbose)
    if a.jobs > 1 and len(todo) > 1:
        with concurrent.futures.ThreadPoolExecutor(min(a.jobs, len(todo))) as pool:
            reports = list(pool.map(checker.check, todo))
    else:
        reports = [checker.check(b) for b in todo]

    failures = 0
    ran = 0
    for lines, failed, did_run in reports:
        failures += failed
        if did_run:
            ran += 1
        for line in lines:
            print(line)

    run_note = f", {ran} run" if a.run else ""
    print(
        f"{len(files)} files: {len(all_blocks)} blocks, {len(todo)} checked{run_note}, "
        f"{len(fragments)} fragments skipped, {failures} failures"
    )
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
