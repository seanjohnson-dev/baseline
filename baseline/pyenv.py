"""Detect an editable install (`pip install -e .`) of the repo under test.

With an editable install, `import pkg` from the base worktree may resolve to
the *real* tree (HEAD's code) through a .pth file / editable finder, and the
base run would silently test the wrong code. We find such installs and, for
both sides, prepend the side's own copy of the mapped source dirs to
PYTHONPATH (PYTHONPATH entries win over .pth paths and over the
meta-path finders setuptools installs for editable mode).
"""
from __future__ import annotations

import glob
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional, Set


def site_dirs(env: Optional[dict] = None) -> List[Path]:
    env = env or os.environ
    cands: Set[Path] = set()
    venv = env.get("VIRTUAL_ENV")
    prefixes = []
    if venv:
        prefixes.append(Path(venv))
    for name in ("python", "python3"):
        exe = shutil.which(name, path=env.get("PATH"))
        if exe:
            prefixes.append(Path(exe).resolve().parent.parent)
            prefixes.append(Path(exe).parent.parent)
    for pre in prefixes:
        for d in glob.glob(str(pre / "lib" / "python*" / "site-packages")) + \
                [str(pre / "Lib" / "site-packages")]:
            if os.path.isdir(d):
                cands.add(Path(d))
    return sorted(cands)


def editable_dirs(root: Path, env: Optional[dict] = None) -> List[str]:
    """Repo-relative directories that an editable install maps onto the real tree."""
    root_s = str(root.resolve())
    found: Set[str] = set()
    for sd in site_dirs(env):
        for f in list(sd.glob("*.pth")) + list(sd.glob("*.egg-link")) + list(sd.glob("__editable__*finder*.py")):
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if root_s not in text and str(root) not in text:
                continue
            for m in re.finditer(r"(?:^|['\"\s])(" + re.escape(root_s) + r"[^'\"\s,}]*)", text, re.M):
                p = Path(m.group(1))
                if f.suffix == ".py" and p.suffix == "" and (p / "__init__.py").exists():
                    p = p.parent  # finder mapping points at the package dir itself
                elif f.suffix == ".py" and p.suffix == ".py":
                    p = p.parent
                try:
                    rel = p.resolve().relative_to(root.resolve()).as_posix()
                except (ValueError, OSError):
                    continue
                found.add(rel or ".")
    return sorted(found)


def pythonpath_for(side_root: Path, real_root: Path, rel_dirs: List[str], existing: Optional[str]) -> str:
    """PYTHONPATH for one side: its own copy of the editable dirs first, then the user's
    PYTHONPATH with entries inside the real repo re-pointed at this side's tree."""
    parts = [str(side_root / d) if d != "." else str(side_root) for d in rel_dirs]
    for entry in (existing or "").split(os.pathsep):
        if not entry:
            continue
        try:
            rel = Path(entry).resolve().relative_to(real_root.resolve())
            entry = str(side_root / rel)
        except (ValueError, OSError):
            pass
        parts.append(entry)
    return os.pathsep.join(parts)
