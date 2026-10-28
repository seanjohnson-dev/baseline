"""vitest and jest adapters (both emit the jest-style JSON report)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import normalize
from ..model import FAIL, LOAD_ERROR, PASS, SKIP, LoadError, RunResult, TestResult, Unit
from .base import Invocation, Runner, Side, crash_result, split_positional

STACK_LINE = re.compile(r"^\s+at\s")
EXC_PREFIX = re.compile(r"^([A-Za-z_$][\w$.]*(?:Error|Exception))(?::|$)")
JS_REGEX_SPECIAL = re.compile(r"([.*+?^${}()|\[\]\\/\-])")


def js_regex_escape(s: str) -> str:
    return JS_REGEX_SPECIAL.sub(r"\\\1", s)


def split_failure(text: str):
    """jest/vitest failure message -> (exc_type, message-without-stack)."""
    text = normalize.strip_ansi(text or "")
    lines = text.splitlines()
    msg_lines: List[str] = []
    for ln in lines:
        if STACK_LINE.match(ln):
            break
        msg_lines.append(ln)
    msg = "\n".join(l for l in msg_lines if l.strip() and not l.strip().startswith("●")).strip()
    m = EXC_PREFIX.match(msg)
    return (m.group(1) if m else ""), msg


def find_token(argv: List[str], names) -> Optional[int]:
    for i, a in enumerate(argv):
        base = os.path.basename(a)
        for n in names:
            if base in (n, n + ".js", n + ".mjs", n + ".cjs", n + ".cmd") or a.endswith(f"/{n}/bin/{n}.js"):
                return i
    return None


class JsJsonRunner(Runner):
    tool = ""
    VALUE_OPTS: set = set()
    SHORT_VALUE_OPTS: set = set()
    DROP: set = set()
    DROP_VALUE: set = set()

    def __init__(self, argv: List[str], project_dir: Path):
        super().__init__(argv, project_dir)
        idx = find_token(argv, [self.tool])
        if idx is None:
            idx = len(argv) - 1
        self.prefix = self._local_bin(argv[: idx + 1])
        rest = argv[idx + 1:]
        rest = self._subcommand(rest)
        opts, pos = split_positional(rest, self.VALUE_OPTS, self.SHORT_VALUE_OPTS,
                                     lambda t: not t.startswith("-"))
        self.user_opts = self._clean(opts)
        self.user_pos = pos

    def _local_bin(self, prefix: List[str]) -> List[str]:
        """`npx vitest` -> node_modules/.bin/vitest when installed locally (saves ~0.4s per run)."""
        if len(prefix) >= 2 and os.path.basename(prefix[0]) in ("npx", "npx.cmd") and prefix[-1] == self.tool:
            extra = prefix[1:-1]
            if all(e in ("--no-install", "--offline", "-y", "--yes") for e in extra):
                local = self.project_dir / "node_modules" / ".bin" / self.tool
                if local.exists():
                    return [str(Path("node_modules") / ".bin" / self.tool)]
        return prefix

    def _subcommand(self, rest: List[str]) -> List[str]:
        return rest

    def _clean(self, opts: List[str]) -> List[str]:
        out: List[str] = []
        i = 0
        while i < len(opts):
            a = opts[i]
            name = a.split("=", 1)[0]
            if name in self.DROP or any(name.startswith(d + ".") for d in self.DROP_VALUE):
                i += 1
                continue
            if name in self.DROP_VALUE:
                i += 1 if "=" in a else 2
                continue
            out.append(a)
            i += 1
        return out

    def resolve_prefix(self, side: Side) -> List[str]:
        p = list(self.prefix)
        if p and p[0].startswith("node_modules"):
            p[0] = str(side.cwd / p[0])
        return p

    # -- parsing -----------------------------------------------------------------
    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        rep = inv.report
        if proc.timed_out:
            return crash_result(proc, f"{self.name} timed out after {proc.duration:.0f}s", inv)
        if rep is None or not rep.exists() or rep.stat().st_size == 0:
            if inv.mode == "subset" and re.search(r"No test files found|No tests found", proc.output):
                r = RunResult(exit_code=proc.returncode, duration=proc.duration)
                r.invocations.append(inv.argv)
                return r
            tail = normalize.one_line(proc.output[-600:], 400)
            return crash_result(proc, f"{self.name} exited with code {proc.returncode} without writing a "
                                      f"report{': ' + tail if tail else ''}", inv)
        try:
            data = json.loads(rep.read_text(encoding="utf-8", errors="replace"))
        except ValueError as e:
            return crash_result(proc, f"could not parse {self.name} JSON report: {e}", inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        for suite in data.get("testResults") or []:
            self._suite(suite, side, res)
        return res

    def _suite(self, suite: dict, side: Side, res: RunResult) -> None:
        rel = side.rel(suite.get("name") or suite.get("testFilePath") or "")
        asserts = suite.get("assertionResults") or []
        message = suite.get("message") or ""
        exec_err = suite.get("testExecError")
        failed_suite = suite.get("status") == "failed" and (message.strip() or exec_err)
        if failed_suite and not any(a.get("status") == "failed" for a in asserts):
            text = message
            if not text.strip() and isinstance(exec_err, dict):
                text = (exec_err.get("message") or "") + "\n" + (exec_err.get("stack") or "")
            exc, msg = split_failure(text)
            key = self.make_key(exc, msg, side)
            uid = rel if not asserts else rel + " > (suite)"
            res.load_errors[uid] = LoadError(uid, key, normalize.first_lines(msg, 8),
                                             normalize.first_lines(normalize.strip_ansi(text), 30),
                                             self.classify(key, text, side), {"file": rel})
        seen: Dict[str, int] = {}
        for a in asserts:
            titles = list(a.get("ancestorTitles") or []) + [a.get("title") or ""]
            full = a.get("fullName") or " ".join(t for t in titles if t)
            base_id = rel + " > " + " > ".join(t for t in titles if t)
            n = seen.get(base_id, 0) + 1
            seen[base_id] = n
            tid = base_id if n == 1 else f"{base_id} [{n}]"
            st = a.get("status")
            if st == "failed":
                fm = "\n".join(a.get("failureMessages") or [])
                exc, msg = split_failure(fm)
                key = self.make_key(exc, msg, side)
                res.tests[tid] = TestResult(tid, rel, FAIL, a.get("title") or "", {"file": rel, "full": full,
                                                                                      "nth": str(n)},
                                            key, normalize.first_lines(msg, 8),
                                            normalize.first_lines(normalize.strip_ansi(fm), 30),
                                            self.classify(key, fm, side))
            else:
                status = PASS if st == "passed" else SKIP
                res.tests[tid] = TestResult(tid, rel, status, a.get("title") or "",
                                            {"file": rel, "full": full, "nth": str(n)})

    def _name_pattern(self, units: List[Unit]) -> Optional[str]:
        names = sorted({u.locator.get("full", "") for u in units if u.kind != LOAD_ERROR})
        if not names:
            return None
        return "^(?:" + "|".join(js_regex_escape(n) for n in names) + ")$"

    def source_contains(self, text: str, unit: Unit) -> bool:
        name = unit.name or unit.id.rsplit(" > ", 1)[-1]
        return name in text


class VitestRunner(JsJsonRunner):
    name = "vitest"
    tool = "vitest"
    VALUE_OPTS = {"-t", "--testNamePattern", "-c", "--config", "-r", "--root", "--dir", "--project",
                  "--reporter", "--outputFile", "--pool", "--environment", "--shard", "--exclude",
                  "--maxWorkers", "--minWorkers", "--testTimeout", "--hookTimeout", "--retry", "--bail",
                  "--mode", "--workspace", "--teardownTimeout", "--maxConcurrency", "--silent",
                  "--sequence.seed", "--logHeapUsage", "--browser", "--inspect-brk", "--api"}
    SHORT_VALUE_OPTS = {"-t", "-c", "-r"}
    DROP = {"--watch", "-w", "--ui", "--open", "--changed", "--passWithNoTests", "--update", "-u"}
    DROP_VALUE = {"--reporter", "--outputFile", "--changed"}

    def _subcommand(self, rest: List[str]) -> List[str]:
        if rest and rest[0] in ("run", "watch", "dev", "related"):
            sub = rest[0]
            rest = rest[1:]
            if sub == "related":
                return ["related"] + rest
        return rest

    def _cmd(self, side: Side, report: Path, extra: List[str]) -> List[str]:
        return (self.resolve_prefix(side) + ["run"] + self.user_opts + extra
                + ["--reporter=json", f"--outputFile={report}"])

    def full(self, side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.json"
        return [Invocation(self._cmd(side, report, self.user_pos), report, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.json"
        files = sorted({side.from_repo_rel(u.locator.get("file", u.group)) for u in units})
        extra = list(files) + ["--passWithNoTests"]
        pat = self._name_pattern(units)
        if pat:
            extra += ["-t", pat]
        return [Invocation(self._cmd(side, report, extra), report, "subset")]

    def list_tests(self, side: Side, files: List[str]) -> Optional[List[str]]:
        return None


class JestRunner(JsJsonRunner):
    name = "jest"
    tool = "jest"
    VALUE_OPTS = {"-t", "--testNamePattern", "-c", "--config", "--rootDir", "--roots", "--testPathPattern",
                  "--testPathIgnorePatterns", "--maxWorkers", "-w", "--reporters", "--outputFile",
                  "--testTimeout", "--shard", "--selectProjects", "--env", "--testEnvironment",
                  "--coverageDirectory", "--seed", "--testMatch", "--testRegex", "--setupFiles",
                  "--globalSetup", "--globalTeardown", "--testSequencer", "--maxConcurrency",
                  "--changedSince", "--testRunner", "--preset", "--projects"}
    SHORT_VALUE_OPTS = {"-t", "-c", "-w"}
    DROP = {"--watch", "--watchAll", "--json", "-o", "--onlyChanged", "--lastCommit", "--listTests",
            "--passWithNoTests", "-u", "--updateSnapshot"}
    DROP_VALUE = {"--outputFile", "--changedSince"}

    def _cmd(self, side: Side, report: Path, extra: List[str]) -> List[str]:
        return (self.resolve_prefix(side) + self.user_opts + extra
                + ["--json", f"--outputFile={report}", "--watchAll=false"])

    def full(self, side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.json"
        return [Invocation(self._cmd(side, report, self.user_pos), report, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.json"
        files = sorted({side.from_repo_rel(u.locator.get("file", u.group)) for u in units})
        extra = ["--passWithNoTests", "--runTestsByPath"] + files
        pat = self._name_pattern(units)
        if pat:
            extra += ["-t", pat]
        return [Invocation(self._cmd(side, report, extra), report, "subset")]
