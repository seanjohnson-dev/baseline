"""Verdict logic (pure functions; see docs/VERDICTS.md).

Inputs per failing unit: the observations on head and on base, plus a few facts
(does the file exist on base, did anything the test depends on change).
Two jobs:
  * needs_head()/needs_base(): how many more runs each side still needs
    (sequential sampling with early stop), and
  * decide(): the verdict + evidence once sampling is done.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import brand, normalize
from .model import (ABSENT, BLOCKED, CHECK, COMMAND, ENVIRONMENT, FAIL, FLAKY, LOAD_ERROR, NEW, PASS, PRE,
                    PRE_CHANGED, SKIP, UNKNOWN, Obs, Unit)


@dataclass
class Policy:
    runs: int = 5            # head: max runs per failing unit (the first full run counts)
    base_required: int = 5   # base: runs every deterministic verdict needs
    base_runs: int = 10      # base: confirmation target, spent while cheap (see engine) or riding along
    max_runs: int = 15       # per side, when a verdict is suspicious (see suspicious())

    @classmethod
    def from_runs(cls, runs: int, base_runs: Optional[int] = None, max_runs: Optional[int] = None) -> "Policy":
        runs = max(1, runs)
        if base_runs is not None:  # explicit: no optional confirmation, exactly this many
            req = conf = max(1, base_runs)
        else:
            req, conf = runs, 2 * runs
        m = max_runs if max_runs is not None else 3 * runs
        return cls(runs, req, conf, max(m, runs, conf))


@dataclass
class UnitState:
    unit: Unit
    head: List[Obs] = field(default_factory=list)
    base: List[Obs] = field(default_factory=list)
    closure_changed: Optional[bool] = None   # True/False/None(unknown)
    base_problem: str = ""                   # base side untrusted for this unit
    exists_on_base: Optional[bool] = None    # file/package exists at the base commit
    mentioned_on_base: Optional[bool] = None # test name found in base sources
    blocked_tests: List[str] = field(default_factory=list)
    head_attempts: int = 0
    base_attempts: int = 0


def _split(obs: List[Obs]):
    fails = [o for o in obs if o.status in (FAIL, BLOCKED)]
    passes = [o for o in obs if o.status == PASS]
    skips = [o for o in obs if o.status == SKIP]
    absent = [o for o in obs if o.status == ABSENT]
    return fails, passes, skips, absent


def _keys(obs: List[Obs]) -> List[str]:
    return [o.key for o in obs]


def noisy(obs: List[Obs]) -> bool:
    return len(set(_keys(obs))) > 1


def errors_match(hf: List[Obs], bf: List[Obs]) -> Optional[str]:
    """'exact' | 'loose' | None. Loose (numbers masked) only when a side's own text varies run to run."""
    hk, bk = set(_keys(hf)), set(_keys(bf))
    if hk & bk:
        return "exact"
    if (len(hk) > 1 or len(bk) > 1) and ({normalize.loose(k) for k in hk} & {normalize.loose(k) for k in bk}):
        return "loose"
    return None


def candidate(st: UnitState) -> str:
    return decide(st)["verdict"]


def suspicious(st: UnitState) -> bool:
    """A deterministic-looking verdict that a coin-flip flaky test could fake.

    * NEW / error-changed although nothing the test depends on changed, or
    * failure text that varies between runs of the same code.
    Suspicious units get up to Policy.max_runs per side before we commit.
    """
    if st.unit.kind != "test":
        return False
    hf, hp, _, _ = _split(st.head)
    if hp:
        return False
    bf, bp, _, _ = _split(st.base)
    if bf and bp:
        return False
    cand = candidate(st)
    if cand in (NEW, PRE_CHANGED) and st.closure_changed is False and (bp or bf):
        return True
    if cand in (NEW, PRE, PRE_CHANGED, ENVIRONMENT) and (noisy(hf) or noisy(bf)):
        return True
    return False


def needs_head(st: UnitState, p: Policy) -> int:
    hf, hp, _, _ = _split(st.head)
    if hp or not hf:
        return 0
    target = p.max_runs if suspicious(st) else p.runs
    if st.unit.kind == CHECK:
        target = 1  # a check (tsc/eslint/prettier) prints the same errors for the same code
    elif st.unit.group.startswith("(") or st.unit.kind == COMMAND:
        target = min(target, 2)  # whole-session failure: each rerun is a full run
    if st.head_attempts >= target + 2:
        return 0
    return max(0, target - len(hf))


def base_targets(st: UnitState, p: Policy):
    """(required, optional) number of base runs for this unit."""
    hf, hp, _, _ = _split(st.head)
    if hp or st.base_problem:
        return 0, 0
    bf, bp, bs, ba = _split(st.base)
    if bf and bp:
        return 0, 0
    if not bf and not bp and (bs or ba):
        return 0, 0  # skipped/absent on base: deterministic, no point repeating
    if st.unit.kind == CHECK:
        return 1, 1
    if st.unit.group.startswith("(") or st.unit.kind == COMMAND:
        t = min(p.runs, 2)
        return t, t
    if st.unit.kind == LOAD_ERROR:
        return p.runs, p.runs
    if suspicious(st):
        return p.max_runs, p.max_runs
    req = p.base_required
    if bp and not bf and st.closure_changed is None:
        req = max(req, p.base_runs)  # NEW and we can't tell what the change touched: confirm
    return req, max(req, p.base_runs)


def _base_count(st: UnitState) -> int:
    bf, bp, _, _ = _split(st.base)
    return len(bf) + len(bp)


def needs_base(st: UnitState, p: Policy) -> int:
    req, _ = base_targets(st, p)
    if st.base_attempts >= p.max_runs + 2:
        return 0
    return max(0, req - _base_count(st))


def wants_base(st: UnitState, p: Policy) -> int:
    """Optional confirmation runs (taken when they ride along or are cheap)."""
    _, opt = base_targets(st, p)
    if st.base_attempts >= p.max_runs + 2:
        return 0
    return max(0, opt - _base_count(st))


def fixed_target(st: UnitState, p: Policy) -> int:
    """Runs per side before calling a test 'fixed'. A change that didn't touch anything the test depends on
    can't have fixed it, so such candidates (likely flaky) must survive the suspicious budget."""
    if st.closure_changed is False:
        return p.max_runs
    if st.closure_changed is None:
        return p.base_runs
    return p.runs


def needs_head_fixed(st: UnitState, p: Policy) -> int:
    """'Fixed' candidate (failed on base, passed on head): confirm it keeps passing on head."""
    hf, hp, _, _ = _split(st.head)
    t = fixed_target(st, p)
    if hf or st.head_attempts >= t + 2:
        return 0
    return max(0, t - len(hp))


def needs_base_fixed(st: UnitState, p: Policy) -> int:
    bf, bp, _, _ = _split(st.base)
    hf, _, _, _ = _split(st.head)
    t = fixed_target(st, p)
    if bp or hf or st.base_problem or st.base_attempts >= t + 2:
        return 0
    return max(0, t - len(bf))


def fixed_verdict(st: UnitState) -> str:
    """'fixed' when it failed every base run and passed every head run, else 'flaky'."""
    bf, bp, _, _ = _split(st.base)
    hf, hp, _, _ = _split(st.head)
    return "fixed" if bf and hp and not bp and not hf else "flaky"


def flip_result(st: UnitState) -> Dict:
    """FLAKY result for a test that is NOT among head's failures (it passed in the full head run) but
    flipped between pass and fail in runs that were made anyway. Informational: never NEW, exit code 0."""
    hf, hp, _, _ = _split(st.head)
    bf, bp, _, _ = _split(st.base)
    h, b = len(hf) + len(hp), len(bf) + len(bp)
    out: Dict = {"id": st.unit.id, "kind": st.unit.kind, "verdict": FLAKY, "reason": "",
                 "head": _side_summary(st.head), "base": _side_summary(st.base),
                 "runs_total": h + b, "env_class": None, "env_suspect": False, "flaky_side": None,
                 "match": None, "blocked_tests": [], "closure_changed": st.closure_changed, "surfaced": True}
    if hf and hp:
        out.update(flaky_side="head",
                   reason=f"Passed {len(hp)} and failed {len(hf)} of {h} runs on head with the same code — flaky "
                          f"(not counted as a head failure; it passed in the full run).")
    else:
        out.update(flaky_side="base",
                   reason=f"On base it failed {len(bf)} and passed {len(bp)} of {b} runs with the same code; on head "
                          f"it passed {len(hp)}/{h} — flaky, not fixed or broken by this change.")
    return out


def _side_summary(obs: List[Obs]) -> Dict:
    f, p, s, a = _split(obs)
    ex = f[0] if f else None
    return {
        "runs": len(f) + len(p),
        "failed": len(f),
        "passed": len(p),
        "skipped": len(s),
        "absent": len(a),
        "cached_runs": sum(1 for o in obs if o.cached),
        "error": ex.key if ex else "",
        "error_raw": ex.raw or ex.message if ex else "",
        "message": ex.message if ex else "",
        "distinct_errors": len(set(_keys(f))),
    }


def _env_of(obs: List[Obs]) -> Optional[Dict[str, str]]:
    envs = [o.env for o in obs]
    if obs and all(envs):
        return envs[0]
    return None


def _what_same(kind: str) -> str:
    return {LOAD_ERROR: "Fails to load", CHECK: "Reported"}.get(kind, "Fails")


def decide(st: UnitState) -> Dict:
    u = st.unit
    hf, hp, hs, ha = _split(st.head)
    bf, bp, bs, ba = _split(st.base)
    h, b = len(hf) + len(hp), len(bf) + len(bp)
    out: Dict = {"id": u.id, "kind": u.kind, "verdict": UNKNOWN, "reason": "",
                 "head": _side_summary(st.head), "base": _side_summary(st.base),
                 "runs_total": h + b, "env_class": None, "env_suspect": False, "flaky_side": None,
                 "match": None, "blocked_tests": list(st.blocked_tests),
                 "closure_changed": st.closure_changed}
    what = "fails to load" if u.kind == LOAD_ERROR else "fails"
    henv = _env_of(hf)

    # 1. flaky on head: never NEW, regardless of base
    if hf and hp:
        out.update(verdict=FLAKY, flaky_side="head",
                   reason=f"Failed {len(hf)} and passed {len(hp)} of {h} runs on head with the same code — "
                          f"flaky, not caused by this change.")
        return out
    if not hf:
        out.update(verdict=FLAKY, flaky_side="head",
                   reason="Failed in the full run but never again on rerun — flaky (or order-dependent).")
        return out

    # 2. base could not be established
    if st.base_problem:
        out.update(verdict=UNKNOWN, reason=f"No trustworthy base result ({st.base_problem}); this must not be "
                                           f"reported as pre-existing.")
        return out

    # 3. flaky on base
    if bf and bp:
        out.update(verdict=FLAKY, flaky_side="base",
                   reason=f"Failed all {h} head runs, but on base it is flaky too (failed {len(bf)}/{b}) — "
                          f"can't be attributed to this change.")
        return out

    # 4. passes on base -> NEW
    if bp and not bf:
        if u.kind == CHECK:
            reason = (f"Not among base's errors ({b} run(s) of the same command) but reported on head — this "
                      f"change introduced it.")
        elif u.kind == COMMAND:
            reason = f"Passed on base ({b}/{b}) but fails on head ({len(hf)}/{h}) — this change broke it."
        elif u.kind == LOAD_ERROR:
            reason = (f"Loads fine on base but {what} on head ({len(hf)}/{h}) — this change broke it"
                      + (f"; {len(st.blocked_tests)} test(s) in it can't run." if st.blocked_tests else "."))
        else:
            reason = f"Passed all {b} runs on base but failed all {h} runs on head — this change broke it."
        if henv:
            out["env_suspect"] = True
            out["env_class"] = henv
            reason += (f" The error looks environmental ({henv['detail']}): the change probably added a "
                       f"requirement this environment doesn't have.")
        out.update(verdict=NEW, reason=reason)
        return out

    # 5. fails on base too
    if bf and u.kind == COMMAND:
        benv = _env_of(bf)
        if henv and benv:
            out.update(verdict=ENVIRONMENT, env_class=henv,
                       reason=f"The command fails on base ({len(bf)}/{b}) and head ({len(hf)}/{h}) with an "
                              f"environment error ({henv['detail']}) — fix the environment, not the code.")
            return out
        out.update(verdict=UNKNOWN, env_class=benv,
                   reason=f"Can't tell: the command fails on base too and {brand.NAME} can't read its output per "
                          "error, so the exit code proves nothing either way; compare the error lines yourself "
                          "(head and base below) — do not call this pre-existing.")
        return out
    if bf:
        benv = _env_of(bf)
        match = errors_match(hf, bf)
        out["match"] = match
        if match:
            if henv and benv:
                out.update(verdict=ENVIRONMENT, env_class=henv,
                           reason=f"Fails on base ({len(bf)}/{b}) and head ({len(hf)}/{h}) with an environment "
                                  f"error ({henv['detail']}) — fix the environment, not the code.")
            else:
                extra = " (error text varies run to run; matched ignoring numbers)" if match == "loose" else ""
                out.update(verdict=PRE,
                           reason=f"{_what_same(u.kind)} the same way on base "
                                  f"({len(bf)}/{b}) and head ({len(hf)}/{h}){extra} — already broken before "
                                  f"this change.")
            return out
        if henv and benv:
            out.update(verdict=ENVIRONMENT, env_class=henv,
                       reason=f"Fails on both sides with environment errors (base: {benv['detail']}; head: "
                              f"{henv['detail']}) — fix the environment, not the code.")
            return out
        if benv and not henv:
            out.update(verdict=UNKNOWN, env_class=benv,
                       reason=f"On base it fails for an environmental reason ({benv['detail']}) while head fails "
                              f"differently — the base result proves nothing; this must not be reported as "
                              f"pre-existing.")
            return out
        out["similarity"] = normalize.similarity(hf[0].key, bf[0].key)
        blocked_note = " (on base its file failed to load)" if all(o.status == BLOCKED for o in bf) else ""
        reason = (f"Failed on base too ({len(bf)}/{b}){blocked_note}, but the error is different now — still "
                  f"failing, check this change didn't make it worse.")
        if henv:
            out["env_suspect"] = True
            out["env_class"] = henv
        out.update(verdict=PRE_CHANGED, reason=reason)
        return out

    # 6. never ran on base
    if u.kind == CHECK:
        out.update(verdict=UNKNOWN, reason=f"The base run failed without errors {brand.NAME} can read, so it is not "
                                           "known whether base has this error — do not call it pre-existing.")
        return out
    if bs and not ba:
        out.update(verdict=UNKNOWN, reason="Skipped on base, so there is no base result to compare against.")
        return out
    if st.exists_on_base is False:
        thing = "file" if u.kind == LOAD_ERROR else "test's file"
        out.update(verdict=NEW, reason=f"New on head (the {thing} does not exist on base) and {what} on head "
                                       f"({len(hf)}/{h}) — introduced by this change.")
        if henv:
            out["env_suspect"] = True
            out["env_class"] = henv
        return out
    if u.kind == LOAD_ERROR and st.exists_on_base:
        out.update(verdict=NEW, reason=f"Exists on base and loads there (no error), but {what} on head "
                                       f"({len(hf)}/{h}) — this change broke it.")
        if henv:
            out["env_suspect"] = True
            out["env_class"] = henv
        return out
    if st.mentioned_on_base is False:
        out.update(verdict=NEW, reason=f"Test added on head (not present on base) and fails on head "
                                       f"({len(hf)}/{h}) — introduced by this change.")
        return out
    out.update(verdict=UNKNOWN, reason="The test exists on base but did not run there (no result), so there is "
                                       "nothing to compare against.")
    return out
