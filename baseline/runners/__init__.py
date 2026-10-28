"""Runner detection: from a user-supplied command, or from the repository layout."""
from __future__ import annotations

import configparser
import json
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .base import Invocation, Runner, Side  # noqa: F401
from .gotest import GoRunner
from .jsjson import JestRunner, VitestRunner, find_token
from .nodetest import NodeTestRunner, detect_node_test, is_node_test
from .checks import CheckRunner, check_tool
from .others import CargoRunner, GenericRunner, RspecRunner
from .pytest import PytestRunner, find_pytest

RUNNERS = {r.name: r for r in (PytestRunner, VitestRunner, JestRunner, GoRunner, CargoRunner, RspecRunner,
                               NodeTestRunner, GenericRunner)}
SHELL_META = re.compile(r"[|&;<>()$`]")


@dataclass
class Detection:
    runner: str
    argv: List[str]
    source: str          # "command" | "auto: <reason>"
    note: str = ""


def _python() -> str:
    for name in ("python", "python3"):
        if shutil.which(name):
            return name
    return sys.executable


def runner_for_argv(argv: List[str]) -> str:
    if find_pytest(argv) is not None:
        return "pytest"
    if find_token(argv, ["vitest"]) is not None:
        return "vitest"
    if find_token(argv, ["jest"]) is not None:
        return "jest"
    if len(argv) >= 2 and os.path.basename(argv[0]) in ("go", "go.exe") and argv[1] == "test":
        return "go"
    if len(argv) >= 2 and os.path.basename(argv[0]) in ("cargo", "cargo.exe") and argv[1] == "test":
        return "cargo"
    if any(os.path.basename(a) == "rspec" for a in argv[:3]):
        return "rspec"
    if is_node_test(argv):
        return "node-test"
    tool = check_tool(argv)
    if tool:
        return tool
    return "generic"


def _npm_script(argv: List[str], project: Path) -> Optional[List[str]]:
    """`npm test` / `npm run test` / `yarn test` / `pnpm test` -> the script's own command when simple."""
    if not argv or os.path.basename(argv[0]) not in ("npm", "yarn", "pnpm", "bun"):
        return None
    rest = argv[1:]
    if rest and rest[0] in ("run", "run-script"):
        rest = rest[1:]
    if not rest:
        return None
    script, extra = rest[0], rest[1:]
    if extra and extra[0] == "--":
        extra = extra[1:]
    try:
        scripts = json.loads((project / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError):
        return None
    body = scripts.get(script)
    if not body or SHELL_META.search(body) or "=" in body.split()[0]:
        return None
    cmd = shlex.split(body)
    local = project / "node_modules" / ".bin" / cmd[0]
    if local.exists():
        cmd[0] = str(Path("node_modules") / ".bin" / cmd[0])
    return cmd + extra


def missing_npm_script(argv: List[str], project: Path) -> Optional[str]:
    """`npm test` / `npm run X` / `pnpm X` / `yarn run X` whose script package.json doesn't define -> X."""
    if len(argv) < 2 or os.path.basename(argv[0]) not in ("npm", "pnpm", "yarn", "bun"):
        return None
    tool, rest = os.path.basename(argv[0]), argv[1:]
    if rest[0] in ("run", "run-script"):
        if len(rest) < 2 or rest[1].startswith("-"):
            return None
        name = rest[1]
    elif rest[0] in ("test", "t", "tst") and tool in ("npm", "pnpm", "yarn"):
        name = "test"
    else:
        return None
    try:
        scripts = json.loads((project / "package.json").read_text()).get("scripts") or {}
    except (OSError, ValueError):
        return None
    return None if name in scripts else name


def from_command(argv: List[str], project: Path) -> Detection:
    if len(argv) == 1 and " " in argv[0]:
        argv = shlex.split(argv[0])
    resolved = _npm_script(argv, project)
    if resolved and runner_for_argv(resolved) != "generic":
        return Detection(runner_for_argv(resolved), resolved, "command",
                         f"resolved `{' '.join(argv)}` to its package.json script")
    return Detection(runner_for_argv(argv), argv, "command")


def _pytest_configured(project: Path) -> Optional[str]:
    if (project / "pytest.ini").exists():
        return "pytest.ini"
    if (project / "conftest.py").exists():
        return "conftest.py"
    pp = project / "pyproject.toml"
    if pp.exists() and "[tool.pytest" in pp.read_text(errors="replace"):
        return "pyproject.toml [tool.pytest]"
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        f = project / name
        if f.exists():
            cp = configparser.ConfigParser()
            try:
                cp.read(f)
                if cp.has_section(section):
                    return f"{name} [{section}]"
            except configparser.Error:
                pass
    for d in ("tests", "test"):
        td = project / d
        if td.is_dir() and (any(td.rglob("test_*.py")) or any(td.rglob("*_test.py"))):
            return f"{d}/ contains test_*.py"
    return None


def _js_runner(project: Path) -> Optional[Detection]:
    pj = project / "package.json"
    if not pj.exists():
        return None
    try:
        data = json.loads(pj.read_text())
    except ValueError:
        data = {}
    deps = {}
    for k in ("dependencies", "devDependencies"):
        deps.update(data.get(k) or {})
    test_script = (data.get("scripts") or {}).get("test", "")
    configs = [p.name for p in project.glob("vitest.config.*")] + [p.name for p in project.glob("vite.config.*")]
    if "vitest" in deps or "vitest" in test_script or any(c.startswith("vitest") for c in configs):
        return Detection("vitest", _js_bin(project, "vitest") + ["run"], "auto: vitest in package.json")
    if "jest" in deps or "jest" in test_script or list(project.glob("jest.config.*")):
        return Detection("jest", _js_bin(project, "jest"), "auto: jest in package.json")
    if re.match(r"^node\s(?:[^|&;]*\s)?--test\b", test_script) and not SHELL_META.search(test_script):
        return Detection("node-test", shlex.split(test_script), "auto: package.json test script runs node --test")
    return None


def _node_test(project: Path) -> Optional[Detection]:
    files = detect_node_test(project)
    if not files:
        return None
    argv = ["node", "--test"]
    note = ""
    if any(f.endswith((".ts", ".mts")) for f in files) or _imports_ts(project, files):
        from ..envclass import node_version
        v = node_version()
        m = re.match(r"v(\d+)\.(\d+)", v)
        ver = (int(m.group(1)), int(m.group(2))) if m else None
        if ver and ver < (22, 6):
            note = (f"these tests load TypeScript, which needs Node >= 22.6; the node on PATH is {v}, so they "
                    f"will fail to load (ENVIRONMENT)")
        elif ver and ver < (23, 6):
            argv.append("--experimental-strip-types")
    argv += files
    return Detection("node-test", argv, f"auto: {len(files)} file(s) import node:test", note)


def _imports_ts(project: Path, files: List[str]) -> bool:
    for f in files[:20]:
        try:
            if re.search(r"""from\s+['"][^'"]+\.m?ts['"]|import\(['"][^'"]+\.m?ts['"]""",
                         (project / f).read_text(errors="replace")[:8000]):
                return True
        except OSError:
            pass
    return False


def _js_bin(project: Path, tool: str) -> List[str]:
    if (project / "node_modules" / ".bin" / tool).exists():
        return [str(Path("node_modules") / ".bin" / tool)]
    return ["npx", tool]


def auto_detect(project: Path) -> List[Detection]:
    found: List[Detection] = []
    why = _pytest_configured(project)
    if why:
        found.append(Detection("pytest", [_python(), "-m", "pytest"], f"auto: {why}"))
    js = _js_runner(project)
    if js:
        found.append(js)
    elif (project / "package.json").exists() or not found:
        nt = _node_test(project)
        if nt:
            found.append(nt)
    if (project / "go.mod").exists():
        found.append(Detection("go", ["go", "test", "./..."], "auto: go.mod"))
    if (project / "Cargo.toml").exists():
        found.append(Detection("cargo", ["cargo", "test"], "auto: Cargo.toml"))
    if (project / ".rspec").exists() or (project / "spec").is_dir():
        gem = (project / "Gemfile")
        argv = ["bundle", "exec", "rspec"] if gem.exists() else ["rspec"]
        found.append(Detection("rspec", argv, "auto: .rspec/spec"))
    return found


def make_runner(det: Detection, project: Path) -> Runner:
    if det.runner in CHECK_TOOLS:
        return CheckRunner(det.argv, project, det.runner)
    return RUNNERS[det.runner](det.argv, project)


CHECK_TOOLS = ("tsc", "eslint", "prettier", "next-build")
