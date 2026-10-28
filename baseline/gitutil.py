"""Read-only git helpers (plus worktree add/remove, see worktree.py).

Nothing here touches the user's working tree, index, stash or branches:
GIT_OPTIONAL_LOCKS=0 stops `git status`/`git diff` from refreshing the index.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class GitError(Exception):
    pass


def git_env() -> dict:
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env.pop("GIT_INDEX_FILE", None)
    return env


def git(args: List[str], cwd, check: bool = True, timeout: float = 120) -> str:
    try:
        p = subprocess.run(["git"] + args, cwd=str(cwd), env=git_env(), stdin=subprocess.DEVNULL,
                           capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise GitError("git is not installed")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} timed out")
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.decode('utf-8', 'replace').strip()}")
    return p.stdout.decode("utf-8", "replace")


def try_git(args: List[str], cwd) -> Optional[str]:
    try:
        return git(args, cwd).strip()
    except GitError:
        return None


def repo_root(cwd) -> Path:
    out = try_git(["rev-parse", "--show-toplevel"], cwd)
    if not out:
        raise GitError(f"not inside a git repository: {cwd}")
    return Path(out).resolve()


def git_dir(root: Path) -> Path:
    """Per-worktree git dir (where last.json lives)."""
    out = git(["rev-parse", "--absolute-git-dir"], root).strip()
    return Path(out)


def common_git_dir(root: Path) -> Path:
    out = git(["rev-parse", "--git-common-dir"], root).strip()
    p = Path(out)
    if not p.is_absolute():
        p = (root / p)
    return p.resolve()


def rev_parse(root: Path, ref: str) -> Optional[str]:
    return try_git(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], root)


def head_sha(root: Path) -> Optional[str]:
    return rev_parse(root, "HEAD")


def current_branch(root: Path) -> Optional[str]:
    out = try_git(["symbolic-ref", "--quiet", "--short", "HEAD"], root)
    return out or None


@dataclass
class DefaultBranch:
    ref: str   # e.g. origin/main or main
    name: str  # e.g. main


def default_branch(root: Path) -> Optional[DefaultBranch]:
    sym = try_git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], root)
    if sym and sym.startswith("refs/remotes/"):
        ref = sym[len("refs/remotes/"):]
        if rev_parse(root, ref):
            return DefaultBranch(ref, ref.split("/", 1)[1])
    for name in ("main", "master"):
        if rev_parse(root, "refs/heads/" + name):
            return DefaultBranch(name, name)
    for name in ("main", "master"):
        if rev_parse(root, "refs/remotes/origin/" + name):
            return DefaultBranch("origin/" + name, name)
    return None


def merge_base(root: Path, a: str, b: str) -> Optional[str]:
    return try_git(["merge-base", a, b], root)


def is_ancestor(root: Path, a: str, b: str) -> bool:
    p = subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=str(root), env=git_env(),
                       capture_output=True)
    return p.returncode == 0


@dataclass
class BaseInfo:
    sha: str
    ref: str     # what the user would recognise ("main", "HEAD", the --base value)
    how: str     # one-line explanation of how it was chosen
    warning: str = ""


def resolve_base(root: Path, base_opt: Optional[str]) -> BaseInfo:
    head = head_sha(root)
    if head is None:
        raise GitError("the repository has no commits yet, so there is no base to compare against")
    if base_opt:
        sha = rev_parse(root, base_opt)
        if not sha:
            raise GitError(f"--base {base_opt!r} does not name a commit")
        warning = ""
        if not is_ancestor(root, sha, head):
            warning = (f"base {base_opt} is not an ancestor of HEAD; differences may include commits "
                       f"that are not part of this change (try --base $(git merge-base HEAD {base_opt}))")
        return BaseInfo(sha, base_opt, f"--base {base_opt}", warning)
    dflt = default_branch(root)
    if dflt is None:
        return BaseInfo(head, "HEAD", "no default branch (origin/HEAD, main, master) found; comparing "
                        "the working tree against HEAD")
    cur = current_branch(root)
    if cur == dflt.name:
        return BaseInfo(head, "HEAD", f"on the default branch {cur}; comparing the working tree against HEAD")
    mb = merge_base(root, "HEAD", dflt.ref)
    if not mb:
        return BaseInfo(head, "HEAD", f"HEAD shares no history with {dflt.ref}; comparing against HEAD")
    if mb == head:
        return BaseInfo(head, "HEAD", f"HEAD has not diverged from {dflt.ref}; comparing the working "
                        "tree against HEAD")
    return BaseInfo(mb, dflt.ref, f"merge-base of HEAD and {dflt.ref}")


def is_dirty(root: Path) -> bool:
    out = git(["status", "--porcelain", "--untracked-files=normal"], root)
    return bool(out.strip())


def status_porcelain(root: Path) -> str:
    return git(["status", "--porcelain", "--untracked-files=all", "--ignored=no"], root)


def changed_files(root: Path, base_sha: str) -> List[str]:
    """Files that differ between base and the working tree (incl. untracked, deletions)."""
    out = git(["diff", "--name-only", "--no-renames", "-z", base_sha, "--"], root)
    files = [f for f in out.split("\0") if f]
    untracked = git(["ls-files", "--others", "--exclude-standard", "-z"], root)
    files += [f for f in untracked.split("\0") if f]
    return sorted(set(files))


def exists_at(root: Path, sha: str, path: str) -> bool:
    p = subprocess.run(["git", "cat-file", "-e", f"{sha}:{path}"], cwd=str(root), env=git_env(),
                       capture_output=True)
    return p.returncode == 0


def show(root: Path, sha: str, path: str) -> Optional[str]:
    p = subprocess.run(["git", "show", f"{sha}:{path}"], cwd=str(root), env=git_env(), capture_output=True)
    if p.returncode != 0:
        return None
    return p.stdout.decode("utf-8", "replace")


def ls_tree(root: Path, sha: str, path: str) -> List[str]:
    out = try_git(["ls-tree", "--name-only", f"{sha}", path.rstrip("/") + "/"], root)
    return out.splitlines() if out else []


def worktree_list(root: Path) -> List[str]:
    out = git(["worktree", "list", "--porcelain"], root)
    return [line[len("worktree "):] for line in out.splitlines() if line.startswith("worktree ")]


def short(sha: str) -> str:
    return sha[:7]
