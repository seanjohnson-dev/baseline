"""Plain-English report (agents read the first line and the Agent: line)."""
from __future__ import annotations

from typing import Dict, List

from . import brand, normalize
from .deps import describe as describe_deps
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
    if v["kind"] == "check_error":
        if verdict == NEW:
            return f"not in base's errors ({plural(b['runs'], 'run')}), reported on head"
        if verdict == PRE:
            return "reported on base and head"
    if verdict == NEW:
        if v.get("head_only_fails") and b["failed"]:
            return (f"fails on base {_counts(b)} and head {_counts(h)}, but head fails in "
                    f"{plural(len(v['head_only_fails']), 'file')} base doesn't")
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


def plural(n: int, word: str, many: str = "") -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def kind_of(rep: Dict) -> str:
    """'tests' (per-test runner), 'check' (one unit per error) or 'command' (whole command)."""
    r = rep["runner"]
    if r.get("name") == "chain":
        return "chain"
    if r.get("check"):
        return "check"
    return "tests" if r["per_test"] else "command"


def command_text(rep: Dict) -> str:
    return normalize.one_line(" ".join(rep["runner"]["command"]))[:120]


def render(rep: Dict, json_path: str = "") -> str:
    s = rep["summary"]
    base = rep["base"]
    head = rep["head"]
    kind = kind_of(rep)
    lines: List[str] = []
    parts = [f"{s[k]} {w}" for k, w in SUMMARY_WORDS if s.get(k)]
    head_s = head["short"] + ("+dirty" if head["dirty"] else "")
    runs = rep.get("test_runs", 0)
    timing = f"{rep.get('duration_s', 0):.1f}s, " + (plural(runs, "test run") if kind == "tests" else plural(runs, "run"))
    where = f"(base {base['short']} → head {head_s}, {timing})"
    if not rep["results"]:
        if kind == "chain":
            what = f"all {plural(s.get('parts', 0), 'part')} of `{command_text(rep)}` passed"
        elif kind == "tests":
            total = s["tests_total"]
            what = "the 1 test passed" if total == 1 else f"all {total} tests passed"
        elif kind == "check":
            what = f"check passed (`{command_text(rep)}`)"
        else:
            what = f"`{command_text(rep)}` passed"
        lines.append(f"{brand.NAME}: {what} on head  {where}")
    else:
        lines.append(f"{brand.NAME}: {', '.join(parts)}  {where}")
    if kind == "chain":
        lines.append(f"  chained command `{command_text(rep)}`: {s.get('parts_judged', 0)} of "
                     f"{plural(s.get('parts', 0), 'part')} judged, each on its own; base = {base['how']}")
        for pi in rep["runner"].get("parts", []):
            lines.append(f"  [part {pi['index']}: {_short(pi['command'])}] {part_state(pi, rep['results'])}")
    elif kind == "check":
        noun = rep["runner"].get("noun", "error")
        unread = [v for v in rep["results"] if v["kind"] == "command"]
        if unread and not s.get("check_errors"):
            # never "0 errors" for a command that failed: its output just couldn't be read
            what = "the command failed and its output could not be read (no errors parsed)"
        else:
            what = f"{plural(s.get('check_errors', 0), noun)} on head"
        lines.append(f"  {what}; check {rep['runner']['name']} (`{command_text(rep)}`); base = {base['how']}")
    elif kind == "tests":
        lines.append(f"  {plural(s['tests_total'], 'test')} on head: {s['tests_passed']} passed, "
                     f"{s['tests_failed']} failed"
                     + (f", {s['tests_skipped']} skipped" if s["tests_skipped"] else "")
                     + (f", {s['load_errors']} file(s)/package(s) failed to load" if s["load_errors"] else "")
                     + f"; runner {rep['runner']['name']}; base = {base['how']}")
    else:
        lines.append(f"  command `{command_text(rep)}`; base = {base['how']}")
        lines.append("  note: unrecognised command — its output can't be read per test or per error, so it is one "
                     "unit: NEW if it passes on base or head fails in files base doesn't (`FAIL <file>` lines), "
                     "else UNKNOWN when base fails too.")
    if rep.get("deps_changed") and rep["results"]:
        lines.append(f"  Dependencies changed: {describe_deps(rep['deps_changed'])} — base ran with the new ones, so "
                     f"a failure that fails on base too is UNKNOWN, not pre-existing or environment")
    for verdict, title in SECTIONS:
        items = [v for v in rep["results"] if v["verdict"] == verdict]
        if not items:
            continue
        lines.append(title)
        for v in items:
            label = part_label(v) + v["id"] + (" [load error]" if v["kind"] == "load_error" else "")
            lines.append(f"  {label} — {_evidence(v, base['short'])}")
            h, b = v["head"], v["base"]
            if v["kind"] == "command" and (h["error_raw"] or b["error_raw"]):
                for name, side in (("head", h), ("base", b)):
                    shown = [ln for ln in (side["error_raw"] or "").splitlines() if ln.strip()][:4]
                    for i, ln in enumerate(shown):
                        lines.append(f"      {name if i == 0 else ' ' * len(name)}: {normalize.one_line(ln)[:200]}")
                only = v.get("head_only_fails") or []
                if only:
                    lines.append(f"      failing on head only ({len(only)}): " + ", ".join(only[:8])
                                 + (f" (+{len(only) - 8} more)" if len(only) > 8 else ""))
            elif verdict == PRE_CHANGED:
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


def _short(text: str, n: int = 60) -> str:
    t = normalize.one_line(text)
    return t if len(t) <= n else t[: n - 1] + "…"


def part_label(v: Dict) -> str:
    """`[part 2: jest --config jest.api.config.js] ` for a result of a chained command ('' otherwise)."""
    return f"[part {v['part']}: {_short(v.get('part_command', ''))}] " if v.get("part") else ""


def part_state(pi: Dict, results: List[Dict]) -> str:
    """One line per part of a chained command: what was judged and how it came out."""
    if not pi.get("judged"):
        return f"NOT JUDGED ({pi.get('problem') or 'unknown reason'}) — UNKNOWN"
    r = pi.get("runner") or {}
    s = pi.get("summary") or {}
    mine = [v for v in results if v.get("part") == pi["index"]]
    words = [f"{n} {w}" for k, w in SUMMARY_WORDS for n in [sum(1 for v in mine if v["verdict"] == k)] if n]
    if r.get("check"):
        what = f"{r.get('name')} check, {plural(s.get('check_errors', 0), r.get('noun') or 'error')} on head"
    elif r.get("per_test"):
        what = f"{r.get('name')}, {plural(s.get('tests_total', 0), 'test')} on head ({s.get('tests_failed', 0)} failed)"
    else:
        what = "whole command"
    return f"judged: {what}; " + (", ".join(words) if words else "passed")


def agent_line(rep: Dict) -> str:
    s = rep["summary"]
    if not rep["results"]:
        if kind_of(rep) == "chain":
            return "Agent: every part of the chained command was judged and passes; nothing to attribute."
        return "Agent: " + ("all tests pass" if kind_of(rep) == "tests" else "the command passes") + \
            "; nothing to attribute."
    out = []
    if s[NEW] and kind_of(rep) == "check":
        out.append(f"Fix the {s[NEW]} NEW error(s): base doesn't have them, this change introduced them; do not "
                   f"call them pre-existing.")
    elif s[NEW]:
        out.append(f"Fix the {s[NEW]} NEW failure(s): they pass on base and fail with this change; do not call "
                   f"them pre-existing.")
    if s[PRE_CHANGED]:
        out.append(f"{s[PRE_CHANGED]} failure(s) were already failing on base but now fail differently — check you "
                   f"didn't make them worse before calling them pre-existing.")
    if s[UNKNOWN]:
        out.append(f"{s[UNKNOWN]} failure(s) could not be compared with base — do not claim they are pre-existing.")
    deps = rep.get("deps_changed")
    if deps:
        out.append(f"This change edits dependencies ({describe_deps(deps)}) and base ran with the new ones, so a "
                   f"failure on both sides may come from the dependency change.")
    rest = s[PRE] + s[FLAKY] + s[ENVIRONMENT]
    if rest:
        out.append(f"The other {rest} failure(s) are pre-existing, flaky or environmental (proof above); report them "
                   f"as such rather than fixing them as part of this change.")
    proven = all(v["verdict"] in (PRE, FLAKY, ENVIRONMENT) and (v["kind"] != "command" or v["verdict"] == FLAKY)
                 for v in rep["results"])
    if not s[NEW] and not s[UNKNOWN] and not s[PRE_CHANGED] and proven and not deps:
        out.insert(0, "No failure was caused by this change.")
    return "Agent: " + " ".join(out)
