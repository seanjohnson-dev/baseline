"""The Claude Code hook: test-command detection, output format/caps, guards, dedup, timeout, never crashing."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _support import LAUNCHER, PKG, commit, git_repo, git_state, have_pytest, mod, rmtree, sh, write

hook = mod("hook")
brand = mod("brand")
CLI = brand.CLI_NAME
P = brand.ENV_PREFIX

# (command, expected argv of the test segment or None, expected cwd suffix relative to /w or None)
DETECT = [
    ("pytest", ["pytest"], ""),
    ("pytest -q tests/test_a.py", ["pytest", "-q", "tests/test_a.py"], ""),
    ("python -m pytest -q -p no:cacheprovider", ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"], ""),
    ("python3 -m pytest", ["python3", "-m", "pytest"], ""),
    (".venv/bin/pytest -x", [".venv/bin/pytest", "-x"], ""),
    ("py.test tests", ["py.test", "tests"], ""),
    ("python -m unittest discover -s tests", ["python", "-m", "unittest", "discover", "-s", "tests"], ""),
    ("python manage.py test", ["python", "manage.py", "test"], ""),
    ("cd sub && pytest -q", ["pytest", "-q"], "sub"),
    ("cd sub; cd deeper && npx vitest run", ["vitest", "run"], "sub/deeper"),
    ("(cd sub && go test ./...)", ["go", "test", "./..."], "sub"),
    ("FOO=1 BAR=2 pytest", ["pytest"], ""),
    ("env FOO=1 pytest -q", ["pytest", "-q"], ""),
    ("timeout 60 pytest", ["pytest"], ""),
    ("timeout -s KILL 2m python -m pytest", ["python", "-m", "pytest"], ""),
    ("time go test ./...", ["go", "test", "./..."], ""),
    ("pytest -q 2>&1 | tail -20", ["pytest", "-q"], ""),
    ("go test ./... 2>&1 | tee log.txt", ["go", "test", "./..."], ""),
    ("pytest > out.txt", ["pytest"], ""),
    ("npm install && npm test", ["npm", "test"], ""),
    ("npm run test", ["npm", "run", "test"], ""),
    ("npm run test:unit -- --reporter dot", ["npm", "run", "test:unit", "--", "--reporter", "dot"], ""),
    ("npm t", ["npm", "t"], ""),
    ("yarn test", ["yarn", "test"], ""),
    ("pnpm test", ["pnpm", "test"], ""),
    ("pnpm vitest run", ["pnpm", "vitest", "run"], ""),
    ("npx vitest run test/a.test.js", ["vitest", "run", "test/a.test.js"], ""),
    ("npx jest --ci", ["jest", "--ci"], ""),
    ("npx --yes vitest", ["vitest"], ""),
    ("bun test", ["bun", "test"], ""),
    ("uv run pytest -q", ["pytest", "-q"], ""),
    ("poetry run python -m pytest", ["python", "-m", "pytest"], ""),
    ("bundle exec rspec spec/a_spec.rb", ["rspec", "spec/a_spec.rb"], ""),
    ("cargo test", ["cargo", "test"], ""),
    ("cargo nextest run", ["cargo", "nextest", "run"], ""),
    ("make test", ["make", "test"], ""),
    ("make -j4 test-unit", ["make", "-j4", "test-unit"], ""),
    ("tox -e py311", ["tox", "-e", "py311"], ""),
    ("source .venv/bin/activate && pytest", ["pytest"], ""),
    ("git status && pytest -q", ["pytest", "-q"], ""),
    ("echo start; pytest; echo done", ["pytest"], ""),
    ("pytest -k 'not slow and not db'", ["pytest", "-k", "not slow and not db"], ""),
    ("ls tests/ && pytest tests/test_x.py::test_y", ["pytest", "tests/test_x.py::test_y"], ""),
    # negatives
    ("npm install", None, None),
    ("npm run build", None, None),
    ("git status", None, None),
    ("ls tests/", None, None),
    ("cat test.py", None, None),
    ("echo pytest", None, None),
    ("pip install pytest", None, None),
    ("grep -r pytest .", None, None),
    ("go build ./...", None, None),
    ("go vet ./...", None, None),
    ("cargo build", None, None),
    ("make", None, None),
    ("make build", None, None),
    ("python script.py", None, None),
    ("python -m http.server", None, None),
    ("rm -rf node_modules", None, None),
    ("git commit -m 'fix pytest failures'", None, None),
    ("ls /nonexistent", None, None),
    ("", None, None),
    ("pytest 'unbalanced", None, None),
    ("cd - && pytest", None, None),
    ("pytest $(cat args.txt)", None, None),
    (f"{CLI} check -- pytest", None, None),
    (f"/opt/plugin/bin/{CLI} check -- pytest -q", None, None),
    (f"python3 -m {PKG} check", None, None),
    (f"cd x && {CLI} check", None, None),
    ("cat <<EOF | python -m pytest\nx\nEOF", None, None),
]


class DetectionTest(unittest.TestCase):
    def test_table(self):
        self.assertGreaterEqual(len(DETECT), 40)
        bad = []
        for cmd, argv, cwd in DETECT:
            seg = hook.find_test_segment(cmd, "/w")
            got = (seg.argv, os.path.relpath(seg.cwd, "/w").replace(".", "", 1) if seg.cwd == "/w"
                   else os.path.relpath(seg.cwd, "/w")) if seg else (None, None)
            want = (argv, cwd)
            if got != want:
                bad.append(f"{cmd!r}: got {got}, want {want}")
        self.assertEqual(bad, [])

    def test_env_prefix_and_venv(self):
        seg = hook.find_test_segment("FOO=1 timeout 5 env BAR=2 pytest", "/w")
        self.assertEqual(seg.env, {"FOO": "1", "BAR": "2"})
        seg = hook.find_test_segment("export DB=x && source venv/bin/activate && pytest", "/w")
        self.assertEqual(seg.env["DB"], "x")
        self.assertEqual(seg.env["VIRTUAL_ENV"], "/w/venv")
        self.assertTrue(seg.env["PATH"].startswith("/w/venv/bin" + os.pathsep))

    def test_npm_script_by_body(self):
        d = tempfile.mkdtemp()
        try:
            Path(d, "package.json").write_text(json.dumps({"scripts": {"ci": "vitest run", "build": "tsc"}}))
            self.assertIsNotNone(hook.find_test_segment("npm run ci", d))
            self.assertIsNone(hook.find_test_segment("npm run build", d))
        finally:
            rmtree(d)

    def test_output_says_failed(self):
        for text in ("6 failed, 7 passed, 1 error in 0.31s", "===== 1 failed, 11 passed in 0.3s =====",
                     "ERROR tests/test_r.py - ModuleNotFoundError", "Interrupted: 1 error during collection",
                     "      Tests  1 failed | 8 passed (9)", " Test Files  2 failed | 5 passed (7)",
                     "Tests:       2 failed, 10 passed, 12 total", "--- FAIL: TestX (0.00s)",
                     "FAIL\texample.com/shop/cart\t0.01s", "test result: FAILED. 3 passed; 1 failed",
                     "  2 failing", "10 examples, 2 failures", "FAILED (failures=1)"):
            self.assertTrue(hook.output_says_failed("noise\n" + text + "\nmore"), text)
        for text in ("12 passed in 0.31s", "      Tests  9 passed (9)", "ok  \texample.com/shop/cart\t0.01s",
                     "test result: ok. 3 passed; 0 failed", "10 examples, 0 failures", "OK", "", "On branch main"):
            self.assertFalse(hook.output_says_failed(text), text)

    def test_output_says_passed(self):
        self.assertTrue(hook.output_says_passed("Exit code 1\n===== 12 passed in 0.31s =====\n"))
        self.assertTrue(hook.output_says_passed(" Test Files  3 passed (3)\n      Tests  9 passed (9)\n"))
        self.assertFalse(hook.output_says_passed("==== 1 failed, 11 passed in 0.3s ===="))
        self.assertFalse(hook.output_says_passed("      Tests  1 failed | 8 passed (9)"))
        self.assertFalse(hook.output_says_passed("Exit code 1"))
        self.assertFalse(hook.output_says_passed(""))


def fake_report(n_new=2, n_pre=1, n_flaky=1, n_env=1, n_unknown=0, msg="AssertionError: assert 1 == 2"):
    def v(i, verdict, **kw):
        d = {"id": f"tests/test_m.py::test_{verdict}_{i}", "kind": "test", "verdict": verdict, "reason": "r",
             "head": {"runs": 5, "failed": 5, "passed": 0, "message": msg, "error": msg},
             "base": {"runs": 10, "failed": 0 if verdict == "new" else 10, "passed": 10 if verdict == "new" else 0,
                      "message": msg, "error": msg},
             "env_class": {"class": "missing_env_var", "detail": "environment variable X is not set"}
             if verdict == "environment" else None, "blocked_tests": [], "flaky_side": "head"}
        d.update(kw)
        return d
    results = [v(i, "new") for i in range(n_new)] + [v(i, "pre_existing") for i in range(n_pre)] + \
              [v(i, "flaky") for i in range(n_flaky)] + [v(i, "environment") for i in range(n_env)] + \
              [v(i, "unknown") for i in range(n_unknown)]
    summary = {"new": n_new, "pre_existing": n_pre, "flaky": n_flaky, "environment": n_env, "unknown": n_unknown,
               "pre_existing_error_changed": 0, "tests_total": 100}
    return {"summary": summary, "results": results, "base": {"short": "abc1234", "how": "merge-base of HEAD and main"},
            "head": {"short": "def5678", "dirty": True}, "test_runs": 50, "duration_s": 3.2}


class FormatTest(unittest.TestCase):
    def test_groups_guidance_and_evidence(self):
        text = hook.format_context(fake_report(), "pytest -q", "/p/bin/x")
        self.assertIn("NEW — caused by the current change (2):", text)
        self.assertIn("PRE-EXISTING — already failing on the base commit abc1234", text)
        self.assertIn("base abc1234 error: AssertionError: assert 1 == 2", text)
        self.assertIn("FLAKY", text)
        self.assertIn("ENVIRONMENT", text)
        self.assertIn("environment variable X is not set", text)
        self.assertIn("Fix the NEW failures", text)
        self.assertIn("cite base abc1234", text)
        self.assertNotIn("UNKNOWN means", text)  # no unknowns -> no unknown guidance
        self.assertIn("head def5678 + uncommitted changes", text)
        self.assertTrue(text.startswith(f"[{brand.NAME}]"))

    def test_no_new(self):
        text = hook.format_context(fake_report(n_new=0, n_unknown=1), "pytest", "/p/bin/x")
        self.assertIn("None of these failures was caused by the current change", text)
        self.assertIn("UNKNOWN means there is no trustworthy base result", text)

    def test_caps(self):
        long_msg = "AssertionError: " + "x" * 5000
        rep = fake_report(n_new=200, n_pre=150, n_flaky=80, n_env=60, n_unknown=40, msg=long_msg)
        text = hook.format_context(rep, "pytest " + "-k a " * 500, "/p/bin/x")
        self.assertLessEqual(len(text), hook.MAX_CONTEXT)
        self.assertIn("more (see the details)", text)
        self.assertIn("How to report these failures", text, "guidance must survive the cap")
        self.assertTrue(all(len(line) <= hook.LINE_MAX + 200 for line in text.splitlines()))


def run_hook(payload, env=None, timeout=120):
    e = dict(os.environ)
    e.pop(P + "IN_HOOK", None)
    e.pop("DATABASE_URL", None)
    e.update(env or {})
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, str(LAUNCHER), "hook", "post-tool-failure"], input=data, env=e,
                          capture_output=True, text=True, timeout=timeout)


def payload(command, cwd, sid="s1", **kw):
    d = {"session_id": sid, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
         "tool_input": {"command": command}, "cwd": str(cwd), "error": "Exit code 1"}
    d.update(kw)
    return d


class GuardsTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()
        self.env = {"XDG_CACHE_HOME": self.cache}

    def tearDown(self):
        rmtree(self.cache)

    def test_non_test_command_is_silent_and_fast(self):
        t = time.time()
        p = run_hook(payload("ls /nonexistent", "/"), self.env)
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertLess(time.time() - t, 2.0)

    def test_trace_log(self):
        log = Path(self.cache) / "trace.jsonl"
        run_hook(payload("ls /nonexistent", "/"), {**self.env, P + "HOOK_LOG": str(log)})
        rec = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual((rec["event"], rec["command"], rec["context_chars"]), ("PostToolUseFailure", "ls /nonexistent", 0))

    def test_lean_startup(self):
        code = (f"import sys, io, json; sys.path.insert(0, {str(LAUNCHER.parents[1])!r}); "
                f"sys.stdin = io.StringIO(json.dumps({{'tool_name': 'Bash', 'tool_input': {{'command': 'ls'}}, 'cwd': '/'}})); "
                f"from {PKG} import cli; cli.main(['hook', 'post-tool-failure']); "
                f"print(sorted(m for m in sys.modules if m.startswith('{PKG}.')))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        mods = out.stdout.strip()
        self.assertNotIn("engine", mods)
        self.assertNotIn("runners", mods)

    def test_recursion_guard_and_off_switch(self):
        repo = git_repo({"tests/test_a.py": "def test_a():\n    assert 0\n"})
        try:
            for env in ({P + "IN_HOOK": "1"}, {P + "HOOK": "off"}):
                p = run_hook(payload("python -m pytest", repo), {**self.env, **env})
                self.assertEqual((p.returncode, p.stdout), (0, ""), env)
            p = run_hook(payload(f"{CLI} check -- python -m pytest", repo), self.env)
            self.assertEqual(p.stdout, "")
        finally:
            rmtree(repo)

    def test_never_crashes(self):
        for bad in ("", "not json", "[1,2]", json.dumps({"tool_input": "x"}), json.dumps({"tool_input": {"command": 5}}),
                    json.dumps({"tool_input": {"command": "pytest"}, "cwd": "/definitely/not/here"}),
                    json.dumps({"tool_input": {"command": "pytest"}, "cwd": "/", "is_interrupt": True})):
            p = run_hook(bad, self.env)
            self.assertEqual((p.returncode, p.stdout), (0, ""), bad)

    def test_internal_error_is_swallowed(self):
        orig = hook.handle
        out = []
        try:
            hook.handle = lambda payload: 1 / 0
            import io
            old_in, sys.stdin = sys.stdin, io.StringIO("{}")
            try:
                out.append(hook.main(["post-tool-failure"]))
            finally:
                sys.stdin = old_in
        finally:
            hook.handle = orig
        self.assertEqual(out, [0])

    def test_not_a_git_repo_is_silent(self):
        d = tempfile.mkdtemp()
        try:
            self.assertIsNone(hook.handle(payload("pytest", d), session_dir=Path(self.cache)))
        finally:
            rmtree(d)

    def test_generic_runner_gets_one_line_note(self):
        repo = git_repo({"Makefile": "test:\n\tfalse\n"})
        try:
            ctx = hook.handle(payload("make test", repo), session_dir=Path(self.cache))
            self.assertIn("no verdict", ctx)
            self.assertEqual(len(ctx.splitlines()), 1)
        finally:
            rmtree(repo)

    def test_slow_command_note(self):
        repo = git_repo({"tests/test_a.py": "def test_a():\n    assert 0\n"})
        try:
            ctx = hook.handle(payload("python -m pytest", repo, duration_ms=500000), session_dir=Path(self.cache))
            self.assertIn("No verdict", ctx)
        finally:
            rmtree(repo)


class PostToolUseTest(unittest.TestCase):
    """A 'successful' Bash call can hide failing tests behind `| tail` or `; git status`."""

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())
        self.repo = git_repo({"tests/test_a.py": "def test_a():\n    assert 0\n"})
        self.calls = []
        self.orig = hook.run_check
        hook.run_check = lambda seg, budget: self.calls.append(seg.argv) or f"[{brand.NAME}] Per-test verdicts: x"

    def tearDown(self):
        hook.run_check = self.orig
        rmtree(self.cache)
        rmtree(self.repo)

    def ok(self, command, stdout):
        return {"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": str(self.repo),
                "tool_input": {"command": command}, "tool_response": {"stdout": stdout, "stderr": ""}}

    def test_piped_failures_are_attributed(self):
        ctx = hook.handle(self.ok("python -m pytest -q 2>&1 | tail -5; git status", "1 failed in 0.02s\n"),
                          session_dir=self.cache)
        self.assertTrue(ctx)
        self.assertEqual(self.calls, [["python", "-m", "pytest", "-q"]])

    def test_passing_or_non_test_output_is_ignored(self):
        self.assertIsNone(hook.handle(self.ok("python -m pytest -q | tail -3", "3 passed in 0.02s\n"),
                                      session_dir=self.cache))
        self.assertIsNone(hook.handle(self.ok("git log | head", "1 failed in 0.02s\n"), session_dir=self.cache))
        self.assertEqual(self.calls, [])

    def test_event_name_in_output(self):
        repo = self.repo
        p = run_hook(json.dumps(self.ok("python -m pytest -q | tail -3", "3 passed in 0.1s")),
                     {"XDG_CACHE_HOME": str(self.cache)})
        self.assertEqual(p.stdout, "")
        p = subprocess.run([sys.executable, str(LAUNCHER), "hook", "post-tool-use"], input=json.dumps(
            {"hook_event_name": "PostToolUseFailure", "tool_input": {"command": "pytest"}, "cwd": str(repo)}),
            capture_output=True, text=True)
        self.assertEqual((p.returncode, p.stdout), (0, ""), "event mismatch -> silent")


class DedupTest(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())
        self.repo = git_repo({"tests/test_a.py": "def test_a():\n    assert 0\n"})
        self.calls = []
        self.orig = hook.run_check
        hook.run_check = lambda seg, budget: self.calls.append(seg.argv) or f"[{brand.NAME}] Per-test verdicts: x"

    def tearDown(self):
        hook.run_check = self.orig
        rmtree(self.cache)
        rmtree(self.repo)

    def test_same_state_is_cached_and_changes_rerun(self):
        p = payload("python -m pytest -q", self.repo)
        first = hook.handle(p, session_dir=self.cache)
        second = hook.handle(p, session_dir=self.cache)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("still holds", second)
        self.assertIn(first, second)
        (self.repo / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 2\n")
        hook.handle(p, session_dir=self.cache)
        self.assertEqual(len(self.calls), 2, "a working-tree edit must invalidate the cached verdict")
        (self.repo / "new_untracked.py").write_text("x = 1\n")
        hook.handle(p, session_dir=self.cache)
        self.assertEqual(len(self.calls), 3, "a new untracked file must invalidate too")
        hook.handle(payload("python -m pytest -q", self.repo, sid="other"), session_dir=self.cache)
        self.assertEqual(len(self.calls), 4, "per session")
        later = hook.handle(p, now=time.time() + hook.DEDUP_SECONDS + 5, session_dir=self.cache)
        self.assertEqual(len(self.calls), 5, f"expires after the dedup window: {later}")

    def test_session_cap(self):
        os.environ[brand.ENV_PREFIX + "HOOK_MAX_RUNS"] = "2"
        try:
            for i in range(3):
                (self.repo / "f.txt").write_text(str(i))  # new fingerprint each time
                ctx = hook.handle(payload("python -m pytest", self.repo, sid="cap"), session_dir=self.cache)
            self.assertEqual(len(self.calls), 2)
            self.assertIn("limit", ctx)
        finally:
            os.environ.pop(brand.ENV_PREFIX + "HOOK_MAX_RUNS", None)


@unittest.skipUnless(have_pytest(), "pytest not installed")
class HookIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        rmtree(self.cache)

    def test_verdicts_reach_the_context(self):
        repo = git_repo({"calc.py": "def f():\n    return 1\n",
                         "tests/test_c.py": "from calc import f\n\ndef test_f():\n    assert f() == 1\n\n"
                                            "def test_old():\n    assert 2 == 3\n"})
        try:
            (repo / "calc.py").write_text("def f():\n    return 2\n")
            before = git_state(repo)
            cmd = f"cd {repo} && {sys.executable} -m pytest -q -p no:cacheprovider 2>&1 | tail -5"
            p = run_hook(payload(cmd, "/"), {"XDG_CACHE_HOME": self.cache})
            self.assertEqual(p.returncode, 0, p.stderr)
            lines = p.stdout.strip().splitlines()
            self.assertEqual(len(lines), 1, p.stdout)
            out = json.loads(lines[0])
            ctx = out["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUseFailure")
            self.assertIn("NEW — caused by the current change (1):", ctx)
            self.assertIn("tests/test_c.py::test_f", ctx)
            self.assertIn("PRE-EXISTING — already failing", ctx)
            self.assertIn("tests/test_c.py::test_old", ctx)
            self.assertEqual(git_state(repo), before)
        finally:
            rmtree(repo)

    def test_timeout_gives_no_verdict_and_cleans_up(self):
        repo = git_repo({"tests/test_s.py": "import time\n\ndef test_slow():\n    time.sleep(30)\n    assert 0\n"})
        try:
            before = git_state(repo)
            t = time.time()
            p = run_hook(payload(f"{sys.executable} -m pytest -q", repo),
                         {"XDG_CACHE_HOME": self.cache, P + "HOOK_TIMEOUT": "3"})
            self.assertLess(time.time() - t, 20)
            ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("No verdict (timed out", ctx)
            self.assertNotIn("PRE-EXISTING —", ctx)
            self.assertEqual(git_state(repo), before)
            ps = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True).stdout
            self.assertNotIn(str(repo), ps, "test processes must be killed")
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
