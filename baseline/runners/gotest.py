"""go test adapter: `go test -json -count=1`; build/vet/setup failures become package load errors."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import normalize
from ..model import FAIL, LOAD_ERROR, PASS, SKIP, LoadError, RunResult, TestResult, Unit
from .base import Invocation, Runner, Side, crash_result

VALUE_FLAGS = {"-run", "-count", "-timeout", "-tags", "-p", "-parallel", "-bench", "-cpu", "-coverprofile",
               "-o", "-exec", "-ldflags", "-gcflags", "-asmflags", "-mod", "-skip", "-covermode", "-coverpkg",
               "-blockprofile", "-cpuprofile", "-memprofile", "-mutexprofile", "-trace", "-benchtime",
               "-outputdir", "-modfile", "-pkgdir", "-toolexec", "-overlay", "-pgo", "-shuffle", "-vet",
               "-fuzz", "-fuzztime", "-list", "-C", "-benchmem"}
BOOL_FLAGS_NO_VALUE = {"-benchmem"}
FRAMING = re.compile(r"^\s*(=== (RUN|PAUSE|CONT|NAME)\b|--- (FAIL|PASS|SKIP):|PASS$|FAIL$|ok\s|FAIL\s|coverage:)")
BUILD_HEADER = re.compile(r"^# (\S+)")


class GoRunner(Runner):
    name = "go"

    def __init__(self, argv: List[str], project_dir: Path):
        super().__init__(argv, project_dir)
        try:
            ti = argv.index("test")
        except ValueError:
            ti = 1
        self.prefix = argv[: ti + 1]
        rest = argv[ti + 1:]
        self.binary_args: List[str] = []
        if "-args" in rest:
            k = rest.index("-args")
            self.binary_args = rest[k:]
            rest = rest[:k]
        flags: List[str] = []
        pkgs: List[str] = []
        i = 0
        while i < len(rest):
            a = rest[i]
            if a.startswith("-"):
                name = a.split("=", 1)[0].replace("--", "-")
                if name in ("-json", "-v") or name == "-count":
                    if "=" not in a and name in VALUE_FLAGS and name not in BOOL_FLAGS_NO_VALUE:
                        i += 1
                    i += 1
                    continue
                flags.append(a)
                if "=" not in a and name in VALUE_FLAGS and name not in BOOL_FLAGS_NO_VALUE and i + 1 < len(rest):
                    flags.append(rest[i + 1])
                    i += 2
                    continue
            else:
                pkgs.append(a)
            i += 1
        self.flags = flags
        self.pkgs = pkgs or ["."]
        self.module = self._module_path()

    def _module_path(self) -> str:
        cur = self.project_dir
        for _ in range(20):
            gm = cur / "go.mod"
            if gm.exists():
                m = re.search(r"^module\s+(\S+)", gm.read_text(errors="replace"), re.M)
                return m.group(1) if m else ""
            if cur.parent == cur:
                break
            cur = cur.parent
        return ""

    def _flags_without_run(self) -> List[str]:
        out: List[str] = []
        i = 0
        while i < len(self.flags):
            a = self.flags[i]
            if a.split("=", 1)[0] in ("-run", "--run"):
                i += 1 if "=" in a else 2
                continue
            out.append(a)
            i += 1
        return out

    def full(self, side: Side, tag: str) -> List[Invocation]:
        argv = self.prefix + ["-json", "-count=1"] + self.flags + self.pkgs + self.binary_args
        return [Invocation(argv, None, "full")]

    def subset(self, units: List[Unit], side: Side, tag: str) -> List[Invocation]:
        whole = sorted({u.group for u in units if u.kind == LOAD_ERROR})
        if any(g.startswith("(") for g in whole):
            return self.full(side, tag)
        tests: Dict[str, set] = {}
        for u in units:
            if u.kind == LOAD_ERROR or u.group in whole:
                continue
            top = u.locator.get("test", u.name).split("/")[0]
            tests.setdefault(u.group, set()).add(top)
        invs: List[Invocation] = []
        base = self.prefix + ["-json", "-count=1"] + self._flags_without_run()
        if whole:
            invs.append(Invocation(base + whole + self.binary_args, None, "subset"))
        if tests:
            names = sorted(set().union(*tests.values()))
            pat = "^(?:" + "|".join(re.escape(n) for n in names) + ")$"
            invs.append(Invocation(base + ["-run", pat] + sorted(tests) + self.binary_args, None, "subset"))
        return invs

    # -- parsing ---------------------------------------------------------------------
    def parse(self, inv: Invocation, proc, side: Side) -> RunResult:
        if proc.timed_out:
            return crash_result(proc, f"go test timed out after {proc.duration:.0f}s", inv)
        res = RunResult(exit_code=proc.returncode, duration=proc.duration, output_tail=proc.output[-4000:])
        res.invocations.append(inv.argv)
        outputs: Dict[tuple, List[str]] = {}
        status: Dict[tuple, str] = {}
        order: List[tuple] = []
        pkg_out: Dict[str, List[str]] = {}
        pkg_status: Dict[str, str] = {}
        pkg_failed_build: Dict[str, str] = {}
        build_out: Dict[str, List[str]] = {}
        saw_json = False
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            saw_json = True
            act = ev.get("Action")
            if "ImportPath" in ev and act in ("build-output", "build-fail"):
                if act == "build-output":
                    build_out.setdefault(ev["ImportPath"], []).append(ev.get("Output", ""))
                continue
            pkg = ev.get("Package", "")
            test = ev.get("Test")
            if test:
                k = (pkg, test)
                if k not in outputs:
                    outputs[k] = []
                    order.append(k)
                if act == "output":
                    outputs[k].append(ev.get("Output", ""))
                elif act in ("pass", "fail", "skip"):
                    status[k] = act
            else:
                if act == "output":
                    pkg_out.setdefault(pkg, []).append(ev.get("Output", ""))
                elif act in ("pass", "fail", "skip"):
                    pkg_status[pkg] = act
                    if ev.get("FailedBuild"):
                        pkg_failed_build[pkg] = ev["FailedBuild"]
        # old go versions print build errors on stderr as "# pkg" blocks
        cur: Optional[str] = None
        for line in proc.stderr.splitlines():
            m = BUILD_HEADER.match(line)
            if m:
                cur = m.group(1)
                build_out.setdefault(cur, [])
                continue
            if cur is not None:
                build_out[cur].append(line + "\n")
        if not saw_json:
            tail = normalize.one_line(proc.output[-600:], 400)
            return crash_result(proc, f"go test exited with code {proc.returncode} without JSON output"
                                      f"{': ' + tail if tail else ''}", inv)
        failed_children = {(p, t.rsplit("/", 1)[0]) for (p, t), s in status.items() if s == "fail" and "/" in t}
        for k in order:
            pkg, test = k
            st = status.get(k)
            if st is None:
                # started but never finished (panic/timeout): the package output explains it
                st = "fail" if pkg_status.get(pkg) == "fail" else None
                if st is None:
                    continue
            msg_lines = [ln.rstrip("\n") for ln in outputs.get(k, []) if not FRAMING.match(ln)]
            msg = "\n".join(l.strip() for l in msg_lines if l.strip())
            if st == "fail" and k in failed_children and not msg:
                continue  # parent failed only because a subtest failed
            tid = f"{pkg}::{test}"
            loc = {"pkg": pkg, "test": test}
            if st == "fail":
                if not msg and status.get(k) is None:
                    msg = "\n".join(l.strip() for l in pkg_out.get(pkg, []) if l.strip() and not FRAMING.match(l))
                key = self.make_key("", msg, side)
                raw = "".join(outputs.get(k, []))
                res.tests[tid] = TestResult(tid, pkg, FAIL, test, loc, key, normalize.first_lines(msg, 8),
                                            normalize.first_lines(raw, 40), self.classify(key, msg, side))
            else:
                res.tests[tid] = TestResult(tid, pkg, PASS if st == "pass" else SKIP, test, loc)
        tested_pkgs = {p for (p, _t) in order}
        for pkg, st in pkg_status.items():
            if st != "fail":
                continue
            if pkg in tested_pkgs and any(res.tests.get(f"{pkg}::{t}") and res.tests[f"{pkg}::{t}"].status == FAIL
                                          for (p, t) in order if p == pkg):
                continue
            ip = pkg_failed_build.get(pkg)
            lines: List[str] = []
            if ip:
                lines = build_out.get(ip, [])
            if not lines:
                for imp, out in build_out.items():
                    if imp.split(" ")[0] == pkg:
                        lines = out
                        break
            if not lines:
                lines = [l for l in pkg_out.get(pkg, []) if not re.match(r"^(FAIL|ok)\s", l)]
            body = "".join(l if l.endswith("\n") else l + "\n" for l in lines)
            msg = "\n".join(l.strip() for l in body.splitlines() if l.strip() and not BUILD_HEADER.match(l))
            if not msg:
                msg = f"package {pkg} failed"
            key = self.make_key("", msg, side)
            res.load_errors[pkg] = LoadError(pkg, key, normalize.first_lines(msg, 8), normalize.first_lines(body, 40),
                                             self.classify(key, msg, side), {"pkg": pkg})
        return res

    def pkg_dir(self, pkg: str) -> Optional[str]:
        if self.module and (pkg == self.module or pkg.startswith(self.module + "/")):
            return pkg[len(self.module):].lstrip("/") or "."
        return None

    def source_contains(self, text: str, unit: Unit) -> bool:
        name = unit.locator.get("test", unit.name).split("/")[0]
        return bool(re.search(r"func\s+" + re.escape(name) + r"\s*\(", text))
