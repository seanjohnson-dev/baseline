"""Temporary detached worktree for the base side.

`git worktree add --detach <tmp>/wt <sha>` outside the repo; hooks disabled;
node_modules mirrored (symlinks per package so workspace links point into the
worktree, not back at HEAD's code). Always removed (try/finally + signal
handlers raise into finally); stale ones from killed runs are pruned at start.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from . import brand, gitutil

OWNER_FILE = "owner.json"
SKIP_NM = {".vite", ".vitest", ".cache", ".vite-temp"}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wt_marker() -> str:
    return brand.TMP_PREFIX + "wt-"


def prune_stale(root: Path) -> List[str]:
    """Remove worktrees left behind by earlier runs that died (e.g. kill -9)."""
    removed = []
    try:
        paths = gitutil.worktree_list(root)
    except gitutil.GitError:
        return removed
    for p in paths:
        pp = Path(p)
        if wt_marker() not in pp.parent.name:
            continue
        owner = pp.parent / OWNER_FILE
        alive = False
        try:
            alive = _pid_alive(int(json.loads(owner.read_text()).get("pid", 0)))
        except (OSError, ValueError):
            alive = False
        if not alive:
            gitutil.try_git(["worktree", "remove", "--force", str(pp)], root)
            shutil.rmtree(pp.parent, ignore_errors=True)
            removed.append(p)
    gitutil.try_git(["worktree", "prune"], root)
    return removed


class BaseWorktree:
    def __init__(self, root: Path, sha: str, keep: bool = False, copy_ignored: Optional[List[str]] = None,
                 project_rel: str = "."):
        self.root = root
        self.sha = sha
        self.keep = keep
        self.copy_ignored = copy_ignored or []
        self.project_rel = project_rel
        self.tmp: Optional[Path] = None
        self.path: Optional[Path] = None
        self.notes: List[str] = []
        self.copied: List[str] = []

    def __enter__(self) -> "BaseWorktree":
        self.create()
        return self

    def __exit__(self, *exc) -> None:
        self.remove()

    def create(self) -> Path:
        self.tmp = Path(tempfile.mkdtemp(prefix=wt_marker()))
        (self.tmp / OWNER_FILE).write_text(json.dumps({"pid": os.getpid(), "repo": str(self.root),
                                                        "created": time.time()}))
        hooks = self.tmp / "no-hooks"
        hooks.mkdir()
        self.path = self.tmp / "wt"
        try:
            gitutil.git(["-c", f"core.hooksPath={hooks}", "worktree", "add", "--detach", "--quiet",
                         str(self.path), self.sha], self.root, timeout=600)
        except gitutil.GitError:
            shutil.rmtree(self.tmp, ignore_errors=True)
            gitutil.try_git(["worktree", "prune"], self.root)
            self.tmp = self.path = None
            raise
        self._link_node_modules()
        self._copy_ignored()
        return self.path

    def remove(self) -> None:
        if self.tmp is None:
            return
        if self.keep:
            self.notes.append(f"kept base worktree at {self.path} (remove with: git worktree remove --force "
                              f"{self.path})")
            return
        if self.path is not None:
            gitutil.try_git(["worktree", "remove", "--force", str(self.path)], self.root)
        shutil.rmtree(self.tmp, ignore_errors=True)
        gitutil.try_git(["worktree", "prune"], self.root)
        self.tmp = self.path = None

    # -- dependencies --------------------------------------------------------------------
    def _workspace_dirs(self) -> List[str]:
        dirs = {""}
        proj = "" if self.project_rel in (".", "") else self.project_rel
        dirs.add(proj)
        try:
            pj = json.loads((self.root / "package.json").read_text())
            ws = pj.get("workspaces") or []
            if isinstance(ws, dict):
                ws = ws.get("packages") or []
        except (OSError, ValueError):
            ws = []
        pnpm = self.root / "pnpm-workspace.yaml"
        if pnpm.exists():
            for line in pnpm.read_text(errors="replace").splitlines():
                line = line.strip()
                if line.startswith("- "):
                    ws.append(line[2:].strip().strip("'\""))
        for pattern in ws:
            if not isinstance(pattern, str) or pattern.startswith("!"):
                continue
            for d in glob.glob(str(self.root / pattern)):
                if os.path.isdir(d):
                    dirs.add(os.path.relpath(d, self.root))
        return sorted(dirs)

    def _link_node_modules(self) -> None:
        assert self.path is not None
        for rel in self._workspace_dirs():
            src = self.root / rel / "node_modules"
            dst_parent = self.path / rel
            if not src.is_dir() or not dst_parent.is_dir() or (dst_parent / "node_modules").exists():
                continue
            try:
                self._mirror(src, dst_parent / "node_modules")
            except OSError as e:
                self.notes.append(f"could not link {rel or '.'}/node_modules into the base worktree: {e}")

    def _mirror(self, src: Path, dst: Path) -> None:
        dst.mkdir()
        for entry in os.scandir(src):
            if entry.name in SKIP_NM:
                continue
            if entry.name.startswith("@") and entry.is_dir(follow_symlinks=False):
                (dst / entry.name).mkdir()
                for sub in os.scandir(entry.path):
                    self._link_entry(sub, dst / entry.name / sub.name)
            else:
                self._link_entry(entry, dst / entry.name)

    def _link_entry(self, entry: os.DirEntry, dst: Path) -> None:
        assert self.path is not None
        target = entry.path
        if entry.is_symlink():
            real = os.path.realpath(entry.path)
            try:
                rel = Path(real).relative_to(self.root)
                if "node_modules" not in rel.parts:
                    # workspace package linked into node_modules: point at the base worktree's copy
                    cand = self.path / rel
                    target = str(cand) if cand.exists() else real
                else:
                    target = real
            except ValueError:
                target = real
        os.symlink(target, dst, target_is_directory=os.path.isdir(target))

    def _copy_ignored(self) -> None:
        assert self.path is not None
        if not self.copy_ignored:
            return
        for pattern in self.copy_ignored:
            matches = glob.glob(str(self.root / pattern), recursive=True)
            for m in matches:
                rel = os.path.relpath(m, self.root)
                if rel.startswith("..") or rel.split(os.sep)[0] == ".git":
                    continue
                dst = self.path / rel
                if dst.exists():
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                if os.path.isdir(m):
                    shutil.copytree(m, dst, symlinks=True)
                else:
                    shutil.copy2(m, dst)
                self.copied.append(rel)


def missing_ignored(root: Path, wt: Path, limit: int = 8) -> List[str]:
    """Top-level ignored files present in the real tree but not in the worktree (hint only)."""
    out = gitutil.try_git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], root) or ""
    names = []
    for line in out.splitlines():
        line = line.rstrip("/")
        if "/" in line or line in ("node_modules", "__pycache__", ".pytest_cache", ".venv", "venv", "dist",
                                   "build", "target", ".mypy_cache", ".ruff_cache", ".tox", "coverage"):
            continue
        if fnmatch.fnmatch(line, "*.pyc") or (wt / line).exists():
            continue
        names.append(line)
    return names[:limit]
