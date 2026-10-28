"""Mixed results are NEW (or UNKNOWN), never ENVIRONMENT/PRE-EXISTING/FLAKY; jest-family commands
(`react-scripts test`, `craco test`) are read per test; check output with notices around it; children are
non-interactive. Deterministic: a small fake `react-scripts`/`jest` (Python) prints jest's --json report."""
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path

from _support import commit, git_repo, mod, rmtree, run_cli, write

m = mod("model")
d = mod("decide")
report = mod("report")
hook = mod("hook")
runners = mod("runners")
checks = mod("runners.checks")
procs = mod("procs")

# --------------------------------------------------------------------------------------------- fake jest
SHIM = r'''#!/usr/bin/env python3
"""Fake jest / react-scripts test: results from jest-spec.json in the cwd ({file: {test: "pass" | "fail:msg"}})."""
import json, os, re, sys, time
args = sys.argv[1:]
if args[:1] == ["test"]:
    args = args[1:]
with open(os.environ.get("SHIM_LOG", os.devnull), "a") as f:
    f.write(json.dumps({"argv": args, "CI": os.environ.get("CI")}) + "\n")
if "--watch" in args or "--watchAll" in args or ("--watchAll=false" not in args and os.environ.get("CI") != "true"):
    time.sleep(600)  # what react-scripts does without CI=true: watch mode, never exits
out = next((a.split("=", 1)[1] for a in args if a.startswith("--outputFile=")), None)
pat = args[args.index("-t") + 1] if "-t" in args else None
paths = []
if "--runTestsByPath" in args:
    i = args.index("--runTestsByPath") + 1
    while i < len(args) and not args[i].startswith("-"):
        paths.append(os.path.normpath(args[i])); i += 1
spec = json.load(open("jest-spec.json"))
results, failed = [], False
print("console.error\n    Warning: An update to Foo inside a test was not wrapped in act(...)")
for f, tests in sorted(spec.items()):
    if paths and os.path.normpath(f) not in paths:
        continue
    ars = []
    for t, st in tests.items():
        if pat and not re.search(pat, t):
            continue
        ok = st == "pass"
        failed |= not ok
        ars.append({"ancestorTitles": [], "title": t, "fullName": t, "status": "passed" if ok else "failed",
                    "failureMessages": [] if ok else [st[5:] + "\n    at Object.<anonymous> (" + f + ":3:1)"]})
    if not ars:
        continue
    bad = any(a["status"] == "failed" for a in ars)
    print(("FAIL " if bad else "PASS ") + f)
    results.append({"name": os.path.abspath(f), "status": "failed" if bad else "passed", "message": "",
                    "assertionResults": ars})
print("Tests:       %d failed, %d total" % (sum(a["status"] == "failed" for r in results for a in r["assertionResults"]),
                                           sum(len(r["assertionResults"]) for r in results)))
if out:
    json.dump({"testResults": results}, open(out, "w"))
sys.exit(1 if failed else 0)
'''


def cra_repo(base_spec, extra_files=None, scripts=None, deps=None):
    files = {"package.json": json.dumps({"scripts": scripts or {"test": "react-scripts test"},
                                         "dependencies": deps or {"react-scripts": "5.0.1"}}),
             ".gitignore": "node_modules\n", "jest-spec.json": json.dumps(base_spec)}
    for f in base_spec:
        files[f] = "test('x', () => {});\n"
    files.update(extra_files or {})
    repo = git_repo(files)
    for tool in ("react-scripts", "craco", "jest"):
        p = repo / "node_modules" / ".bin" / tool
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(SHIM)
        p.chmod(0o755)
    return repo


def set_head(repo, spec, files=None):
    write(repo, {"jest-spec.json": json.dumps(spec), **(files or {})})
    commit(repo, "head")


def check(repo, *cmd, env=None, extra=()):
    e = dict(os.environ, **(env or {}))
    e.pop("CI", None)
    p = run_cli(["check", "--json", "--base", "main~1", *extra, "--", *cmd], repo, env=e)
    try:
        return p, json.loads(p.stdout)
    except ValueError:
        raise AssertionError(p.stdout + p.stderr)


def verdicts(rep):
    return {v["id"]: v["verdict"] for v in rep["results"]}


class CraJestTest(unittest.TestCase):
    """`react-scripts test` / `craco test` / `npm test` -> the jest runner, per test, never watch mode."""

    def setUp(self):
        base = {"src/a.test.js": {"adds": "pass", "old bug": "fail:Error: expect(received).toBe(expected) 1 2"},
                "src/b.test.js": {"bills": "pass", "db": "fail:Error: connect ECONNREFUSED 127.0.0.1:5432"}}
        self.repo = cra_repo(base)
        set_head(self.repo, {"src/a.test.js": {"adds": "fail:Error: expect(received).toBe(expected) 3 4",
                                               "old bug": "fail:Error: expect(received).toBe(expected) 1 2"},
                             "src/b.test.js": {"bills": "pass", "db": "fail:Error: connect ECONNREFUSED 127.0.0.1:5432",
                                               "new one": "fail:Error: connect ECONNREFUSED 127.0.0.1:5432"}})
        self.log = self.repo.parent / (self.repo.name + "-shim.log")

    def tearDown(self):
        rmtree(self.repo)

    def assert_verdicts(self, rep):
        v = verdicts(rep)
        self.assertEqual(rep["runner"]["name"], "jest")
        self.assertEqual(v["src/a.test.js > adds"], "new")
        self.assertEqual(v["src/a.test.js > old bug"], "pre_existing")
        self.assertEqual(v["src/b.test.js > db"], "environment")
        # absent on base (its file ran there) and failing on head with an env-looking error: NEW, not ENVIRONMENT
        self.assertEqual(v["src/b.test.js > new one"], "new")

    def test_forms(self):
        for cmd in (["npm", "test"], ["npm", "run", "test"], ["react-scripts", "test"],
                    ["CI=true", "npx", "react-scripts", "test", "--watchAll=false"], ["npx", "react-scripts", "test"],
                    ["node_modules/.bin/craco", "test", "--watch"]):
            with self.subTest(cmd=cmd):
                t0 = time.time()
                if cmd[0] == "react-scripts":
                    cmd = ["node_modules/.bin/react-scripts"] + cmd[1:]
                p, rep = check(self.repo, *cmd, env={"SHIM_LOG": str(self.log)})
                self.assertLess(time.time() - t0, 120)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assert_verdicts(rep)
        calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertTrue(all(c["CI"] == "true" and "--watchAll=false" in c["argv"] and "--watch" not in c["argv"]
                            for c in calls), calls[:3])
        # reruns are only the failing tests: by path, with an escaped name pattern
        self.assertTrue(any("--runTestsByPath" in c["argv"] and "-t" in c["argv"] for c in calls))

    def test_text_report_names_tests(self):
        e = dict(os.environ)
        e.pop("CI", None)
        p = run_cli(["check", "--base", "main~1", "--", "CI=true react-scripts test --watchAll=false"], self.repo,
                    env=dict(e, PATH=str(self.repo / "node_modules" / ".bin") + os.pathsep + e["PATH"]))
        self.assertIn("src/a.test.js > adds", p.stdout)
        self.assertNotIn("unrecognised command", p.stdout)
        self.assertEqual(p.returncode, 1)


class CraDetectTest(unittest.TestCase):
    def test_auto_detect_prefers_test_script_and_notes_others(self):
        repo = cra_repo({"src/a.test.js": {"adds": "pass"}}, {"jest.api.config.js": "module.exports = {};\n"},
                        scripts={"test": "react-scripts test", "test:api": "jest --config jest.api.config.js"})
        try:
            found = runners.auto_detect(repo)
            self.assertEqual(found[0].runner, "jest")
            self.assertEqual([os.path.basename(x) for x in found[0].argv], ["react-scripts", "test"])
            self.assertIn("jest.api.config.js", found[0].note)
            p = run_cli(["detect"], repo)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("jest", p.stdout)
        finally:
            rmtree(repo)

    def test_jest_config_without_direct_dependency(self):
        repo = git_repo({"package.json": json.dumps({"jest": {"testEnvironment": "node"}})})
        try:
            self.assertEqual(runners.auto_detect(repo)[0].runner, "jest")
        finally:
            rmtree(repo)
        repo = git_repo({"package.json": "{}", "jest.config.js": "module.exports = {};\n"})
        try:
            self.assertEqual(runners.auto_detect(repo)[0].runner, "jest")
        finally:
            rmtree(repo)

    def test_from_command_forms(self):
        p = Path(git_repo({"package.json": json.dumps({"scripts": {
            "test": "CI=true react-scripts test --watchAll=false", "t2": "craco test && echo done"}})}))
        try:
            # `npm run t2` ("craco test && echo done") is a chain now (was: jest, its first part only)
            self.assertEqual(runners.from_command(["npm", "run", "t2"], p).runner, "chain")
            for argv, env in ((["npm", "test"], {"CI": "true"}),
                              (["CI=true react-scripts test --watchAll=false"], {"CI": "true"}),
                              (["yarn", "react-scripts", "test"], {}), (["pnpm", "exec", "craco", "test"], {})):
                det = runners.from_command(argv, p)
                self.assertEqual((det.runner, det.env), ("jest", env), argv)
            r = runners.make_runner(runners.from_command(["npx", "react-scripts", "test", "--watch"], p), p)
            self.assertEqual(r.prefix, ["npx", "react-scripts", "test"])
            self.assertNotIn("--watch", r.user_opts)
        finally:
            rmtree(p)


# --------------------------------------------------------------------------------------------- whole commands
def F(key, env=None, fails=None):
    return m.Obs(m.FAIL, key, key, key, env, fails=fails)


ENV = {"class": "missing_module", "detail": "package 'x' is not installed"}


def cmd_state(head, base):
    return d.UnitState(m.Unit("./t.sh", m.COMMAND, "./t.sh", "./t.sh"), list(head), list(base), exists_on_base=True)


class WholeCommandDecideTest(unittest.TestCase):
    def test_extra_failing_file_on_head_is_new(self):
        h = [F("x", ENV, ["FAIL src/a.test.js", "FAIL src/new.test.js"])] * 2
        b = [F("x", ENV, ["FAIL src/a.test.js"])] * 2
        v = d.decide(cmd_state(h, b))
        self.assertEqual(v["verdict"], m.NEW, v)
        self.assertEqual(v["head_only_fails"], ["FAIL src/new.test.js"])

    def test_env_text_both_sides_is_unknown_never_environment(self):
        for fails in (None, ["FAIL src/a.test.js"]):
            v = d.decide(cmd_state([F("x", ENV, fails)] * 2, [F("x", ENV, fails)]))
            self.assertEqual(v["verdict"], m.UNKNOWN, v)

    def test_extra_file_in_one_head_run_only_is_a_hint(self):
        h = [F("x", None, ["FAIL a", "FAIL b"]), F("x", None, ["FAIL a"])]
        v = d.decide(cmd_state(h, [F("x", None, ["FAIL a"])]))
        self.assertEqual(v["verdict"], m.UNKNOWN)
        self.assertIn("FAIL b", v["reason"])

    def test_flaky_base_is_not_flaky(self):
        v = d.decide(cmd_state([F("x")] * 2, [F("x"), m.Obs(m.PASS)]))
        self.assertEqual(v["verdict"], m.UNKNOWN)

    def test_runs_per_side(self):
        pol = d.Policy.from_runs(5)
        st = cmd_state([F("x", None, ["FAIL a"])], [F("x", None, ["FAIL a"])])
        self.assertEqual(d.needs_base(st, pol), 0)  # one base run answers 'fails on both sides'
        st = cmd_state([F("x", None, ["FAIL a", "FAIL b"])] * 2, [F("x", None, ["FAIL a"])])
        self.assertEqual(d.needs_base(st, pol), 1)  # NEW needs a second base run

    def test_per_test_env_only_with_the_same_error_on_base(self):
        u = m.Unit("t > x", m.TEST, "t", "x")
        v = d.decide(d.UnitState(u, [F("a", ENV)] * 3, [F("b", ENV)] * 3))
        self.assertEqual(v["verdict"], m.UNKNOWN)
        v = d.decide(d.UnitState(u, [F("a", ENV)] * 3, [F("a", ENV)] * 3))
        self.assertEqual(v["verdict"], m.ENVIRONMENT)
        v = d.decide(d.UnitState(u, [F("a", ENV)] * 3, [], exists_on_base=False))
        self.assertEqual(v["verdict"], m.NEW)


JEST_LIKE = """console.error
    Warning: An update to Foo inside a test was not wrapped in act(...)
    Error: connect ECONNREFUSED 127.0.0.1:5432 (from a passing test's log)
 FAIL  src/lib/old.test.js (5.2 s)
  ● old › keeps the total
    expect(received).toBe(expected)
    Expected: 4
    Received: 5
{extra}Test Suites: {n} failed, 230 passed, 234 total
Tests:       {n} failed, 2300 passed, 2333 total
"""


class WholeCommandOutputTest(unittest.TestCase):
    def test_fail_units_and_proof_lines(self):
        text = JEST_LIKE.format(extra=" FAIL  src/lib/new.test.js\n", n=2)
        self.assertEqual(checks.fail_units(text, ["/r"]), ["FAIL src/lib/new.test.js", "FAIL src/lib/old.test.js"])
        el = checks.error_lines(text)
        self.assertTrue(el["first"][0].startswith("FAIL"), el)
        self.assertTrue(any(ln.startswith("Tests:") for ln in el["last"]), el)
        self.assertFalse(any("console.error" in ln or "act(" in ln for ln in el["first"]), el)
        self.assertEqual(checks.fail_units("--- FAIL: TestX (0.01s)\nFAIL\texample.com/m\t0.2s\n", []),
                         ["--- FAIL: TestX", "FAIL example.com/m"])
        self.assertIsNone(checks.fail_units("all good\n", []))

    def _repo(self, head_extra):
        # an unrecognised wrapper around a jest-like suite; both sides fail and print environment-looking text
        script = ("#!/bin/sh\ncat out.txt\necho \"Error: Cannot find module 'missing-pkg' from 'src/x.js'\" >&2\n"
                  "exit 1\n")
        repo = git_repo({"t.sh": script, "out.txt": JEST_LIKE.format(extra="", n=1)})
        (repo / "t.sh").chmod(0o755)
        commit(repo, "x")
        write(repo, {"out.txt": JEST_LIKE.format(extra=head_extra, n=2)})
        commit(repo, "head")
        return repo

    def test_regression_extra_fail_file_on_head_is_new_not_environment(self):
        repo = self._repo(" FAIL  src/lib/invoice.test.js\n")
        try:
            p, rep = check(repo, "./t.sh")
            v = rep["results"][0]
            self.assertEqual(v["verdict"], "new", p.stdout)
            self.assertEqual(p.returncode, 1)
            self.assertEqual(v["id"], "./t.sh")
            txt = run_cli(["check", "--base", "main~1", "--", "./t.sh"], repo).stdout
            self.assertNotIn("ENVIRONMENT", txt)
            self.assertNotIn("No failure was caused", txt)
            self.assertIn("FAIL src/lib/invoice.test.js", txt)
        finally:
            rmtree(repo)

    def test_same_failing_files_both_sides_is_unknown(self):
        repo = self._repo("")
        try:
            p, rep = check(repo, "./t.sh")
            self.assertEqual(rep["results"][0]["verdict"], "unknown", p.stdout)
            self.assertEqual(p.returncode, 3)
            self.assertLessEqual(rep["test_runs"], 3)  # head twice (flaky?), base once
        finally:
            rmtree(repo)


class WordingTest(unittest.TestCase):
    def rep(self, results, check=False):
        s = {k: 0 for k in ("new", "pre_existing", "pre_existing_error_changed", "flaky", "environment", "unknown")}
        for v in results:
            s[v["verdict"]] += 1
        s.update(tests_total=0, tests_passed=0, tests_failed=0, tests_skipped=0, load_errors=0, check_errors=0)
        side = {"runs": 1, "failed": 1, "passed": 0, "error": "e", "error_raw": "e", "message": "e"}
        return {"summary": s, "results": [dict(v, head=side, base=side) for v in results],
                "base": {"short": "abc", "how": "x", "sha": "a"}, "head": {"short": "def", "dirty": False},
                "runner": {"name": "eslint" if check else "generic", "command": ["npx", "eslint", "src"],
                           "per_test": True, "check": check, "noun": "lint error"}}

    def test_agent_line_never_clears_unknown(self):
        r = self.rep([{"id": "x", "kind": "command", "verdict": "unknown", "reason": "r"},
                      {"id": "y", "kind": "test", "verdict": "environment", "reason": "r"}])
        self.assertNotIn("No failure was caused", report.agent_line(r))
        self.assertNotIn("None of these failures", hook.format_context(r, "cmd", "baseline"))

    def test_no_zero_errors_for_unreadable_check(self):
        r = self.rep([{"id": "npx eslint src", "kind": "command", "verdict": "unknown", "reason": "r"}], check=True)
        out = report.render(r)
        self.assertNotIn("0 lint errors", out)
        self.assertIn("could not be read", out)


# --------------------------------------------------------------------------------------------- eslint noise
NOTICE = "Browserslist: caniuse-lite is outdated. Please run:\n  npx update-browserslist-db@latest\n"


def eslint_json(errors):
    return json.dumps([{"filePath": "/r/src/" + f, "messages": [{"ruleId": r, "severity": 2, "message": msg}]}
                       for f, r, msg in errors])


class EslintNoiseTest(unittest.TestCase):
    def test_json_with_notice_before_and_after(self):
        js = eslint_json([("a.js", "no-undef", "'x' is not defined."), ("b.js", "no-unused-vars", "'y' unused")])
        for text in (NOTICE + js, js + "\n" + NOTICE, "npm warn config x\n" + NOTICE + js + "\n" + NOTICE):
            got = checks.parse_eslint(text)
            self.assertEqual(len(got), 2, text[:80])
        self.assertEqual(checks.parse_eslint(NOTICE + "[]\n"), [])

    def test_tsc_and_prettier_ignore_notices(self):
        self.assertEqual(len(checks.parse_tsc(NOTICE + "src/a.ts(1,2): error TS2322: bad\n" + NOTICE)), 1)
        self.assertEqual(len(checks.parse_prettier(NOTICE + "[warn] src/a.js\n[warn] Code style issues found\n")), 1)

    def test_end_to_end_with_stderr_notice(self):
        tool = ("#!/bin/sh\nprintf '%s\\n' \"Browserslist: caniuse-lite is 7 months old. Please run:\" >&2\n"
                "cat lint.json\nprintf 'Browserslist: caniuse-lite is outdated\\n'\nexit 1\n")
        base = [("a.js", "no-undef", "'x' is not defined.")]
        repo = git_repo({"bin/eslint": tool, "lint.json": eslint_json(base)})
        (repo / "bin" / "eslint").chmod(0o755)
        commit(repo, "x")
        write(repo, {"lint.json": eslint_json(base + [("c.js", "eqeqeq", "Expected '===' and instead saw '=='.")])})
        commit(repo, "head")
        try:
            p, rep = check(repo, "bin/eslint", "src")
            self.assertEqual(sorted(verdicts(rep).values()), ["new", "pre_existing"], p.stdout)
            self.assertEqual(rep["summary"]["check_errors"], 2)
            self.assertEqual(p.returncode, 1)
        finally:
            rmtree(repo)

    def test_unreadable_failure_is_named_by_command(self):
        repo = git_repo({"bin/eslint": "#!/bin/sh\necho 'Oops! Something went wrong!'\nexit 2\n"})
        (repo / "bin" / "eslint").chmod(0o755)
        commit(repo, "x")
        try:
            p = run_cli(["check", "--base", "HEAD", "--", "bin/eslint", "src"], repo)
            self.assertEqual(p.returncode, 3, p.stdout)
            self.assertNotIn("(command)", p.stdout)
            self.assertIn("bin/eslint src", p.stdout)
            self.assertNotIn("0 lint errors", p.stdout)
        finally:
            rmtree(repo)


# --------------------------------------------------------------------------------------------- non-interactive
class NonInteractiveTest(unittest.TestCase):
    def test_child_env_and_stdin(self):
        r = procs.run([sys.executable, "-c", "import os,sys; print(os.environ['CI'], repr(sys.stdin.read()))"],
                      ".", env=dict(os.environ, CI="false"), timeout=20)
        self.assertEqual(r.stdout.strip(), "true ''")

    def test_watcher_and_stdin_reader_do_not_hang(self):
        script = ("#!/bin/sh\nread answer\nif [ \"$CI\" != \"true\" ]; then sleep 300; fi\n"
                  "echo 'FAIL src/a.test.js'\nexit 1\n")
        repo = git_repo({"t.sh": script})
        (repo / "t.sh").chmod(0o755)
        try:
            t0 = time.time()
            p = run_cli(["check", "--timeout", "60", "--base", "HEAD", "--", "./t.sh"], repo,
                        env=dict(os.environ, CI="0"))
            self.assertLess(time.time() - t0, 50)
            self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        finally:
            rmtree(repo)


# --------------------------------------------------------------------------------------------- hook
def payload(cmd, cwd, event="PostToolUseFailure", out=""):
    return {"hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd),
            "session_id": "mixed-" + str(time.time()), "tool_response": {"stdout": out}, "error": out}


class HookFormsTest(unittest.TestCase):
    FORMS = [
        ("npx jest", ["npx", "jest"], "jest"),
        ("npx jest --config jest.api.config.js 2>&1 | tail -30", ["npx", "jest", "--config", "jest.api.config.js"],
         "jest"),
        ("npx vitest run", ["npx", "vitest", "run"], "vitest"),
        ("CI=true npx react-scripts test --watchAll=false 2>&1 | tail -40",
         ["npx", "react-scripts", "test", "--watchAll=false"], "jest"),
        ("react-scripts test --watchAll=false", ["react-scripts", "test", "--watchAll=false"], "jest"),
        ("npx craco test", ["npx", "craco", "test"], "jest"),
        ("yarn react-scripts test", ["yarn", "react-scripts", "test"], "jest"),
        ("pnpm exec jest", ["pnpm", "exec", "jest"], "jest"),
        ("CI=true npm test", ["npm", "test"], "npm-test"),
        ("npm run test -- --watchAll=false | tail", ["npm", "run", "test", "--", "--watchAll=false"], "npm-test"),
    ]

    def test_forms(self):
        for cmd, argv, kind in self.FORMS:
            seg = hook.find_test_segment(cmd, "/w")
            self.assertIsNotNone(seg, cmd)
            self.assertEqual((seg.argv, seg.kind), (argv, kind), cmd)
        self.assertEqual(hook.find_test_segment("CI=true npx react-scripts test", "/w").env, {"CI": "true"})
        self.assertIsNone(hook.find_test_segment("npx react-scripts start", "/w"))

    def test_npm_script_body_with_react_scripts(self):
        repo = git_repo({"package.json": json.dumps({"scripts": {"web": "react-scripts test"}})})
        try:
            self.assertEqual(hook.find_test_segment("npm run web", str(repo)).kind, "npm-test")
        finally:
            rmtree(repo)


class HookEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.cache = Path(git_repo({}))  # scratch dir for session state
        self.repo = cra_repo({"src/a.test.js": {"adds": "pass"}})
        set_head(self.repo, {"src/a.test.js": {"adds": "fail:Error: expect(received).toBe(expected) 3 4"}})
        self.env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)
        rmtree(self.repo)
        rmtree(self.cache)

    def test_npx_and_cra_forms_get_verdicts(self):
        jest_out = "FAIL src/a.test.js\nTests:       1 failed, 1 total\n"
        for cmd, event in (("npx jest", "PostToolUseFailure"),
                           ("CI=true npx react-scripts test --watchAll=false 2>&1 | tail -40", "PostToolUse"),
                           ("npm test", "PostToolUseFailure")):
            with self.subTest(cmd=cmd):
                ctx = hook.handle(payload(cmd, self.repo, event, jest_out), session_dir=self.cache)
                self.assertIsNotNone(ctx, cmd)
                self.assertIn("src/a.test.js > adds", ctx)
                self.assertIn("NEW", ctx)
                os.environ.clear()
                os.environ.update(self.env)

    def test_never_silent_when_it_declines(self):
        e = dict(os.environ)
        os.environ["PATH"] = "/usr/bin:/bin"
        try:
            ctx = hook.handle(payload("jest --ci", self.repo), session_dir=self.cache)
        finally:
            os.environ.clear()
            os.environ.update(e)
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.startswith("[" + mod("brand").NAME + "] No verdict"), ctx)
        self.assertIn("check -- jest --ci", ctx)


# --------------------------------------------------------------------------------------------- --strict-changed
def _have_pytest():
    import importlib.util
    return importlib.util.find_spec("pytest") is not None


@unittest.skipUnless(_have_pytest(), "pytest not installed")
class StrictChangedTest(unittest.TestCase):
    def test_error_changed_fails_only_with_strict_changed(self):
        repo = git_repo({"tests/test_a.py": "def test_a():\n    assert 5 == 7\n"})
        write(repo, {"tests/test_a.py": "def test_a():\n    assert 6 == 7\n"})
        commit(repo, "head")
        try:
            for extra, code in (((), 0), (("--strict-changed",), 1)):
                p, rep = check(repo, sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", extra=extra)
                self.assertEqual(verdicts(rep), {"tests/test_a.py::test_a": "pre_existing_error_changed"}, p.stdout)
                self.assertEqual(p.returncode, code, extra)
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
