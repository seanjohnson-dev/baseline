"""node:test runner: TAP parsing, rerun pattern, real `node --test` when node >= 20 is on PATH."""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from _support import commit, git_repo, mod, rmtree, run_cli, write

nodetest = mod("runners.nodetest")
runners = mod("runners")
base = mod("runners.base")
model = mod("model")
procs = mod("procs")

TAP = """TAP version 13
# Subtest: adds
ok 1 - adds
  ---
  duration_ms: 1.1
  ...
# Subtest: group
    # Subtest: inner \\#1 (bad)
    not ok 1 - inner \\#1 (bad)
      ---
      duration_ms: 1.7
      location: '/r/a.test.mjs:6:11'
      failureType: 'testCodeFailure'
      error: |-
        Expected values to be strictly equal:

        1 !== 2

      code: 'ERR_ASSERTION'
      name: 'AssertionError'
      stack: |-
        TestContext.<anonymous> (file:///r/a.test.mjs:6:50)
      ...
    1..1
not ok 2 - group
  ---
  location: '/r/a.test.mjs:4:1'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  ...
# Subtest: later
ok 3 - later # SKIP
  ---
  ...
# Error [ERR_MODULE_NOT_FOUND]: Cannot find module '/r/nope.ts' imported from /r/b.test.mjs
# Subtest: /r/b.test.mjs
not ok 4 - /r/b.test.mjs
  ---
  location: '/r/b.test.mjs:1:1'
  failureType: 'testCodeFailure'
  exitCode: 1
  error: 'test failed'
  ...
1..4
# tests 4
"""


class FakeProc:
    def __init__(self, rc=1):
        self.returncode, self.duration, self.output, self.timed_out = rc, 0.1, "", False


def node_ok() -> bool:
    exe = shutil.which("node")
    if not exe:
        return False
    v = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout
    m = re.match(r"v(\d+)", v)
    return bool(m) and int(m.group(1)) >= 20


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())

    def tearDown(self):
        rmtree(self.tmp)

    def test_parse_tap(self):
        rep = self.tmp / "r.tap"
        rep.write_text(TAP)
        r = nodetest.NodeTestRunner(["node", "--test"], Path("/r"))
        side = base.Side("head", Path("/r"), Path("/r"), self.tmp)
        res = r.parse(base.Invocation([], rep), FakeProc(), side)
        self.assertEqual(res.tests["adds"].status, model.PASS)
        t = res.tests["group > inner #1 (bad)"]
        self.assertEqual(t.status, model.FAIL)
        self.assertEqual(t.group, "a.test.mjs")
        self.assertIn("1 !== 2", t.message)
        self.assertNotIn("group", res.tests)  # derived parent failure is not a unit
        self.assertEqual(res.tests["later"].status, model.SKIP)
        self.assertIn("b.test.mjs", res.load_errors)
        self.assertIn("ERR_MODULE_NOT_FOUND", res.load_errors["b.test.mjs"].message)

    def test_rerun_pattern_escapes_and_includes_ancestors(self):
        r = nodetest.NodeTestRunner(["node", "--test", "--test-reporter=spec", "a.test.mjs"], Path("/r"))
        side = base.Side("head", Path("/r"), Path("/r"), self.tmp)
        u = model.Unit("group > inner #1 (bad)", model.TEST, "a.test.mjs", "inner #1 (bad)",
                       {"file": "a.test.mjs", "path": "group\x1finner #1 (bad)"})
        argv = r.subset([u], side, "x")[0].argv
        self.assertIn("--test-name-pattern=^(?:group|inner\\ \\#1\\ \\(bad\\))$", argv)
        self.assertEqual(argv[-1], "a.test.mjs")
        self.assertNotIn("--test-reporter=spec", argv)

    def test_detection(self):
        self.assertEqual(runners.runner_for_argv(["node", "--test", "--experimental-strip-types"]), "node-test")
        repo = git_repo({"package.json": json.dumps({"scripts": {"test": "node --test"}}),
                         "tests/x.test.mjs": "import test from 'node:test';\n"})
        try:
            self.assertEqual(runners.from_command(["npm", "test"], repo).runner, "node-test")
            (repo / "package.json").write_text("{}")
            det = runners.auto_detect(repo)
            self.assertEqual(det[0].runner, "node-test")
            self.assertEqual(det[0].argv, ["node", "--test", "tests/x.test.mjs"])
        finally:
            rmtree(repo)


@unittest.skipUnless(node_ok(), "node >= 20 not on PATH")
class NodeIntegrationTest(unittest.TestCase):
    SRC = ("import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
           "import { add } from './add.mjs';\n"
           "test('math', async (t) => {\n  await t.test('adds', () => assert.equal(add(1, 2), 3));\n"
           "  await t.test('zero', () => assert.equal(add(0, 0), 0));\n});\n"
           "test('old broken', () => assert.equal(1, 2));\n")

    def test_new_and_pre_existing(self):
        repo = git_repo({"add.mjs": "export const add = (a, b) => a + b;\n", "a.test.mjs": self.SRC})
        try:
            write(repo, {"add.mjs": "export const add = (a, b) => a + b + (a ? 1 : 0);\n"})
            p = run_cli(["check", "--json", "--", "node", "--test"], repo)
            rep = json.loads(p.stdout)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            got = {v["id"]: v["verdict"] for v in rep["results"]}
            self.assertEqual(got, {"math > adds": "new", "old broken": "pre_existing"})
            new = [v for v in rep["results"] if v["verdict"] == "new"][0]
            self.assertIn("4 !== 3", new["head"]["message"])
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
