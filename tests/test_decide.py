"""Table-driven tests of the verdict logic and the sampling policy."""
import unittest

from _support import mod

m = mod("model")
d = mod("decide")

ENV_MOD = {"class": "missing_module", "detail": "Python module 'x' is not installed"}
ENV_VAR = {"class": "missing_env_var", "detail": "environment variable X_URL is not set"}


def F(key="AssertionError: assert 1 == 2", env=None):
    return m.Obs(m.FAIL, key, key, key, env)


def P():
    return m.Obs(m.PASS)


def S(status):
    return m.Obs(status)


def B(key="ModuleNotFoundError: No module named 'gone'"):
    return m.Obs(m.BLOCKED, "[file failed to load] " + key, key, key)


def state(head, base, kind=m.TEST, **kw):
    u = m.Unit("tests/test_a.py::test_x" if kind == m.TEST else "tests/test_a.py", kind, "tests/test_a.py",
               "test_x", {"file": "tests/test_a.py", "rest": "::test_x"})
    return d.UnitState(u, list(head), list(base), **kw)


CASES = [
    # name, head, base, extra state kwargs, expected verdict, extra expectations
    ("flaky on head beats everything", [F(), P()], [P()] * 5, {}, m.FLAKY, {"flaky_side": "head"}),
    ("flaky on head even if base fails", [F(), F(), P()], [F()] * 5, {}, m.FLAKY, {}),
    ("never failed again on rerun", [P()], [], {}, m.FLAKY, {}),
    ("passes on base -> NEW", [F()] * 5, [P()] * 5, {}, m.NEW, {"env_suspect": False}),
    ("NEW with env-looking head error", [F("ModuleNotFoundError: x", ENV_MOD)] * 5, [P()] * 5, {}, m.NEW,
     {"env_suspect": True}),
    ("same error on base -> PRE", [F()] * 5, [F()] * 5, {}, m.PRE, {"match": "exact"}),
    ("different error on base -> PRE changed", [F("AssertionError: assert 6 == 7")] * 5,
     [F("AssertionError: assert 5 == 7")] * 5, {}, m.PRE_CHANGED, {}),
    ("assertion values matter", [F("AssertionError: assert 1.56 == 1.58")] * 3,
     [F("AssertionError: assert 1.57 == 1.58")] * 3, {}, m.PRE_CHANGED, {}),
    ("noisy text both sides matches loosely", [F("assert 0.31 > 0.5"), F("assert 0.12 > 0.5")],
     [F("assert 0.44 > 0.5"), F("assert 0.2 > 0.5")], {}, m.PRE, {"match": "loose"}),
    ("noise on one side only does not excuse a real change", [F("assert 6 == 7")] * 3,
     [F("assert 5 == 7")] * 3, {}, m.PRE_CHANGED, {}),
    ("env error on both sides -> ENVIRONMENT", [F("KeyError: 'X_URL'", ENV_VAR)] * 5,
     [F("KeyError: 'X_URL'", ENV_VAR)] * 5, {}, m.ENVIRONMENT, {}),
    ("different env errors -> UNKNOWN (env only counts with the same error on base)",
     [F("KeyError: 'X_URL'", ENV_VAR)] * 2, [F("ModuleNotFoundError: x", ENV_MOD)] * 2, {}, m.UNKNOWN, {}),
    ("env only on base: base proves nothing", [F()] * 5, [F("ModuleNotFoundError: x", ENV_MOD)] * 5, {},
     m.UNKNOWN, {}),
    ("env only on head, base fails differently", [F("ModuleNotFoundError: x", ENV_MOD)] * 3, [F()] * 3, {},
     m.PRE_CHANGED, {"env_suspect": True}),
    ("flaky on base", [F()] * 5, [P(), F(), P()], {}, m.FLAKY, {"flaky_side": "base"}),
    ("base untrusted", [F()] * 5, [], {"base_problem": "base run crashed"}, m.UNKNOWN, {}),
    ("base untrusted wins over evidence", [F()] * 5, [P()] * 3, {"base_problem": "timeout"}, m.UNKNOWN, {}),
    ("file blocked on base", [F()] * 3, [B()] * 3, {}, m.PRE_CHANGED, {}),
    ("test file new on head", [F()] * 5, [S(m.ABSENT)], {"exists_on_base": False}, m.NEW, {}),
    ("test added to existing file", [F()] * 5, [S(m.ABSENT)], {"exists_on_base": True,
                                                                "mentioned_on_base": False}, m.NEW, {}),
    ("test exists on base but did not run", [F()] * 5, [S(m.ABSENT)], {"exists_on_base": True,
                                                                        "mentioned_on_base": True}, m.UNKNOWN, {}),
    ("skipped on base", [F()] * 5, [S(m.SKIP)], {"exists_on_base": True}, m.UNKNOWN, {}),
    ("no base data at all", [F()] * 5, [], {}, m.UNKNOWN, {}),
]

LOAD_CASES = [
    ("load error, loads on base -> NEW", [F("ModuleNotFoundError: No module named 'shop.report'")] * 5,
     [P()] * 5, {"blocked_tests": ["tests/test_a.py::test_x"]}, m.NEW, {}),
    ("load error, same on base -> PRE", [F("SyntaxError: invalid syntax")] * 5, [F("SyntaxError: invalid syntax")] * 5,
     {}, m.PRE, {}),
    ("load error changed", [F("SyntaxError: a")] * 5, [F("SyntaxError: b")] * 5, {}, m.PRE_CHANGED, {}),
    ("load error, env both sides", [F("ModuleNotFoundError: x", ENV_MOD)] * 5,
     [F("ModuleNotFoundError: x", ENV_MOD)] * 5, {}, m.ENVIRONMENT, {}),
    ("load error, new file", [F("ImportError: x")] * 5, [S(m.ABSENT)], {"exists_on_base": False}, m.NEW, {}),
    ("load error, file on base with no tests and no error", [F("ImportError: x")] * 5, [S(m.ABSENT)],
     {"exists_on_base": True}, m.NEW, {}),
    ("load error flaky", [F("ImportError: x"), P()], [], {}, m.FLAKY, {}),
]


class DecideTableTest(unittest.TestCase):
    def run_cases(self, cases, kind):
        for name, head, base, kw, want, extra in cases:
            with self.subTest(name):
                v = d.decide(state(head, base, kind, **kw))
                self.assertEqual(v["verdict"], want, f"{name}: {v['reason']}")
                for k, val in extra.items():
                    self.assertEqual(v[k], val, name)
                self.assertTrue(v["reason"].endswith("."), v["reason"])

    def test_tests(self):
        self.run_cases(CASES, m.TEST)

    def test_load_errors(self):
        self.run_cases(LOAD_CASES, m.LOAD_ERROR)

    def test_evidence_fields(self):
        v = d.decide(state([F("AssertionError: assert 6 == 7")] * 5, [F("AssertionError: assert 5 == 7")] * 4))
        self.assertEqual((v["head"]["failed"], v["head"]["runs"], v["base"]["failed"], v["base"]["runs"]),
                         (5, 5, 4, 4))
        self.assertEqual(v["runs_total"], 9)
        self.assertEqual(v["head"]["error"], "AssertionError: assert 6 == 7")
        self.assertEqual(v["base"]["error"], "AssertionError: assert 5 == 7")
        self.assertIn("similarity", v)
        nv = d.decide(state([F()] * 5, [P()] * 5, m.LOAD_ERROR, blocked_tests=["a", "b"]))
        self.assertEqual(nv["blocked_tests"], ["a", "b"])
        self.assertIn("2 test(s)", nv["reason"])

    def test_flaky_is_never_new(self):
        # exhaustive over small sequences: any head pass => never NEW
        import itertools
        for hseq in itertools.product("FP", repeat=4):
            if "F" not in hseq or "P" not in hseq:
                continue
            for bseq in ("PPPPP", "FFFFF", "PFPFP", ""):
                h = [F() if c == "F" else P() for c in hseq]
                b = [F() if c == "F" else P() for c in bseq]
                self.assertEqual(d.decide(state(h, b))["verdict"], m.FLAKY)


class PolicyTest(unittest.TestCase):
    pol = d.Policy.from_runs(5)

    def test_defaults(self):
        self.assertEqual((self.pol.runs, self.pol.base_required, self.pol.base_runs, self.pol.max_runs),
                         (5, 5, 10, 15))
        p = d.Policy.from_runs(3, base_runs=4)
        self.assertEqual((p.base_required, p.base_runs), (4, 4))

    def test_head_stops_on_first_pass_and_after_budget(self):
        st = state([F()], [])
        self.assertEqual(d.needs_head(st, self.pol), 4)
        st.head = [F(), P()]
        self.assertEqual(d.needs_head(st, self.pol), 0)
        st.head = [F()] * 5
        self.assertEqual(d.needs_head(st, self.pol), 0)

    def test_base_required_and_optional(self):
        st = state([F()] * 5, [F()], closure_changed=True)
        self.assertEqual(d.needs_base(st, self.pol), 4)
        self.assertEqual(d.wants_base(st, self.pol), 9)
        st.base = [F()] * 5
        self.assertEqual(d.needs_base(st, self.pol), 0)
        self.assertEqual(d.wants_base(st, self.pol), 5)
        st.base = [F(), P()]
        self.assertEqual((d.needs_base(st, self.pol), d.wants_base(st, self.pol)), (0, 0))

    def test_no_base_runs_for_head_flaky(self):
        st = state([F(), P()], [])
        self.assertEqual(d.needs_base(st, self.pol), 0)

    def test_new_with_unknown_closure_requires_confirmation(self):
        st = state([F()] * 5, [P()], closure_changed=None)
        self.assertEqual(d.needs_base(st, self.pol), 9)

    def test_suspicious_new_escalates_both_sides(self):
        st = state([F()] * 5, [P()] * 5, closure_changed=False)
        self.assertTrue(d.suspicious(st))
        self.assertEqual(d.needs_base(st, self.pol), 10)
        self.assertEqual(d.needs_head(st, self.pol), 10)
        changed = state([F()] * 5, [P()] * 5, closure_changed=True)
        self.assertFalse(d.suspicious(changed))

    def test_noisy_failure_text_is_suspicious(self):
        st = state([F("assert 0.1 > 0.5"), F("assert 0.3 > 0.5")], [F("assert 0.2 > 0.5")], closure_changed=True)
        self.assertTrue(d.suspicious(st))

    def test_pre_with_unchanged_closure_is_not_suspicious(self):
        st = state([F()] * 5, [F()] * 5, closure_changed=False)
        self.assertFalse(d.suspicious(st))

    def test_absent_on_base_needs_no_more_runs(self):
        st = state([F()] * 5, [S(m.ABSENT)], exists_on_base=False)
        self.assertEqual(d.needs_base(st, self.pol), 0)

    def test_session_failure_is_cheap(self):
        u = m.Unit("(test session)", m.LOAD_ERROR, "(test session)")
        st = d.UnitState(u, [F()], [])
        self.assertEqual(d.needs_head(st, self.pol), 1)
        self.assertEqual(d.needs_base(st, self.pol), 2)

    def test_fixed_needs_more_evidence_when_nothing_it_depends_on_changed(self):
        st = state([P()], [F()], closure_changed=False)
        self.assertEqual(d.needs_head_fixed(st, self.pol), 14)
        self.assertEqual(d.needs_base_fixed(st, self.pol), 14)

    def test_fixed_confirmation(self):
        st = state([P()], [F()], closure_changed=True)
        self.assertEqual(d.needs_head_fixed(st, self.pol), 4)
        self.assertEqual(d.needs_base_fixed(st, self.pol), 4)
        st.head = [P()] * 5
        st.base = [F()] * 5
        self.assertEqual(d.fixed_verdict(st), "fixed")
        st.base.append(P())
        self.assertEqual(d.fixed_verdict(st), "flaky")
        self.assertEqual(d.needs_base_fixed(st, self.pol), 0)


if __name__ == "__main__":
    unittest.main()
