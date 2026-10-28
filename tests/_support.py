"""Shared test helpers. The package is located via brand.py, so tests survive a rename."""
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data"


def _find_pkg() -> str:
    for p in sorted(ROOT.iterdir()):
        if (p / "brand.py").is_file() and (p / "__init__.py").is_file():
            return p.name
    raise RuntimeError("package with brand.py not found")


PKG = _find_pkg()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def mod(name: str):
    return importlib.import_module(f"{PKG}.{name}")


brand = mod("brand")
LAUNCHER = ROOT / "bin" / brand.CLI_NAME

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.invalid", GIT_COMMITTER_NAME="T",
               GIT_COMMITTER_EMAIL="t@example.invalid", GIT_CONFIG_NOSYSTEM="1")
GIT_ENV.pop("GIT_DIR", None)


def sh(args, cwd, check=True, env=None, input=None):
    return subprocess.run(args, cwd=str(cwd), env=env or GIT_ENV, check=check, capture_output=True, text=True,
                          input=input)


def write(base: Path, files: dict) -> None:
    for rel, body in files.items():
        p = Path(base) / rel
        if body is None:
            if p.exists():
                p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body).lstrip("\n"))


def git_repo(files: dict, branch: str = "main") -> Path:
    d = Path(tempfile.mkdtemp(prefix="t-repo-"))
    sh(["git", "init", "-q", "-b", branch], d)
    sh(["git", "config", "user.email", "t@example.invalid"], d)
    sh(["git", "config", "user.name", "T"], d)
    write(d, files)
    commit(d, "base")
    return d


def commit(d: Path, msg: str) -> None:
    sh(["git", "add", "-A"], d)
    sh(["git", "commit", "-q", "--allow-empty", "-m", msg], d)


def git_state(d: Path):
    st = sh(["git", "status", "--porcelain", "--untracked-files=all"], d).stdout
    wl = sh(["git", "worktree", "list", "--porcelain"], d).stdout
    stash = sh(["git", "stash", "list"], d).stdout
    branches = sh(["git", "for-each-ref", "--format=%(refname) %(objectname)"], d).stdout
    head = sh(["git", "rev-parse", "HEAD"], d).stdout
    return st, wl, stash, branches, head


def run_cli(args, cwd, env=None, timeout=300):
    e = dict(os.environ if env is None else env)
    e.pop("DATABASE_URL", None)
    return subprocess.run([sys.executable, str(LAUNCHER)] + list(args), cwd=str(cwd), env=e, capture_output=True,
                          text=True, timeout=timeout)


def have_pytest() -> bool:
    return subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode == 0


def rmtree(p) -> None:
    shutil.rmtree(p, ignore_errors=True)
