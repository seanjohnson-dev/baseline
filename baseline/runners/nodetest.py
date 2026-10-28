"""node:test (`node --test`): TAP reporter written to a file (Node >= 20).

Test ids are the name path ("suite > test"); passing tests carry no location in
TAP, so the file is known only for failures (enough to rerun them). A test file
that fails to load is reported by node as a failing "test" named after the file:
that becomes a load error with the subprocess's stderr as its message.
Reruns: `--test-name-pattern` matching the test and its ancestors (a subtest only
runs if its parents match, on Node 20 and later alike) plus the files involved.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import normalize
from ..model import FAIL, LOAD_ERROR, PASS, SKIP, LoadError, RunResult, TestResult, Unit
from .base import Invocation, Runner, Side, crash_result

RESULT = re.compile(r"^(?P<ind> *)(?P<not>not )?ok \d+(?: - (?P<name>.*?))?(?: # (?P<dir>SKIP|TODO)\b.*)?$",
                    re.I)
SUBTEST = re.compile(r"^(?P<ind> *)# Subtest: (?P<name>.*)$")
TEST_FILE = re.compile(r"\.(?:c|m)?[jt]sx?$")
SUMMARY = re.compile(r"^# (?:tests|suites|pass|fail|cancelled|skipped|todo|duration_ms) ")


def is_node_test(argv: List[str]) -> bool:
    return bool(argv) and os.path.basename(argv[0]) in ("node", "node.exe") and "--test" in argv


def _unescape(name: str) -> str:
    return re.sub(r"\\(.)", r"\1", name)


def _yaml(lines: List[str], i: int, ind: int):
    """YAML diagnostics block after a result line -> (dict of the keys we use, next index)."""
    out: Dict[str, str] = {}
    pad = " " * (ind + 2)
    if i >= len(lines) or lines[i].strip() != "---":
        return out, i
    i += 1
    key = None
    while i < len(lines) and lines[i].strip() != "...":
        ln = lines[i]
        body = ln[len(pad):] if ln.startswith(pad) else ln.strip()
        m = re.match(r"^([A-Za-z_]+): ?(.*)$", body)
        if m and not ln.startswith(pad + " "):
            key, val = m.group(1), m.group(2)
            if val in ("|-", "|", ">-", ">"):
                out[key] = ""
            else:
                out[key] = val.strip().strip("'\"").replace("''", "'")
                key = None
        elif key is not None:
            out[key] = (out[key] + "\n" + body.strip()).strip("\n") if out.get(key) else body.strip()
        i += 1
    return out, i + 1


class NodeTestRunner(Runner):
    name = "node-test"

    def __init__(self, argv: List[str], project_dir: Path):
        super().__init__(argv, project_dir)
        k = argv.index("--test") if "--test" in argv else 1
        self.prefix = [a for a in argv[: k + 1]]
        rest = argv[k + 1:]
        self.opts: List[str] = []
        self.pos: List[str] = []
        skip_next = False
        for i, a in enumerate(rest):
            if skip_next:
                skip_next = False
                continue
            if a.startswith("--test-reporter") or a.startswith("--test-name-pattern"):
                skip_next = "=" not in a
                continue  # we own the reporter; reruns choose their own pattern
            (self.opts if a.startswith("-") else self.pos).append(a)

    def _argv(self, report: Path, extra: List[str], files: List[str]) -> List[str]:
        return self.prefix + ["--test-reporter=tap", f"--test-reporter-destination={report}"] + self.opts + \
            extra + files

    def full(self, side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.tap"
        return [Invocation(self._argv(report, [], self.pos), report, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        if any(u.kind == LOAD_ERROR or not u.locator.get("file") for u in units):
            return self.full(side, tag)
        names = set()
        for u in units:
            names.update(u.locator.get("path", u.name).split("\x1f"))
        pattern = "^(?:" + "|".join(sorted(re.escape(n) for n in names)) + ")$"
        files = sorted({side.from_repo_rel(u.locator["file"]) for u in units})
        report = side.outdir / f"{tag}.tap"
        return [Invocation(self._argv(report, [f"--test-name-pattern={pattern}"], files), report, "subset")]

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        if proc.timed_out:
            return crash_result(proc, f"node --test timed out after {proc.duration:.0f}s", inv)
        rep = inv.report
        if rep is None or not rep.exists() or rep.stat().st_size == 0:
            return crash_result(proc, f"node --test exited with code {proc.returncode} without a TAP report", inv)
        lines = rep.read_text(errors="replace").splitlines()
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        stack: List[str] = []
        comments: List[str] = []
        i = 0
        while i < len(lines):
            ln = lines[i]
            m = SUBTEST.match(ln)
            if m:
                lvl = len(m.group("ind")) // 4
                stack = stack[:lvl] + [_unescape(m.group("name"))]
                i += 1
                continue
            m = RESULT.match(ln)
            if not m:
                if ln.startswith("# ") and not SUMMARY.match(ln):
                    comments.append(ln[2:])
                i += 1
                continue
            ind = len(m.group("ind"))
            lvl = ind // 4
            name = _unescape(m.group("name") or "")
            diag, i = _yaml(lines, i + 1, ind)
            path = stack[:lvl] + [name]
            if diag.get("failureType") == "subtestsFailed":
                comments = []
                continue  # derived from its subtests
            loc = diag.get("location", "")
            lm = re.match(r"^(.*?):\d+:\d+$", loc)
            file = side.rel(lm.group(1)) if lm else ""
            if lvl == 0 and m.group("not") and os.path.isabs(name) and TEST_FILE.search(name) \
                    and diag.get("failureType") == "testCodeFailure" and "exitCode" in diag:
                rel = side.rel(name)
                text = "\n".join(comments).strip() or diag.get("error", "test file failed")
                first = next((c for c in text.splitlines() if re.search(r"Error|error", c)), text.splitlines()[0])
                key = self.make_key("", first, side)
                res.load_errors[rel] = LoadError(rel, key, normalize.first_lines(first, 4),
                                                 normalize.first_lines(text, 30),
                                                 self.classify(key, text, side), {"file": rel})
                comments = []
                continue
            comments = []
            tid = " > ".join(path)
            locator = {"file": file, "path": "\x1f".join(path)}
            if m.group("dir"):
                res.tests[tid] = TestResult(tid, file or "(node:test)", SKIP, name, locator)
            elif not m.group("not"):
                if tid not in res.tests or res.tests[tid].status != FAIL:
                    res.tests[tid] = TestResult(tid, file or "(node:test)", PASS, name, locator)
            else:
                err = diag.get("error", "")
                etype = diag.get("name", "") if diag.get("name", "") not in ("", "Error") else ""
                key = self.make_key(etype, normalize.first_lines(err, 6), side)
                raw = (err + "\n" + diag.get("stack", "")).strip()
                res.tests[tid] = TestResult(tid, file or "(node:test)", FAIL, name, locator, key,
                                            normalize.first_lines(err, 8), normalize.first_lines(raw, 30),
                                            self.classify(key, raw, side))
        if not res.tests and not res.load_errors and proc.returncode != 0:
            return crash_result(proc, f"node --test exited with code {proc.returncode} and no test results", inv)
        return res


def detect_node_test(project: Path) -> Optional[List[str]]:
    """Test files that import node:test (shallow search, no node_modules) -> their paths."""
    found: List[str] = []
    pats = ("*.test.js", "*.test.mjs", "*.test.cjs", "*.test.ts", "*.test.mts", "test/*.js", "test/*.mjs",
            "tests/*.js", "tests/*.mjs")
    seen = set()
    for depth in ("", "*/", "*/*/"):
        for pat in pats:
            for p in project.glob(depth + pat):
                if "node_modules" in p.parts or p in seen or not p.is_file():
                    continue
                seen.add(p)
                try:
                    head = p.read_text(errors="replace")[:4000]
                except OSError:
                    continue
                if re.search(r"""['"]node:test['"]""", head):
                    found.append(p.relative_to(project).as_posix())
    return sorted(found)[:50]
