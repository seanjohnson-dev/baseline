"""node:test describe() suites: suites are not tests, failing tests inside them get per-test verdicts,
suite-level hook failures are blamed on the suite's tests, and nothing that exists is "missing on head".

The fixtures in data/nodetest are real `node --test` TAP output (Node 24 and Node 20) captured by
data/nodetest/capture.sh, which also documents the scenarios."""
import json
import shutil
import unittest
from pathlib import Path

from _support import DATA, commit, git_repo, mod, rmtree, run_cli, write
from test_nodetest import FakeProc, node_ok

nodetest = mod("runners.nodetest")
base = mod("runners.base")
model = mod("model")
decide = mod("decide")

FIXTURES = DATA / "nodetest"
ROOT = Path("/tmp/nodetest-fixture")  # where capture.sh ran node: the TAP's paths point there

TESTS_ON_HEAD = {
    "invoice > totals lines", "invoice > discounts > applies a percentage",
    "invoice > discounts > applies a fixed amount", "invoice > discounts > stacked > caps at 100%",
    "format > rounds half a cent up", "format > puts the minus sign first", "rates > quotes from the sandbox",
    "ledger > posts a debit", "ledger > posts a credit", "archive > monthly > writes a file",
}
SUITES = {"invoice", "invoice > discounts", "invoice > discounts > stacked", "format", "rates", "ledger",
          "archive", "archive > monthly"}
VERDICTS = {
    "invoice > discounts > applies a percentage": model.NEW,
    "invoice > discounts > stacked > caps at 100%": model.NEW,
    "archive > monthly > writes a file": model.NEW,  # the after hook throws on head only
    "format > rounds half a cent up": "pre_existing",
    "ledger > posts a debit": "pre_existing",  # the before hook throws on both sides, same error
    "ledger > posts a credit": "pre_existing",
    "format > puts the minus sign first": model.PRE_CHANGED,
    "rates > quotes from the sandbox": model.FLAKY,
}


class DescribeFixtureMixin:
    VERSION = ""

    def setUp(self):
        self.tmp = Path(__import__("tempfile").mkdtemp())

    def tearDown(self):
        rmtree(self.tmp)

    def parse(self, which, side_name="head"):
        r = nodetest.NodeTestRunner(["node", "--test"], ROOT)
        side = base.Side(side_name, ROOT, ROOT, self.tmp)
        return r.parse(base.Invocation([], FIXTURES / f"{self.VERSION}-{which}.tap"), FakeProc(), side)

    def test_suites_are_not_tests(self):
        head = self.parse("head")
        self.assertEqual(set(head.tests), TESTS_ON_HEAD)
        self.assertFalse(SUITES & set(head.tests))
        self.assertEqual(head.load_errors, {})

    def test_nested_ids_and_locators(self):
        t = self.parse("head").tests["invoice > discounts > stacked > caps at 100%"]
        self.assertEqual(t.status, model.FAIL)
        self.assertEqual(t.name, "caps at 100%")
        self.assertEqual(t.group, "invoice.test.mjs")
        self.assertEqual(t.locator, {"file": "invoice.test.mjs",
                                     "path": "invoice\x1fdiscounts\x1fstacked\x1fcaps at 100%"})
        self.assertIn("250 !== 0", t.message)

    def test_nothing_that_exists_is_missing_on_head(self):
        b, h = self.parse("base", "base"), self.parse("head")
        self.assertEqual(sorted(set(b.tests) - set(h.tests)), ["invoice > keeps legacy rounding"])
        # the suites that fail only on head (invoice, discounts, stacked) were never units on base either
        self.assertFalse(SUITES & set(b.tests))

    def test_before_hook_failure_is_blamed_on_the_suites_tests(self):
        for which, side in (("base", "base"), ("head", "head")):
            res = self.parse(which, side)
            for tid in ("ledger > posts a debit", "ledger > posts a credit"):
                t = res.tests[tid]
                self.assertEqual(t.status, model.FAIL, (which, tid))
                self.assertEqual(t.message, "hook failed in 'ledger': ledger seed is missing account 4000")
                self.assertEqual(t.group, "invoice.test.mjs")
                self.assertNotIn("did not finish before its parent", t.raw)
        self.assertEqual(self.parse("base", "base").tests["ledger > posts a debit"].key,
                         self.parse("head").tests["ledger > posts a debit"].key)

    def test_after_hook_failure_is_blamed_on_the_tests_that_passed(self):
        self.assertEqual(self.parse("base", "base").tests["archive > monthly > writes a file"].status, model.PASS)
        t = self.parse("head").tests["archive > monthly > writes a file"]
        self.assertEqual(t.status, model.FAIL)
        self.assertEqual(t.message, "hook failed in 'archive': archive temp dir was not removed")
        self.assertEqual(t.locator["file"], "invoice.test.mjs")  # from the suite: a pass has no location

    def test_rerun_with_name_pattern(self):
        # Node 20 lists the tests that don't match as skipped and still runs (and fails) the ledger
        # and archive hooks; neither the suites nor their skipped tests may turn into failures
        sub = self.parse("head-subset")
        self.assertEqual(sub.tests["invoice > discounts > applies a percentage"].status, model.FAIL)
        self.assertFalse(SUITES & set(sub.tests))
        for tid, t in sub.tests.items():
            if not tid.startswith("invoice > "):
                self.assertEqual(t.status, model.SKIP, tid)

    def test_verdicts(self):
        b, h1, h2 = self.parse("base", "base"), self.parse("head"), self.parse("head-rerun")
        got = {}
        for tid, t in h1.tests.items():
            if t.status != model.FAIL:
                continue
            unit = model.Unit(tid, model.TEST, t.group, t.name, dict(t.locator))
            st = decide.UnitState(unit, head=[t.obs(), h2.tests[tid].obs()],
                                  base=[b.tests[tid].obs()] * 2, exists_on_base=True)
            got[tid] = decide.decide(st)["verdict"]
        self.assertEqual(got, VERDICTS)

    def test_rerun_pattern_for_a_nested_test_names_its_suites(self):
        t = self.parse("head").tests["invoice > discounts > stacked > caps at 100%"]
        r = nodetest.NodeTestRunner(["node", "--test", "invoice.test.mjs"], ROOT)
        u = model.Unit(t.id, model.TEST, t.group, t.name, dict(t.locator))
        argv = r.subset([u], base.Side("head", ROOT, ROOT, self.tmp), "x")[0].argv
        self.assertIn("--test-name-pattern=^(?:caps\\ at\\ 100%|discounts|invoice|stacked)$", argv)


class Node24FixtureTest(DescribeFixtureMixin, unittest.TestCase):
    VERSION = "v24"


class Node20FixtureTest(DescribeFixtureMixin, unittest.TestCase):
    VERSION = "v20"


TEST_PARENT = """TAP version 13
# Subtest: parent
    # Subtest: child ok
    ok 1 - child ok
      ---
      duration_ms: 0.17
      ...
    # Subtest: child bad
    not ok 2 - child bad
      ---
      duration_ms: 1.28
      location: '/r/b.test.mjs:13:11'
      failureType: 'testCodeFailure'
      error: 'boom'
      ...
    1..2
not ok 1 - parent
  ---
  duration_ms: 2.08
  location: '/r/b.test.mjs:11:1'
  failureType: 'subtestsFailed'
  error: '1 subtest failed'
  ...
# Subtest: throws
not ok 2 - throws
  ---
  duration_ms: 0.8
  type: 'suite'
  location: '/r/b.test.mjs:3:1'
  failureType: 'testCodeFailure'
  error: 'bad describe'
  code: 'ERR_TEST_FAILURE'
  ...
# Subtest: outer
    # Subtest: inner
        # Subtest: deep
        not ok 1 - deep
          ---
          duration_ms: 0
          location: '/r/b.test.mjs:7:5'
          failureType: 'cancelledByParent'
          error: 'test did not finish before its parent and was cancelled'
          code: 'ERR_TEST_FAILURE'
          ...
        1..1
    not ok 1 - inner
      ---
      duration_ms: 0
      type: 'suite'
      location: '/r/b.test.mjs:6:3'
      failureType: 'cancelledByParent'
      error: 'test did not finish before its parent and was cancelled'
      code: 'ERR_TEST_FAILURE'
      ...
    # Subtest: skipped one
    not ok 2 - skipped one # SKIP
      ---
      duration_ms: 0
      location: '/r/b.test.mjs:9:3'
      failureType: 'cancelledByParent'
      error: 'test did not finish before its parent and was cancelled'
      code: 'ERR_TEST_FAILURE'
      ...
    1..2
not ok 3 - outer
  ---
  duration_ms: 0.4
  type: 'suite'
  location: '/r/b.test.mjs:4:1'
  failureType: 'hookFailed'
  error: 'outer setup'
  code: 'ERR_TEST_FAILURE'
  ...
1..3
"""  # Node 20.19 output (trimmed stacks) of test() with t.test() subtests, a describe() that throws,
# and a before hook that throws in a suite holding a nested suite and a skipped test


class EdgeCaseTest(unittest.TestCase):
    def test_test_parents_suites_without_tests_and_nested_hooks(self):
        tmp = Path(__import__("tempfile").mkdtemp())
        try:
            rep = tmp / "r.tap"
            rep.write_text(TEST_PARENT)
            r = nodetest.NodeTestRunner(["node", "--test"], Path("/r"))
            res = r.parse(base.Invocation([], rep), FakeProc(), base.Side("head", Path("/r"), Path("/r"), tmp))
        finally:
            rmtree(tmp)
        st = {tid: t.status for tid, t in res.tests.items()}
        self.assertEqual(st, {"parent > child ok": model.PASS, "parent > child bad": model.FAIL,
                              # a test() whose subtest failed: its own body passed, it's never missing
                              "parent": model.PASS,
                              # a suite failing with no test in it stays a unit: the failure can't vanish
                              "throws": model.FAIL,
                              "outer > inner > deep": model.FAIL, "outer > skipped one": model.SKIP})
        self.assertEqual(res.tests["throws"].message, "bad describe")
        self.assertEqual(res.tests["outer > inner > deep"].message, "hook failed in 'outer': outer setup")


@unittest.skipUnless(node_ok() and shutil.which("npm"), "node >= 20 and npm not on PATH")
class DescribeIntegrationTest(unittest.TestCase):
    SRC = """\
        import { describe, it, before, after } from 'node:test';
        import assert from 'node:assert/strict';
        import { discount, negative, seed, cleanup } from './calc.mjs';

        describe('invoice', () => {
          it('totals', () => assert.equal(1 + 2, 3));
          describe('discounts', () => {
            it('applies a percentage', () => assert.equal(discount(100, 10), 90));
          });
          it('keeps legacy rounding', () => assert.equal(Math.round(1.4), 1));
        });

        describe('format', () => {
          it('rounds half a cent up', () => assert.equal(Math.round(1.005 * 100) / 100, 1.01));
          it('puts the minus sign first', () => assert.equal(negative(5), '-$5.00'));
        });

        describe('ledger', () => {
          before(() => seed());
          it('posts a debit', () => assert.ok(true));
        });

        describe('archive', () => {
          after(() => cleanup());
          describe('monthly', () => {
            it('writes a file', () => assert.ok(true));
          });
        });
        """
    CALC = """\
        export const discount = (v, p) => v * (1 - p / 100);
        export const negative = (c) => `$-${c.toFixed(2)}`;
        export const seed = () => { throw new Error('ledger seed is missing account 4000'); };
        export const cleanup = () => {};
        """

    def test_describe_suites_end_to_end(self):
        repo = git_repo({"package.json": json.dumps({"name": "d", "version": "1.0.0", "type": "module",
                                                     "scripts": {"test": "node --test"}}),
                         "calc.mjs": self.CALC, "calc.test.mjs": self.SRC})
        try:
            write(repo, {"calc.mjs": self.CALC.replace("1 - p", "1 + p").replace("`$-${c.toFixed(2)}`", "`-$${c}`")
                         .replace("=> {};", "=> { throw new Error('archive temp dir was not removed'); };"),
                         "calc.test.mjs": self.SRC.replace(
                             "  it('keeps legacy rounding', () => assert.equal(Math.round(1.4), 1));\n", "")})
            commit(repo, "head")
            p = run_cli(["check", "--json", "--base", "HEAD~1", "--full-base", "always", "--", "npm", "test"],
                        repo)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            rep = json.loads(p.stdout)
            got = {v["id"]: v["verdict"] for v in rep["results"]}
            self.assertEqual(got, {
                "invoice > discounts > applies a percentage": "new",
                "archive > monthly > writes a file": "new",
                "format > rounds half a cent up": "pre_existing",
                "ledger > posts a debit": "pre_existing",
                "format > puts the minus sign first": "pre_existing_error_changed",
            }, p.stdout)
            self.assertEqual(rep["missing"], ["invoice > keeps legacy rounding"])
            by_id = {v["id"]: v for v in rep["results"]}
            self.assertIn("hook failed in 'ledger': ledger seed is missing account 4000",
                          by_id["ledger > posts a debit"]["head"]["message"])
            self.assertIn("hook failed in 'archive': archive temp dir was not removed",
                          by_id["archive > monthly > writes a file"]["head"]["message"])
            self.assertEqual(rep["summary"]["tests_total"], 6, rep["summary"])  # suites are not counted
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
