"""Plain-English report (agents read the first line and the Agent: line)."""
from __future__ import annotations

from typing import Dict, List

from . import brand, normalize
from .model import ENVIRONMENT, FLAKY, NEW, PRE, PRE_CHANGED, UNKNOWN

SECTIONS = [
    (NEW, "NEW (this change broke these):"),
    (PRE_CHANGED, "PRE-EXISTING (error changed) — still failing, but differently; check you didn't make it worse:"),
    (UNKNOWN, "UNKNOWN (no trustworthy base result — do NOT call these pre-existing):"),
    (PRE, "PRE-EXISTING (already failing on base, same error):"),
    (ENVIRONMENT, "ENVIRONMENT (fails on base and head for environmental reasons):"),
    (FLAKY, "FLAKY (passed and failed on the same code):"),
]
SUMMARY_WORDS = [(NEW, "NEW"), (PRE, "pre-existing"), (PRE_CHANGED, "pre-existing (error changed)"),
                 (FLAKY, "flaky"), (ENVIRONMENT, "environment"), (UNKNOWN, "unknown")]


def _counts(side: Dict) -> str:
    return f"{side['failed']}/{side['runs']}" if side["runs"] else "0/0"


def _evidence(v: Dict, base_short: str) -> str:
    h, b = v["head"], v["base"]
    verdict = v["verdict"]
    noun = "loads" if v["kind"] == "load_error" else "passed"
    if verdict == NEW:
        if b["passed"]:
            return f"{noun} on base {b['passed']}/{b['runs']}, fails on head {_counts(h)}"
        return f"not on base, fails on head {_counts(h)}"
    if verdict == FLAKY:
        if v.get("flaky_side") == "base":
            return f"head failed {_counts(h)}; base failed {_counts(b)} (flaky on base)"
        return f"head failed {h['failed']}, passed {h['passed']} of {h['runs']}"
    if b["runs"]:
        return f"fails on base {_counts(b)} and head {_counts(h)}"
    return f"fails on head {_counts(h)}"


def render(rep: Dict, json_path: str = "") -> str:
    s = rep["summary"]
    base = rep["base"]
    head = rep["head"]
    lines: List[str] = []
    parts = [f"{s[k]} {w}" for k, w in SUMMARY_WORDS if s.get(k)]
    head_s = head["short"] + ("+dirty" if head["dirty"] else "")
    timing = f"{rep.get('duration_s', 0):.1f}s, {rep.get('test_runs', 0)} test runs"
    where = f"(base {base['short']} → head {head_s}, {timing})"
    if not rep["results"]:
        total = s["tests_total"]
        lines.append(f"{brand.NAME}: all {total} tests passed on head  {where}")
    else:
        lines.append(f"{brand.NAME}: {', '.join(parts)}  {where}")
    lines.append(f"  {s['tests_total']} tests on head: {s['tests_passed']} passed, {s['tests_failed']} failed"
                 + (f", {s['tests_skipped']} skipped" if s["tests_skipped"] else "")
                 + (f", {s['load_errors']} file(s)/package(s) failed to load" if s["load_errors"] else "")
                 + f"; runner {rep['runner']['name']}; base = {base['how']}")
    if not rep["runner"]["per_test"]:
        lines.append("  note: unrecognised test runner — can't attribute per test; this is a whole-command verdict "
                     "based on the exit code.")
    for verdict, title in SECTIONS:
        items = [v for v in rep["results"] if v["verdict"] == verdict]
        if not items:
            continue
        lines.append(title)
        for v in items:
            label = v["id"] + (" [load error]" if v["kind"] == "load_error" else "")
            lines.append(f"  {label} — {_evidence(v, base['short'])}")
            h, b = v["head"], v["base"]
            if verdict == PRE_CHANGED:
                lines.append(f"      base: {normalize.one_line(b['message'] or b['error'])}")
                lines.append(f"      head: {normalize.one_line(h['message'] or h['error'])}")
            elif verdict in (NEW, UNKNOWN, PRE, ENVIRONMENT) and (h["message"] or h["error"]):
                lines.append(f"      head: {normalize.one_line(h['message'] or h['error'])}")
            if verdict in (ENVIRONMENT,) and v.get("env_class"):
                lines.append(f"      env: {v['env_class']['detail']}")
            if v.get("env_suspect") and v.get("env_class"):
                lines.append(f"      hint: looks environmental ({v['env_class']['detail']}) — did the change add a "
                             f"dependency/requirement?")
            if v.get("blocked_tests"):
                bt = v["blocked_tests"]
                more = f" (+{len(bt) - 5} more)" if len(bt) > 5 else ""
                lines.append(f"      blocked: {', '.join(bt[:5])}{more}")
            if verdict in (UNKNOWN,):
                lines.append(f"      why: {v['reason']}")
    if rep.get("fixed"):
        fx = rep["fixed"]
        lines.append(f"Fixed (failed every base run, passed every head run): {', '.join(fx[:10])}"
                     + (f" (+{len(fx) - 10} more)" if len(fx) > 10 else ""))
    if rep.get("flaky_passing"):
        fp = rep["flaky_passing"]
        lines.append(f"Flaky but passing on head this time (not failures): {', '.join(fp[:10])}")
    if rep.get("missing"):
        ms = rep["missing"]
        lines.append(f"Missing on head (exist on base; deleted or renamed?): {', '.join(ms[:10])}"
                     + (f" (+{len(ms) - 10} more)" if len(ms) > 10 else ""))
    for w in rep.get("warnings", []):
        lines.append(f"warning: {w}")
    for n in rep.get("notes", []):
        lines.append(f"note: {n}")
    lines.append(agent_line(rep))
    if json_path:
        lines.append(f"Details (JSON): {json_path}")
    return "\n".join(lines)


def agent_line(rep: Dict) -> str:
    s = rep["summary"]
    if not rep["results"]:
        return "Agent: all tests pass; nothing to attribute."
    out = []
    if s[NEW]:
        out.append(f"Fix the {s[NEW]} NEW failure(s): they pass on base and fail with this change; do not call "
                   f"them pre-existing.")
    if s[PRE_CHANGED]:
        out.append(f"{s[PRE_CHANGED]} failure(s) were already failing on base but now fail differently — check you "
                   f"didn't make them worse before calling them pre-existing.")
    if s[UNKNOWN]:
        out.append(f"{s[UNKNOWN]} failure(s) could not be compared with base — do not claim they are pre-existing.")
    rest = s[PRE] + s[FLAKY] + s[ENVIRONMENT]
    if rest:
        out.append(f"The other {rest} failure(s) are pre-existing, flaky or environmental (proof above); report them "
                   f"as such rather than fixing them as part of this change.")
    if not s[NEW] and not s[UNKNOWN] and not s[PRE_CHANGED]:
        out.insert(0, "No failure was caused by this change.")
    return "Agent: " + " ".join(out)
