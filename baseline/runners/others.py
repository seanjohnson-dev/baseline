"""cargo test (text output), rspec (JSON formatter) and the generic whole-command fallback."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List

from .. import normalize
from ..model import COMMAND, FAIL, LOAD_ERROR, PASS, SKIP, LoadError, RunResult, TestResult, Unit
from .base import Invocation, Runner, Side, crash_result

# ----------------------------------------------------------------------------------- cargo
CARGO_RUNNING = re.compile(r"^\s*Running (?:unittests )?(\S+)(?: \((\S+)\))?")
CARGO_DOCTESTS = re.compile(r"^\s*Doc-tests (\S+)")
CARGO_RESULT = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored|bench:.*)$")
CARGO_FAIL_HDR = re.compile(r"^---- (.+?) stdout ----$")
CARGO_COMPILE_ERR = re.compile(r"^error(?:\[E\d+\])?: (.*)$")
CARGO_COULD_NOT = re.compile(r"could not compile `([^`]+)`")


class CargoRunner(Runner):
    name = "cargo"

    def __init__(self, argv: List[str], project_dir: Path):
        super().__init__(argv, project_dir)
        if "--" in argv:
            k = argv.index("--")
            self.cargo_args, self.harness_args = argv[:k], argv[k + 1:]
        else:
            self.cargo_args, self.harness_args = list(argv), []

    def _argv(self, harness_extra: List[str]) -> List[str]:
        args = list(self.cargo_args)
        if "--no-fail-fast" not in args:
            args.append("--no-fail-fast")
        return args + ["--"] + self.harness_args + harness_extra

    def full(self, side: Side, tag: str) -> List[Invocation]:
        return [Invocation(self._argv([]), None, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        if any(u.kind == LOAD_ERROR for u in units):
            return self.full(side, tag)
        names = sorted({u.locator.get("test", u.name) for u in units})
        return [Invocation(self._argv(["--exact"] + names), None, "subset")]

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        if proc.timed_out:
            return crash_result(proc, f"cargo test timed out after {proc.duration:.0f}s", inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        target = "(crate)"
        failures: Dict[str, List[str]] = {}
        cur_fail = None
        lines = proc.output.splitlines()
        for ln in lines:
            m = CARGO_RUNNING.match(ln)
            if m:
                target = m.group(1)
                cur_fail = None
                continue
            m = CARGO_DOCTESTS.match(ln)
            if m:
                target = "doctest:" + m.group(1)
                continue
            m = CARGO_RESULT.match(ln)
            if m:
                name, st = m.group(1), m.group(2)
                tid = f"{target}::{name}"
                status = PASS if st == "ok" or st.startswith("bench") else (SKIP if st == "ignored" else FAIL)
                res.tests[tid] = TestResult(tid, target, status, name, {"test": name, "target": target})
                continue
            m = CARGO_FAIL_HDR.match(ln)
            if m:
                cur_fail = f"{target}::{m.group(1)}"
                failures[cur_fail] = []
                continue
            if cur_fail is not None:
                if ln.startswith("---- ") or ln.strip() == "failures:" or ln.startswith("test result:"):
                    cur_fail = None
                else:
                    failures[cur_fail].append(ln)
        for tid, body in failures.items():
            t = res.tests.get(tid)
            if t is None or t.status != FAIL:
                continue
            text = "\n".join(body).strip()
            pm = re.search(r"panicked at [^\n]*\n(.*?)(?:\nnote:|\Z)", text, re.S)
            msg = pm.group(1).strip() if pm else text
            t.key = self.make_key("panic", msg, side)
            t.message = normalize.first_lines(msg, 8)
            t.raw = normalize.first_lines(text, 40)
            t.env = self.classify(t.key, text, side)
        if not res.tests:
            crate = None
            errs: List[str] = []
            for ln in lines:
                m = CARGO_COMPILE_ERR.match(ln)
                if m and not ln.startswith("error: could not compile"):
                    errs.append(m.group(0))
                m2 = CARGO_COULD_NOT.search(ln)
                if m2:
                    crate = m2.group(1)
            if errs or crate:
                uid = f"(crate {crate})" if crate else "(crate)"
                msg = "\n".join(errs[:10]) or f"could not compile {crate}"
                key = self.make_key("", msg, side)
                res.load_errors[uid] = LoadError(uid, key, normalize.first_lines(msg, 8),
                                                 normalize.first_lines(proc.output, 40),
                                                 self.classify(key, msg, side), {})
            elif proc.returncode != 0:
                return crash_result(proc, f"cargo test exited with code {proc.returncode} and no test results", inv)
        return res


# ----------------------------------------------------------------------------------- rspec
class RspecRunner(Runner):
    name = "rspec"

    def __init__(self, argv: List[str], project_dir: Path):
        super().__init__(argv, project_dir)
        idx = next((i for i, a in enumerate(argv) if a.endswith("rspec")), len(argv) - 1)
        self.prefix = argv[: idx + 1]
        rest = argv[idx + 1:]
        self.opts = [a for a in rest if a.startswith("-")]
        self.pos = [a for a in rest if not a.startswith("-")]

    def _argv(self, report: Path, extra: List[str]) -> List[str]:
        return self.prefix + self.opts + ["--format", "progress", "--format", "json", "--out", str(report)] + extra

    def full(self, side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.json"
        return [Invocation(self._argv(report, self.pos), report, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.json"
        locs = []
        for u in units:
            if u.kind == LOAD_ERROR:
                locs.append(side.from_repo_rel(u.group))
            else:
                locs.append(side.from_repo_rel(u.group) + u.locator.get("index", ""))
        return [Invocation(self._argv(report, locs), report, "subset")]

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        if proc.timed_out:
            return crash_result(proc, f"rspec timed out after {proc.duration:.0f}s", inv)
        rep = inv.report
        if rep is None or not rep.exists() or rep.stat().st_size == 0:
            return crash_result(proc, f"rspec exited with code {proc.returncode} without writing a report", inv)
        try:
            data = json.loads(rep.read_text(errors="replace"))
        except ValueError as e:
            return crash_result(proc, f"could not parse rspec JSON: {e}", inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        for ex in data.get("examples") or []:
            rel = side.rel(ex.get("file_path") or "")
            eid = ex.get("id") or ""
            index = eid[eid.index("["):] if "[" in eid else ""
            tid = f"{rel} > {ex.get('full_description') or eid}"
            st = ex.get("status")
            loc = {"index": index}
            if st == "failed":
                exc = ex.get("exception") or {}
                msg = (exc.get("message") or "").strip()
                key = self.make_key(exc.get("class") or "", msg, side)
                raw = msg + "\n" + "\n".join((exc.get("backtrace") or [])[:20])
                res.tests[tid] = TestResult(tid, rel, FAIL, ex.get("description") or "", loc, key,
                                            normalize.first_lines(msg, 8), raw, self.classify(key, raw, side))
            else:
                res.tests[tid] = TestResult(tid, rel, PASS if st == "passed" else SKIP,
                                            ex.get("description") or "", loc)
        for msg in data.get("messages") or []:
            m = re.search(r"An error occurred while loading (\S+?)\.?\s*$", msg.splitlines()[0] if msg else "")
            if m:
                rel = side.rel(m.group(1))
                body = "\n".join(msg.splitlines()[1:])
                key = self.make_key("", body.strip().splitlines()[0] if body.strip() else msg, side)
                res.load_errors[rel] = LoadError(rel, key, normalize.first_lines(body, 8),
                                                 normalize.first_lines(body, 30), self.classify(key, body, side), {})
        return res


# ----------------------------------------------------------------------------------- generic
class GenericRunner(Runner):
    """Unknown runner: run as-is, attribute only the whole command by exit code."""
    name = "generic"
    per_test = False
    UNIT = "(test command)"

    def full(self, side: Side, tag: str) -> List[Invocation]:
        return [Invocation(list(self.argv), None, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        return self.full(side, tag)

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        if proc.timed_out:
            return crash_result(proc, f"command timed out after {proc.duration:.0f}s", inv)
        if proc.not_found:
            return crash_result(proc, proc.stderr, inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        if proc.returncode == 0:
            res.tests[self.UNIT] = TestResult(self.UNIT, self.UNIT, PASS, self.UNIT)
        else:
            key = f"exit code {proc.returncode}"
            tail = normalize.first_lines("\n".join(proc.output.strip().splitlines()[-15:]), 15)
            res.tests[self.UNIT] = TestResult(self.UNIT, self.UNIT, FAIL, self.UNIT, {}, key,
                                              f"exit code {proc.returncode}", tail, None)
        return res


KIND_COMMAND = COMMAND
