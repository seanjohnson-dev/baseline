"""Does the change edit the dependencies the base run shares with head? (see docs/VERDICTS.md)

The base worktree has the base commit's files but not its installed packages: it links your
`node_modules` and runs in your active Python (and Ruby) environment. So when the change edits a
dependency manifest or lockfile, base runs with the new dependencies, and a failure that happens on
both sides may come from the dependency change itself. Such failures must not be called pre-existing.

Matched by file name at any depth (monorepos, nested packages), anywhere in the repo, whatever the
test runner: a manifest the tests seem not to use still counts (safe over clever).

Not counted, because each worktree builds from its own copy (checked with real runs, see the docs):
go.mod / go.sum / go.work (the module cache is keyed by version) and Cargo.toml / Cargo.lock (each
worktree resolves and builds its own dependencies, also with a shared CARGO_TARGET_DIR).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, List, Optional

from . import gitutil

NAMES = {
    # JavaScript: node_modules is linked into the base worktree
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "pnpm-workspace.yaml", "bun.lockb", "bun.lock",
    # Python: the active environment is used for both sides
    "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock", "uv.lock", "pdm.lock", "setup.py", "setup.cfg",
    # Ruby: installed gems are shared; Gemfile.lock picks the versions bundler loads
    "Gemfile", "Gemfile.lock", "gems.rb", "gems.locked",
}
# package.json keys that can't change what a package manager installs; an edit to only these doesn't count.
# Not "version" or "name": in a workspace they decide whether a sibling package is linked or installed.
PACKAGE_JSON_SAFE_KEYS = {"scripts", "description", "keywords", "author", "contributors", "license", "homepage",
                          "repository", "bugs", "funding"}
_MISSING = object()


def is_manifest(path: str) -> bool:
    """A dependency manifest or lockfile, by name: the NAMES above, `*requirements*.txt|.in`,
    `*constraints*.txt`, any `.txt`/`.in` in a `requirements/` directory, `*.gemspec`."""
    parts = path.replace(os.sep, "/").split("/")
    name = parts[-1]
    if name in NAMES:
        return True
    low = name.lower()
    if low.endswith((".txt", ".in")) and ("requirements" in low or (len(parts) > 1 and parts[-2] == "requirements")):
        return True
    if low.endswith(".txt") and "constraints" in low:
        return True
    return low.endswith(".gemspec")


def package_json_deps_differ(old: Optional[str], new: Optional[str]) -> bool:
    """False only when both versions parse and differ in PACKAGE_JSON_SAFE_KEYS alone."""
    if old is None or new is None:
        return True  # added or deleted
    try:
        a, b = json.loads(old), json.loads(new)
    except ValueError:
        return True
    if not isinstance(a, dict) or not isinstance(b, dict):
        return True
    return any(a.get(k, _MISSING) != b.get(k, _MISSING) for k in (set(a) | set(b)) - PACKAGE_JSON_SAFE_KEYS)


def changed_manifests(root: Path, base_sha: str, changed: Iterable[str]) -> List[str]:
    """The dependency files among `changed` (gitutil.changed_files: base vs the working tree, staged,
    unstaged and untracked), repo-relative and sorted."""
    out = []
    for p in sorted(set(changed)):
        if not is_manifest(p):
            continue
        if os.path.basename(p) == "package.json" and not package_json_deps_differ(gitutil.show(root, base_sha, p),
                                                                                  _read(root / p)):
            continue
        out.append(p)
    return out


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def describe(files: List[str], limit: int = 4) -> str:
    """`package.json, package-lock.json` (+N more)."""
    shown = ", ".join(files[:limit])
    return shown + (f" (+{len(files) - limit} more)" if len(files) > limit else "")
