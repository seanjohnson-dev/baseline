#!/usr/bin/env python3
"""Rename the product: scripts/rename.py <newname> [--root DIR]

Rewrites every occurrence of the current name (lower, Capitalised and UPPER
case) in all text files (incl. the Claude Code plugin's .claude-plugin/*.json,
hooks/hooks.json, skill and command), then renames every file/directory whose
name contains it: the package, bin/<name>, skills/<name>/, commands/<name>.md.
That includes the docs, README/CHANGELOG and bench/ (code, docs, result files
and the scorecard). The current name is read from <pkg>/brand.py. SPEC.md (the
input spec), .git and the git-ignored bench/_work and bench/rivals are left alone.
"""
import argparse
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv"}
# git-ignored benchmark scratch: generated fixture repos and third-party rival clones (bench/README.md)
SKIP_PATHS = {"bench/_work", "bench/rivals"}
SKIP_FILES = {"SPEC.md"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")


def find_package(root: Path) -> Path:
    for p in sorted(root.iterdir()):
        if p.is_dir() and (p / "brand.py").is_file() and (p / "__init__.py").is_file():
            return p
    raise SystemExit(f"rename: no package with brand.py under {root}")


def current_name(pkg: Path) -> str:
    m = re.search(r'^NAME\s*=\s*"([^"]+)"', (pkg / "brand.py").read_text(), re.M)
    if not m:
        raise SystemExit("rename: NAME not found in brand.py")
    return m.group(1)


def _keep_dirs(root: Path, dirpath: str, dirnames: list) -> list:
    rel = Path(dirpath).relative_to(root)
    return [d for d in dirnames if d not in SKIP_DIRS and (rel / d).as_posix() not in SKIP_PATHS]


def text_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = _keep_dirs(root, dirpath, dirnames)
        for f in filenames:
            if f in SKIP_FILES and Path(dirpath) == root:
                continue
            p = Path(dirpath) / f
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            try:
                yield p, data.decode("utf-8")
            except UnicodeDecodeError:
                continue


def rename(root: Path, new: str) -> dict:
    if not NAME_RE.match(new):
        raise SystemExit(f"rename: {new!r} must be a lowercase identifier (letters, digits, _)")
    pkg = find_package(root)
    old = current_name(pkg)
    if new == old:
        return {"old": old, "new": new, "files": 0}
    if (root / new).exists():
        raise SystemExit(f"rename: {root / new} already exists")
    pairs = [(old, new), (old.capitalize(), new.capitalize()), (old.upper(), new.upper())]
    changed = 0
    for path, text in text_files(root):
        out = text
        for a, b in pairs:
            out = out.replace(a, b)
        if out != text:
            path.write_text(out, encoding="utf-8")
            changed += 1
    # then every file/dir whose name holds the name: the package, bin/<name>, and the Claude Code plugin's
    # skills/<name>/ and commands/<name>.md (their manifests and hooks.json were rewritten above)
    todo = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = _keep_dirs(root, dirpath, dirnames)
        for name in filenames + dirnames:
            if old in name and not (name in SKIP_FILES and Path(dirpath) == root):
                todo.append(Path(dirpath) / name)
    renamed = []
    for src in sorted(todo, key=lambda p: len(p.parts), reverse=True):  # deepest first
        dst = src.with_name(src.name.replace(old, new))
        if dst.exists():
            raise SystemExit(f"rename: {dst} already exists")
        src.rename(dst)  # keeps the mode (the launcher stays executable)
        renamed.append(str(dst.relative_to(root)))
    return {"old": old, "new": new, "files": changed, "renamed": sorted(renamed)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("newname")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    a = ap.parse_args(argv)
    res = rename(Path(a.root).resolve(), a.newname)
    print(f"renamed {res['old']} -> {res['new']} ({res['files']} files rewritten; moved: "
          f"{', '.join(res.get('renamed', [])) or 'nothing'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
