"""pytest adapter: injects --junitxml (xunit1 family, which carries the file path)."""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

from .. import normalize
from ..model import FAIL, LOAD_ERROR, PASS, SKIP, LoadError, RunResult, TestResult, Unit
from .base import Invocation, Runner, Side, crash_result, split_positional

VALUE_OPTS = {
    "--rootdir", "--deselect", "--ignore", "--ignore-glob", "--confcutdir", "--basetemp", "--junitxml",
    "--junit-xml", "--junit-prefix", "--maxfail", "--tb", "--durations", "--durations-min", "--log-level",
    "--log-file", "--log-file-level", "--log-format", "--log-date-format", "--log-cli-level",
    "--log-cli-format", "--log-cli-date-format", "--log-file-format", "--log-file-date-format",
    "--cov", "--cov-report", "--cov-config", "--cov-fail-under", "--import-mode", "--capture", "--color",
    "--code-highlight", "--override-ini", "--config-file", "-c", "--dist", "--numprocesses", "--timeout",
    "--reruns", "--reruns-delay", "--count", "--randomly-seed", "--html", "--json-report-file",
    "--report-log", "--doctest-glob", "--assert", "--pdbcls", "--log-auto-indent", "--verbosity",
    "--lfnf", "--last-failed-no-failures", "--max-worker-restart", "--rsyncdir", "--tx", "--keyword",
    "--markers-filter", "--strict-config-file", "--deselect-file",
}
SHORT_VALUE_OPTS = {"-k", "-m", "-p", "-o", "-r", "-W", "-n", "-c"}
# options that depend on the cache plugin or would hang/alter selection; we drop them
DROP_FLAGS = {"--lf", "--last-failed", "--ff", "--failed-first", "--nf", "--new-first", "--sw", "--stepwise",
              "--sw-skip", "--stepwise-skip", "--cache-clear", "--cache-show", "--pdb", "--trace"}
DROP_WITH_VALUE = {"--junitxml", "--junit-xml", "--lfnf", "--last-failed-no-failures"}

EXC_LAST_LINE = re.compile(r"^\S.*?:\d+: ([A-Za-z_][\w.]*)\s*$")
E_LINE = re.compile(r"^E {1,}(.*)$")
EXC_IN_E = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Failure|Warning|Failed))(?::|$)")
SETUP_MSG = re.compile(r'^failed on (?:setup|teardown) with "(.*)"$', re.S)
CONFTEST_ERR = re.compile(r"ImportError while loading conftest '([^']+)'")


def find_pytest(argv: List[str]) -> Optional[int]:
    for i, a in enumerate(argv):
        base = os.path.basename(a)
        if base in ("pytest", "py.test", "pytest.exe") or re.fullmatch(r"pytest-?\d+(\.\d+)?", base):
            return i
        if a == "-m" and i + 1 < len(argv) and argv[i + 1] in ("pytest", "py.test"):
            return i + 1
    return None


class PytestRunner(Runner):
    name = "pytest"

    def __init__(self, argv: List[str], project_dir: Path):
        super().__init__(argv, project_dir)
        idx = find_pytest(argv)
        if idx is None:
            idx = len(argv) - 1
        self.prefix = argv[: idx + 1]
        opts, pos = split_positional(argv[idx + 1:], VALUE_OPTS, SHORT_VALUE_OPTS, self._looks_positional)
        self.user_opts = self._clean_opts(opts)
        self.user_pos = pos

    def _looks_positional(self, tok: str) -> bool:
        if "::" in tok:
            return True
        return (self.project_dir / tok).exists() or tok.endswith(".py")

    @staticmethod
    def _clean_opts(opts: List[str]) -> List[str]:
        out: List[str] = []
        i = 0
        while i < len(opts):
            a = opts[i]
            name = a.split("=", 1)[0]
            if name in DROP_FLAGS:
                i += 1
                continue
            if name in DROP_WITH_VALUE:
                i += 1 if "=" in a else 2
                continue
            out.append(a)
            i += 1
        return out

    def _has_plugin_off(self, plugin: str) -> bool:
        a = self.user_opts
        for i, tok in enumerate(a):
            if tok == "-p" and i + 1 < len(a) and a[i + 1] == "no:" + plugin:
                return True
            if tok == "-pno:" + plugin or tok == "-p=no:" + plugin:
                return True
        return False

    def _common(self, report: Path) -> List[str]:
        args = list(self.user_opts)
        args += [f"--junitxml={report}", "-o", "junit_family=xunit1"]
        if not self._has_plugin_off("cacheprovider"):
            args += ["-p", "no:cacheprovider"]
        if "--continue-on-collection-errors" not in args:
            args.append("--continue-on-collection-errors")
        return args

    def full(self, side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.xml"
        return [Invocation(self.prefix + self._common(report) + self.user_pos, report, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        report = side.outdir / f"{tag}.xml"
        locs: List[str] = []
        for u in units:
            if u.kind == LOAD_ERROR:
                if u.group.startswith("("):
                    return self.full(side, tag)  # session-level failure: rerun everything
                locs.append(side.from_repo_rel(u.group))
            else:
                locs.append(side.from_repo_rel(u.locator.get("file", u.group)) + u.locator.get("rest", ""))
        return [Invocation(self.prefix + self._common(report) + locs, report, "subset")]

    # -- parsing ---------------------------------------------------------------
    def _resolve_file(self, f: str, side: Side) -> str:
        if not f:
            return f
        cur = side.cwd
        while True:
            if (cur / f).exists():
                return side.rel(str(cur / f))
            if cur == side.root or cur.parent == cur:
                break
            cur = cur.parent
        return side.rel(str(side.cwd / f))

    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        rep = inv.report
        if proc.timed_out:
            return crash_result(proc, f"pytest timed out after {proc.duration:.0f}s", inv)
        if rep is None or not rep.exists() or rep.stat().st_size == 0:
            m = CONFTEST_ERR.search(proc.output)
            if m:
                res = RunResult(exit_code=proc.returncode, duration=proc.duration,
                                output_tail=proc.output[-4000:])
                rel = side.rel(m.group(1))
                body = proc.output[m.start():]
                elines = [E_LINE.match(ln).group(1) for ln in body.splitlines() if E_LINE.match(ln)]
                msg = "\n".join(elines) or normalize.first_lines(body, 6)
                key = self.make_key("", msg, side)
                res.load_errors[rel] = LoadError(rel, key, normalize.first_lines(msg, 6),
                                                 normalize.first_lines(body, 30),
                                                 self.classify(key, body, side), {"file": rel})
                res.invocations.append(inv.argv)
                return res
            tail = normalize.one_line(proc.output[-600:], 400)
            return crash_result(proc, f"pytest exited with code {proc.returncode} without writing a report"
                                      f"{': ' + tail if tail else ''}", inv)
        try:
            tree = ET.parse(str(rep))
        except ET.ParseError as e:
            return crash_result(proc, f"could not parse pytest junit report: {e}", inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        for tc in tree.iter("testcase"):
            self._testcase(tc, side, res)
        return res

    def _testcase(self, tc, side: Side, res: RunResult) -> None:
        file_attr = tc.get("file") or ""
        classname = tc.get("classname") or ""
        name = tc.get("name") or ""
        fails = [c for c in tc if c.tag in ("failure", "error")]
        skipped = [c for c in tc if c.tag == "skipped"]
        if not classname and fails and (fails[0].get("message") or "").startswith("collection failure"):
            f = file_attr or name.replace(".", "/") + ".py"
            rel = self._resolve_file(f, side)
            body = fails[0].text or ""
            elines = [E_LINE.match(ln).group(1) for ln in body.splitlines() if E_LINE.match(ln)]
            msg = "\n".join(elines) if elines else normalize.first_lines(body, 8)
            key = self.make_key("", msg, side)
            res.load_errors[rel] = LoadError(rel, key, normalize.first_lines(msg, 8),
                                             normalize.first_lines(body, 40),
                                             self.classify(key, body, side), {"file": rel})
            return
        if not classname and skipped and not fails:
            return  # module skipped at collection
        rel = self._resolve_file(file_attr, side) if file_attr else classname.replace(".", "/") + ".py"
        stem = Path(rel).stem
        parts = classname.split(".") if classname else []
        cls_parts: List[str] = []
        if stem in parts:
            idx = len(parts) - 1 - parts[::-1].index(stem)
            cls_parts = parts[idx + 1:]
        rest = "::" + "::".join(cls_parts + [name])
        tid = rel + rest
        if fails:
            status = FAIL
            msg, exc, body = self._failure_text(fails)
            key = self.make_key(exc, msg, side)
            tr = TestResult(tid, rel, status, name.split("[")[0], {"file": rel, "rest": rest}, key,
                            normalize.first_lines(f"{exc}: {msg}" if exc and not msg.startswith(exc) else msg, 8),
                            normalize.first_lines(body, 40), self.classify(key, body, side))
        else:
            status = SKIP if skipped else PASS
            tr = TestResult(tid, rel, status, name.split("[")[0], {"file": rel, "rest": rest})
        prev = res.tests.get(tid)
        if prev is None or (prev.status != FAIL and status == FAIL):
            res.tests[tid] = tr

    @staticmethod
    def _failure_text(fails) -> Tuple[str, str, str]:
        el = fails[0]
        body = el.text or ""
        msg = el.get("message") or ""
        m = SETUP_MSG.match(msg)
        if m:
            msg = m.group(1)
        exc = ""
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if lines:
            m2 = EXC_LAST_LINE.match(lines[-1])
            if m2:
                exc = m2.group(1)
        if not exc:
            for ln in lines:
                em = E_LINE.match(ln)
                if em:
                    m3 = EXC_IN_E.match(em.group(1).strip())
                    if m3:
                        exc = m3.group(1)
                        break
        if not msg:
            elines = [E_LINE.match(ln).group(1) for ln in lines if E_LINE.match(ln)]
            msg = "\n".join(elines[:10])
        if len(fails) > 1:  # e.g. failure in call + error in teardown
            extra = fails[1].get("message") or ""
            msg = msg + "\n" + extra
        return msg[:4000], exc, body

    def source_contains(self, text: str, unit: Unit) -> bool:
        name = unit.name or unit.id.rsplit("::", 1)[-1].split("[")[0]
        return bool(re.search(r"\bdef\s+" + re.escape(name) + r"\b", text))
