"""Static "could the change have touched this test?" analysis.

Used ONLY to decide how much evidence to collect (a test that fails on head
although nothing it imports changed is suspicious and gets extra reruns).
It never decides a verdict, so imprecision costs time, not correctness.

Returns True (something the test depends on changed, or a global file such as
a config/lockfile changed), False (provably untouched), None (can't tell).
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Iterable, List, Optional, Set

DOC_RE = re.compile(r"(\.(md|rst|adoc|txt)$|^docs?/|(^|/)(LICENSE|CHANGELOG|AUTHORS|NOTICE)[^/]*$|"
                    r"(^|/)\.gitignore$|(^|/)\.github/)", re.I)
PY_SRC = re.compile(r"\.py$")
JS_SRC = re.compile(r"\.(m|c)?(j|t)sx?$")
JS_CONFIG = re.compile(r"(^|/)(vite|vitest|jest|babel|webpack|rollup|esbuild|swc|tsup)[.\w-]*\.(m|c)?[jt]s$|"
                       r"(^|/)(setupTests|test-setup|vitest\.setup|jest\.setup)\.[mc]?[jt]sx?$")
GO_SRC = re.compile(r"\.go$")


def _global(changed: Iterable[str], is_src) -> bool:
    for f in changed:
        if DOC_RE.search(f):
            continue
        if not is_src(f):
            return True
    return False


# ------------------------------------------------------------------------------ python
def python_closure(root: Path, test_file: str, changed: Set[str]) -> Optional[Set[str]]:
    roots = [root, root / "src"]
    d = (root / test_file).parent
    while True:  # pytest "prepend" import mode puts the first dir without __init__.py on sys.path
        if not (d / "__init__.py").exists():
            roots.append(d)
            break
        if d == root or d.parent == d:
            roots.append(d)
            break
        d = d.parent
    seen: Set[str] = set()
    queue: List[str] = [test_file]
    pkg_dir = (root / test_file).parent
    while (pkg_dir / "__init__.py").exists() and pkg_dir != root:
        queue.append(os.path.relpath(pkg_dir / "__init__.py", root).replace(os.sep, "/"))
        pkg_dir = pkg_dir.parent
    # conftest.py files of the test's directory chain are loaded by pytest
    cur = (root / test_file).parent
    while True:
        rel = os.path.relpath(cur / "conftest.py", root).replace(os.sep, "/")
        if (cur / "conftest.py").exists() or rel in changed:
            queue.append(rel)
        if cur == root or cur.parent == cur:
            break
        cur = cur.parent

    def exists(rel: str) -> bool:
        return rel in changed or (root / rel).is_file()

    def resolve(mod: str) -> List[str]:
        out = []
        parts = mod.split(".")
        for base in roots:
            try:
                brel = os.path.relpath(base, root).replace(os.sep, "/")
            except ValueError:
                continue
            brel = "" if brel == "." else brel + "/"
            for i in range(1, len(parts) + 1):
                p = brel + "/".join(parts[:i])
                for cand in (p + ".py", p + "/__init__.py"):
                    if exists(cand):
                        out.append(cand)
        return out

    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        path = root / f
        if not path.is_file():
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except (SyntaxError, ValueError, OSError):
            return None
        if re.search(r"\bimportlib\b|__import__\s*\(|pytest_plugins|runpy", src):
            return None
        pkg = os.path.dirname(f).replace(os.sep, "/")
        for node in ast.walk(tree):
            mods: List[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = pkg.split("/") if pkg else []
                    if node.level > 1:
                        base_parts = base_parts[: len(base_parts) - (node.level - 1)]
                    prefix = "/".join(base_parts)
                    names = [node.module] if node.module else []
                    names += [(node.module + "." if node.module else "") + a.name for a in node.names]
                    for n in names:
                        p = (prefix + "/" if prefix else "") + n.replace(".", "/")
                        for cand in (p + ".py", p + "/__init__.py"):
                            if exists(cand):
                                queue.append(cand)
                    if prefix and exists(prefix + "/__init__.py"):
                        queue.append(prefix + "/__init__.py")
                    continue
                if node.module:
                    mods = [node.module] + [node.module + "." + a.name for a in node.names]
            for m in mods:
                queue.extend(resolve(m))
    return seen


def python_changed(root: Path, test_file: str, changed: Set[str]) -> Optional[bool]:
    if _global(changed, lambda f: PY_SRC.search(f) and not f.endswith("conftest.py")):
        return True
    clo = python_closure(root, test_file, changed)
    if clo is None:
        return None
    return bool(clo & changed)


# ------------------------------------------------------------------------------ js / ts
JS_IMPORT_RES = [
    re.compile(r"""(?:import|export)\s[^'";]*?\bfrom\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bimport\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bvi\.mock\s*\(\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bjest\.mock\s*\(\s*['"]([^'"]+)['"]"""),
]
JS_DYNAMIC = re.compile(r"""\b(?:import|require)\s*\(\s*[^'"\s)]""")
JS_EXTS = ["", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts", ".json", ".vue", ".svelte"]
NODE_BUILTINS = {"assert", "buffer", "child_process", "crypto", "events", "fs", "http", "https", "net", "os",
                 "path", "process", "stream", "timers", "url", "util", "zlib", "worker_threads", "dns", "tty"}


def js_closure(root: Path, project: Path, test_file: str, changed: Set[str]) -> Optional[Set[str]]:
    seen: Set[str] = set()
    queue = [test_file]
    for cfg in list(project.glob("vitest.config.*")) + list(project.glob("vite.config.*")) + \
            list(project.glob("jest.config.*")):
        rel = os.path.relpath(cfg, root).replace(os.sep, "/")
        queue.append(rel)

    def exists(rel: str) -> bool:
        return rel in changed or (root / rel).is_file()

    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        path = root / f
        if not path.is_file() or not JS_SRC.search(f):
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        if JS_DYNAMIC.search(src):
            return None
        is_config = JS_CONFIG.search(f) is not None
        specs: List[str] = []
        for rx in JS_IMPORT_RES:
            specs += rx.findall(src)
        if is_config:  # setupFiles etc. referenced as string literals
            specs += [s for s in re.findall(r"""['"](\.{1,2}/[^'"]+)['"]""", src)]
        here = os.path.dirname(f)
        for spec in specs:
            if spec.startswith("."):
                base = os.path.normpath(os.path.join(here, spec)).replace(os.sep, "/")
                cands = [base + e for e in JS_EXTS] + [base + "/index" + e for e in JS_EXTS[1:]]
                if re.search(r"\.(m|c)?js$", base):
                    stem = re.sub(r"\.(m|c)?js$", "", base)
                    cands += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
                hit = [c for c in cands if exists(c)]
                if not hit and not is_config:
                    continue  # unresolvable relative import: let the runner complain
                queue.extend(hit)
            elif spec.startswith("node:") or spec.split("/")[0] in NODE_BUILTINS:
                continue
            else:
                pkg = "/".join(spec.split("/")[:2]) if spec.startswith("@") else spec.split("/")[0]
                if not (project / "node_modules" / pkg).exists() and not (root / "node_modules" / pkg).exists():
                    return None  # alias or workspace package: can't tell
    return seen


def js_changed(root: Path, project: Path, test_file: str, changed: Set[str]) -> Optional[bool]:
    if _global(changed, lambda f: JS_SRC.search(f) and not JS_CONFIG.search(f)):
        return True
    clo = js_closure(root, project, test_file, changed)
    if clo is None:
        return None
    return bool(clo & changed)


# ------------------------------------------------------------------------------ go
GO_IMPORT_BLOCK = re.compile(r"^import\s*\((.*?)\)", re.S | re.M)
GO_IMPORT_ONE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
GO_QUOTED = re.compile(r'"([^"]+)"')


def go_changed(root: Path, module: str, module_dir: Path, pkg: str, changed: Set[str]) -> Optional[bool]:
    if not module or not (pkg == module or pkg.startswith(module + "/")):
        return None
    mod_rel = os.path.relpath(module_dir, root).replace(os.sep, "/")
    mod_rel = "" if mod_rel == "." else mod_rel + "/"
    start = pkg[len(module):].lstrip("/")
    dirs: Set[str] = set()
    queue = [start]
    while queue:
        d = queue.pop()
        if d in dirs:
            continue
        dirs.add(d)
        ddir = module_dir / d if d else module_dir
        if not ddir.is_dir():
            continue
        for gf in ddir.glob("*.go"):
            try:
                src = gf.read_text(errors="replace")
            except OSError:
                return None
            imps = GO_IMPORT_ONE.findall(src)
            for blk in GO_IMPORT_BLOCK.findall(src):
                imps += GO_QUOTED.findall(blk)
            for imp in imps:
                if imp == module or imp.startswith(module + "/"):
                    queue.append(imp[len(module):].lstrip("/"))
    rel_dirs = {(mod_rel + d).rstrip("/") if d else mod_rel.rstrip("/") for d in dirs}
    for f in changed:
        if DOC_RE.search(f):
            continue
        fdir = os.path.dirname(f)
        if GO_SRC.search(f):
            if fdir in rel_dirs:
                return True
            continue
        # non-Go file: inside a closure package (testdata/embed) or anywhere else -> treat as global
        return True
    return False
