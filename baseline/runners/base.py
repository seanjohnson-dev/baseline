"""Runner adapter interface.

A runner knows how to (1) build the full-suite command with machine-readable
reporting injected, (2) build commands that rerun only some units, and
(3) parse what came out into a RunResult with repo-relative ids.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .. import envclass, normalize
from ..model import RunResult, Unit


@dataclass
class Side:
    """Where a run happens: the real working tree (head) or the base worktree."""
    name: str           # "head" | "base"
    root: Path          # repo root on this side
    cwd: Path           # directory the command runs in (root / project-relative path)
    outdir: Path        # where report files go (outside the repo)
    env: Dict[str, str] = field(default_factory=dict)
    other_roots: List[str] = field(default_factory=list)  # roots of the other side (for normalising)

    def roots(self) -> List[str]:
        return [str(self.root)] + list(self.other_roots)

    def rel(self, path: str) -> str:
        """Absolute or cwd-relative path -> repo-relative posix path."""
        p = Path(path)
        if not p.is_absolute():
            p = self.cwd / p
        try:
            return Path(os.path.normpath(str(p))).relative_to(Path(os.path.normpath(str(self.root)))).as_posix()
        except ValueError:
            try:
                return Path(os.path.realpath(str(p))).relative_to(Path(os.path.realpath(str(self.root)))).as_posix()
            except ValueError:
                return p.as_posix()

    def from_repo_rel(self, rel: str) -> str:
        """Repo-relative path -> path relative to this side's cwd (for command lines)."""
        return os.path.relpath(str(self.root / rel), str(self.cwd))

    def env_ctx(self) -> envclass.EnvContext:
        return envclass.EnvContext(str(self.root), env=self.env or os.environ)


@dataclass
class Invocation:
    argv: List[str]
    report: Optional[Path] = None
    mode: str = "full"                     # "full" | "subset"
    meta: Dict[str, object] = field(default_factory=dict)
    env: Dict[str, str] = field(default_factory=dict)


class Runner:
    name = "generic"
    per_test = True

    def __init__(self, argv: List[str], project_dir: Path):
        self.argv = list(argv)
        self.project_dir = project_dir

    # -- commands ------------------------------------------------------------
    def full(self, side: Side, tag: str) -> List[Invocation]:
        raise NotImplementedError

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        raise NotImplementedError

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        raise NotImplementedError

    def extra_env(self, side: Side) -> Dict[str, str]:
        return {}

    # -- helpers -------------------------------------------------------------
    def display_command(self) -> str:
        import shlex
        return " ".join(shlex.quote(a) for a in self.argv)

    def cache_key(self) -> str:
        return self.name + "\0" + "\0".join(self.argv)

    @staticmethod
    def make_key(exc_type: str, message: str, side: Side) -> str:
        msg = normalize.normalize(message, side.roots())
        if exc_type and not msg.startswith(exc_type):
            return f"{exc_type}: {msg}" if msg else exc_type
        return msg

    @staticmethod
    def classify(key: str, raw: str, side: Side):
        return envclass.classify(key, raw, side.env_ctx())

    def source_contains(self, text: str, unit: Unit) -> bool:
        """Does a base-side source text mention this test (used for 'absent on base')?"""
        name = unit.name or unit.id
        return bool(name) and name in text


def crash_result(proc, problem: str, inv: Invocation) -> RunResult:
    r = RunResult(ok=False, problem=problem, exit_code=proc.returncode, duration=proc.duration,
                  output_tail=proc.output[-4000:])
    r.invocations.append(inv.argv)
    return r


def split_positional(args: List[str], value_opts: set, short_value_opts: set, exists) -> (List[str], List[str]):
    """Split runner args into (options, positionals).

    A positional is a token not starting with '-' that is not the value of a
    preceding option and that looks like a path/test selector.
    """
    opts: List[str] = []
    pos: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            pos.extend(args[i + 1:])
            break
        if a.startswith("-") and len(a) > 1:
            opts.append(a)
            needs_value = ("=" not in a) and (a in value_opts or a in short_value_opts)
            if needs_value and i + 1 < len(args):
                opts.append(args[i + 1])
                i += 2
                continue
            i += 1
            continue
        if exists(a):
            pos.append(a)
        else:
            opts.append(a)
        i += 1
    return opts, pos
