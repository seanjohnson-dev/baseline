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
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from . import brand, gitutil

OWNER_FILE = "owner.json"
SKIP_NM = {".vite", ".vitest", ".cache", ".vite-temp"}
LEGACY_MAX_AGE = 3600.0          # tmp dirs without an owner file count as stale only when older than this
# what our temp dirs may contain; anything else in there means "not ours / not sure" and it is left alone
KIND_CONTENTS = {"wt-": {OWNER_FILE, "no-hooks", "wt"}, "run-": {OWNER_FILE, "head", "base"}}


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


def proc_start(pid: int) -> Optional[str]:
    """Process start time as an opaque string (to tell a live owner from a reused pid); None if unknown."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return "jiffies:" + f.read().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        pass
    if os.name != "posix" or sys.platform.startswith("linux"):
        return None  # linux: /proc is authoritative (unreadable = gone); elsewhere ask ps
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return ("lstart:" + " ".join(out.stdout.split())) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def owner_alive(owner: dict) -> bool:
    try:
        pid = int(owner.get("pid", 0))
    except (TypeError, ValueError):
        return False
    if not _pid_alive(pid):
        return False
    want = owner.get("start")
    if not want:
        return True
    now = proc_start(pid)
    return now is None or now == want


def wt_marker() -> str:
    return brand.TMP_PREFIX + "wt-"


def tmp_root() -> Path:
    return Path(os.path.realpath(tempfile.gettempdir()))


def make_tmp_dir(kind: str, git_dir: Optional[Path] = None, repo: Optional[Path] = None) -> Path:
    """<tmp>/<prefix><kind>XXXX with an owner file (pid + process start time + repo git dir) so that a
    later run can tell whether the run that made it is still alive."""
    d = Path(tempfile.mkdtemp(prefix=brand.TMP_PREFIX + kind))
    owner = {"pid": os.getpid(), "start": proc_start(os.getpid()), "git_dir": str(git_dir or ""),
             "repo": str(repo or ""), "created": time.time()}
    (d / OWNER_FILE).write_text(json.dumps(owner))
    return d


@dataclass
class Stale:
    path: Path
    kind: str           # "wt-" | "run-"
    reason: str
    git_dir: str = ""   # repo git dir to run `git worktree remove/prune` in
    repo: str = ""


def _gitdir_of_worktree(wt: Path) -> Optional[str]:
    """wt/.git must be a file `gitdir: <repo git dir>/worktrees/<name>`; returns the repo git dir."""
    try:
        text = (wt / ".git").read_text().strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gd = Path(text[len("gitdir:"):].strip())
    if gd.parent.name != "worktrees":
        return None
    return str(gd.parent.parent)


def _cwd_users(path: Path) -> Optional[bool]:
    """Is any process's cwd inside path? None if that can't be determined on this system."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    prefix = str(path) + os.sep
    for p in proc.iterdir():
        if not p.name.isdigit():
            continue
        try:
            cwd = os.readlink(p / "cwd")
        except OSError:
            continue
        if cwd == str(path) or cwd.startswith(prefix):
            return True
    return False


def find_stale(deadline: Optional[float] = None, now: Optional[float] = None) -> List[Stale]:
    """Temp dirs of ours (<tmp>/<prefix>wt-*, <prefix>run-*) whose run is gone."""
    root = tmp_root()
    now = now or time.time()
    out: List[Stale] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return out
    for e in entries:
        if deadline is not None and time.time() > deadline:
            break
        kind = next((k for k in KIND_CONTENTS if e.name.startswith(brand.TMP_PREFIX + k)), None)
        if kind is None:
            continue
        try:
            if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                continue
            path = Path(e.path)
            if Path(os.path.realpath(path)).parent != root:
                continue
            names = set(os.listdir(path))
            if not names <= KIND_CONTENTS[kind]:
                continue  # something we didn't put there: not ours to delete
            owner = None
            if OWNER_FILE in names:
                try:
                    owner = json.loads((path / OWNER_FILE).read_text())
                except (OSError, ValueError):
                    owner = None
            if isinstance(owner, dict):
                if owner_alive(owner):
                    continue
                reason = f"its run (pid {owner.get('pid')}) is gone"
            else:
                age = now - path.stat().st_mtime
                if age < LEGACY_MAX_AGE:
                    continue
                if _cwd_users(path) is not False:
                    continue
                reason = f"no owner file, {age / 3600:.1f}h old, no process inside"
                owner = {}
            gd = owner.get("git_dir") or ""
            if kind == "wt-" and "wt" in names:
                real_gd = _gitdir_of_worktree(path / "wt")
                if real_gd is None and os.listdir(path / "wt"):
                    continue  # a non-empty wt/ that isn't a linked worktree: leave it alone
                gd = real_gd or gd
            out.append(Stale(path, kind, reason, gd, owner.get("repo") or ""))
        except OSError:
            continue
    return out


def remove_stale(s: Stale) -> bool:
    """`git worktree remove --force` + prune in the recorded repo, then delete the tmp dir (tmp only)."""
    if Path(os.path.realpath(s.path)).parent != tmp_root():
        return False
    wt = s.path / "wt"
    where = next((d for d in (s.repo, s.git_dir) if d and os.path.isdir(d)), None)
    if where is not None:
        if wt.exists():
            gitutil.try_git(["worktree", "remove", "--force", str(wt)], where)
        shutil.rmtree(s.path, ignore_errors=True)
        gitutil.try_git(["worktree", "prune"], where)
    else:
        shutil.rmtree(s.path, ignore_errors=True)
    return not s.path.exists()


def cleanup_stale(repo: Optional[Path] = None, budget: float = 3.0) -> List[str]:
    """Best effort, bounded, never raises: remove leftovers of killed runs; prune `repo`'s worktree list."""
    removed: List[str] = []
    deadline = time.time() + budget
    try:
        for s in find_stale(deadline=deadline):
            if time.time() > deadline:
                break
            if remove_stale(s):
                removed.append(str(s.path))
        if repo is not None:
            gitutil.try_git(["worktree", "prune"], repo)
    except Exception:  # never let housekeeping break a check
        pass
    return removed


def prune_stale(root: Path) -> List[str]:
    """Compatibility name for cleanup_stale(repo=root)."""
    return cleanup_stale(repo=root)


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
        self.tmp = make_tmp_dir("wt-", gitutil.common_git_dir(self.root), self.root)
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
