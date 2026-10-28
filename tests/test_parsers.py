"""Parsers for every runner, on real reports captured from the research fixtures plus synthetic edge cases."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from _support import DATA, mod

model = mod("model")
procs = mod("procs")
runners = mod("runners")
base = mod("runners.base")

FIX = Path("/tmp/bl-fixtures/py-shop")  # where the samples were captured (only used as a path prefix)


def side(root, name="head", other=()):
    root = Path(root)
    return base.Side(name, root, root, root / ".out", {}, list(other))


def proc(stdout="", stderr="", rc=1, timed_out=False):
    return procs.ProcResult(["x"], ".", rc, stdout, stderr, 0.1, timed_out)


class PytestParserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for f in ("tests/test_cart.py", "tests/test_control.py", "tests/test_env.py", "tests/test_flaky.py",
                  "tests/test_report.py", "tests/test_shipping.py", "tests/test_tax.py"):
            (self.tmp / f).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / f).write_text("")
        (self.tmp / "shop").mkdir()
        (self.tmp / "shop" / "__init__.py").write_text("")
        self.r = runners.PytestRunner(["python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                       "--continue-on-collection-errors"], self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def parse(self, sample, root=None):
        rep = self.tmp / "r.xml"
        shutil.copy(DATA / sample, rep)
        inv = base.Invocation([], rep, "full")
        return self.r.parse(inv, proc(), side(root or self.tmp, other=[str(FIX)]))

    def test_real_fixture_report(self):
        res = self.parse("pytest_head.xml")
        self.assertTrue(res.ok)
        self.assertEqual(res.collected, 12)
        self.assertEqual(sorted(res.load_errors), ["tests/test_report.py"])
        le = res.load_errors["tests/test_report.py"]
        self.assertEqual(le.key, "ModuleNotFoundError: No module named 'shop.report'")
        self.assertIsNone(le.env, "first-party module is a code error, not environment")
        t = res.tests["tests/test_cart.py::test_new_discount_percent"]
        self.assertEqual(t.status, model.FAIL)
        self.assertTrue(t.key.startswith("AssertionError: assert 99.9 == 90"), t.key)
        self.assertEqual(res.tests["tests/test_cart.py::test_total_plain"].status, model.PASS)
        env = {k: res.tests[f"tests/test_env.py::{k}"].env["class"] for k in
               ("test_env_module", "test_env_var", "test_env_service")}
        self.assertEqual(env, {"test_env_module": "missing_module", "test_env_var": "missing_env_var",
                               "test_env_service": "service_unavailable"})
        self.assertIsNone(res.tests["tests/test_tax.py::test_pre_tax_rounding"].env)
        flaky = res.tests["tests/test_flaky.py::test_flaky_random"]
        self.assertIn("0x<ADDR>", flaky.key)
        self.assertEqual(flaky.locator, {"file": "tests/test_flaky.py", "rest": "::test_flaky_random"})

    def test_classes_params_setup_errors_skips(self):
        (self.tmp / "test_p.py").write_text("")
        res = self.parse("pytest_param.xml")
        ids = sorted(res.tests)
        self.assertIn("test_p.py::TestA::TestB::test_n", ids)
        self.assertIn("test_p.py::test_p[x::y]", ids)
        self.assertIn("test_p.py::test_p[[z]]", ids)
        self.assertIn("test_p.py::test_p[a b]", ids)
        self.assertEqual(res.tests["test_p.py::test_p[1]"].status, model.PASS)
        self.assertEqual(res.tests["test_p.py::test_skip"].status, model.SKIP)
        fx = res.tests["test_p.py::test_fx"]
        self.assertEqual(fx.status, model.FAIL)
        self.assertEqual(fx.key, "RuntimeError: fixture boom")
        self.assertEqual(res.tests["test_p.py::test_p[1]"].name, "test_p")

    def test_missing_report_is_a_crash(self):
        inv = base.Invocation([], self.tmp / "nope.xml", "full")
        res = self.r.parse(inv, proc(stderr="ERROR: usage: pytest [options]\npytest: error: unrecognized arguments",
                                     rc=4), side(self.tmp))
        self.assertFalse(res.ok)
        self.assertIn("unrecognized arguments", res.problem)

    def test_conftest_import_error_becomes_load_error(self):
        out = ("ImportError while loading conftest '%s/tests/conftest.py'.\ntests/conftest.py:1: in <module>\n"
               "    import nothere\nE   ModuleNotFoundError: No module named 'nothere'\n") % self.tmp
        res = self.r.parse(base.Invocation([], self.tmp / "nope.xml"), proc(stderr=out, rc=4), side(self.tmp))
        self.assertTrue(res.ok)
        self.assertEqual(list(res.load_errors), ["tests/conftest.py"])
        self.assertEqual(res.load_errors["tests/conftest.py"].env["class"], "missing_module")

    def test_timeout(self):
        res = self.r.parse(base.Invocation([], self.tmp / "r.xml"), proc(timed_out=True), side(self.tmp))
        self.assertFalse(res.ok)
        self.assertIn("timed out", res.problem)

    def test_command_building(self):
        r = runners.PytestRunner(["python", "-m", "pytest", "-q", "-k", "cart", "tests", "--lf", "--junitxml=x.xml",
                                  "-x"], self.tmp)
        s = side(self.tmp)
        full = r.full(s, "f")[0].argv
        self.assertIn("tests", full)
        self.assertNotIn("--lf", full)
        self.assertNotIn("--junitxml=x.xml", full)
        self.assertIn("--continue-on-collection-errors", full)
        self.assertEqual(full.count("no:cacheprovider"), 1)
        self.assertIn("cart", full)  # value of -k is not mistaken for a path
        u = model.Unit("tests/test_cart.py::test_a[1]", model.TEST, "tests/test_cart.py", "test_a",
                       {"file": "tests/test_cart.py", "rest": "::test_a[1]"})
        le = model.Unit("tests/test_report.py", model.LOAD_ERROR, "tests/test_report.py")
        sub = r.subset([u, le], s, "s")[0].argv
        self.assertIn("tests/test_cart.py::test_a[1]", sub)
        self.assertIn("tests/test_report.py", sub)
        self.assertNotIn("tests", sub[sub.index("--continue-on-collection-errors"):])  # positional dropped
        self.assertIn("-k", sub)


class VitestParserTest(unittest.TestCase):
    root = Path("/tmp/bl-fixtures/js-shop")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.r = runners.VitestRunner(["npx", "vitest", "run"], self.tmp)

    def parse(self, data, mode="full"):
        rep = self.tmp / "r.json"
        rep.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return self.r.parse(base.Invocation([], rep, mode), proc(), side(self.root))

    def test_real_fixture_report(self):
        res = self.parse((DATA / "vitest_head.json").read_text())
        self.assertEqual(list(res.load_errors), ["test/report.test.js"])
        self.assertIn("Cannot find module '../src/report.js'", res.load_errors["test/report.test.js"].key)
        self.assertIsNone(res.load_errors["test/report.test.js"].env)
        t = res.tests["test/cart.test.js > new discount percent"]
        self.assertEqual(t.key, "AssertionError: expected 99.9 to be 90 // Object.is equality")
        self.assertEqual(t.locator["full"], "new discount percent")
        self.assertEqual(res.tests["test/env.test.js > env module"].env["class"], "missing_module")
        self.assertEqual(res.tests["test/env.test.js > env service"].env["class"], "service_unavailable")
        self.assertEqual(res.tests["test/control.test.js > control a"].status, model.PASS)

    def test_describe_duplicates_and_suite_errors(self):
        data = {"testResults": [
            {"name": str(self.root / "test/a.test.js"), "status": "failed", "message": "",
             "assertionResults": [
                 {"ancestorTitles": ["outer", "inner"], "title": "t", "fullName": "outer inner t", "status": "failed",
                  "failureMessages": ["Error: boom\n    at x (/a.js:1:1)"]},
                 {"ancestorTitles": [], "title": "dup", "fullName": "dup", "status": "passed", "failureMessages": []},
                 {"ancestorTitles": [], "title": "dup", "fullName": "dup", "status": "failed",
                  "failureMessages": ["AssertionError: x"]},
                 {"ancestorTitles": [], "title": "s", "fullName": "s", "status": "skipped", "failureMessages": []}]},
            {"name": str(self.root / "test/b.test.js"), "status": "failed",
             "message": "Error: afterAll hook failed", "assertionResults": [
                 {"ancestorTitles": [], "title": "ok", "fullName": "ok", "status": "passed", "failureMessages": []}]},
        ]}
        res = self.parse(data)
        self.assertEqual(res.tests["test/a.test.js > outer > inner > t"].key, "Error: boom")
        self.assertEqual(res.tests["test/a.test.js > dup"].status, model.PASS)
        self.assertEqual(res.tests["test/a.test.js > dup [2]"].status, model.FAIL)
        self.assertEqual(res.tests["test/a.test.js > s"].status, model.SKIP)
        self.assertIn("test/b.test.js > (suite)", res.load_errors)

    def test_rerun_command_escapes_regex(self):
        s = side(self.root)
        u = model.Unit("test/a.test.js > a (b) > c?", model.TEST, "test/a.test.js", "c?",
                       {"file": "test/a.test.js", "full": "a (b) c?"})
        le = model.Unit("test/report.test.js", model.LOAD_ERROR, "test/report.test.js",
                        locator={"file": "test/report.test.js"})
        argv = self.r.subset([u, le], s, "x")[0].argv
        self.assertEqual(argv[argv.index("-t") + 1], r"^(?:a \(b\) c\?)$")
        self.assertIn("test/report.test.js", argv)
        self.assertIn("--passWithNoTests", argv)
        self.assertIn("run", argv)

    def test_watch_mode_and_reporters_are_overridden(self):
        r = runners.VitestRunner(["npx", "vitest", "watch", "--reporter=verbose", "test/a"], self.tmp)
        argv = r.full(side(self.root), "f")[0].argv
        self.assertEqual(argv.count("run"), 1)
        self.assertNotIn("--reporter=verbose", argv)
        self.assertIn("--reporter=json", argv)
        self.assertIn("test/a", argv)

    def test_no_test_files_in_subset_is_empty_not_crash(self):
        res = self.r.parse(base.Invocation([], self.tmp / "none.json", "subset"),
                           proc(stdout="No test files found, exiting with code 1"), side(self.root))
        self.assertTrue(res.ok)
        self.assertEqual(res.collected, 0)


class JestParserTest(unittest.TestCase):
    def test_suite_failed_to_run_and_messages(self):
        tmp = Path(tempfile.mkdtemp())
        r = runners.JestRunner(["npx", "jest"], tmp)
        data = {"testResults": [
            {"name": str(tmp / "src/a.test.js"), "status": "failed",
             "message": "  ● Test suite failed to run\n\n    Cannot find module 'left-pad' from 'src/a.test.js'\n",
             "assertionResults": []},
            {"name": str(tmp / "src/b.test.js"), "status": "failed", "message": "", "assertionResults": [
                {"ancestorTitles": ["b"], "title": "adds", "fullName": "b adds", "status": "failed",
                 "failureMessages": ["Error: expect(received).toBe(expected) // Object.is equality\n\n"
                                     "Expected: 3\nReceived: 4\n    at Object.<anonymous> (/x/b.test.js:3:9)"]}]}]}
        (tmp / "r.json").write_text(json.dumps(data))
        res = r.parse(base.Invocation([], tmp / "r.json"), proc(), side(tmp))
        self.assertEqual(res.load_errors["src/a.test.js"].env["class"], "missing_module")
        key = res.tests["src/b.test.js > b > adds"].key
        self.assertIn("Expected: 3", key)
        self.assertIn("Received: 4", key)
        self.assertNotIn("Object.<anonymous>", key)
        u = model.Unit("src/b.test.js > b > adds", model.TEST, "src/b.test.js", "adds",
                       {"file": "src/b.test.js", "full": "b adds"})
        argv = r.subset([u], side(tmp), "s")[0].argv
        self.assertIn("--runTestsByPath", argv)
        self.assertIn("--json", argv)


class GoParserTest(unittest.TestCase):
    def setUp(self):
        self.r = runners.GoRunner(["go", "test", "-count=1", "./..."], Path("/nonexistent"))

    def test_real_fixture_report(self):
        res = self.r.parse(base.Invocation([]), proc((DATA / "go_head.jsonl").read_text()),
                           side("/tmp/bl-fixtures/go-shop"))
        self.assertEqual(list(res.load_errors), ["example.com/shop/report"])
        self.assertEqual(res.load_errors["example.com/shop/report"].key,
                         "report/report_test.go:<L>:<C>: undefined: Summary")
        t = res.tests["example.com/shop/cart::TestNewDiscountPercent"]
        self.assertEqual(t.key, "cart_test.go:<L>: got 99.9 want 90")
        self.assertEqual(res.tests["example.com/shop/envtests::TestEnvModule"].env["class"], "missing_executable")
        self.assertEqual(res.tests["example.com/shop/envtests::TestEnvVar"].env["class"], "missing_env_var")
        self.assertEqual(res.tests["example.com/shop/envtests::TestEnvService"].env["class"], "service_unavailable")
        self.assertEqual(res.tests["example.com/shop/control::TestControlA"].status, model.PASS)
        self.assertEqual(res.collected, 12)

    def test_subtests_and_panics(self):
        res = self.r.parse(base.Invocation([]), proc((DATA / "go_subtests.jsonl").read_text()), side("/src/gosub"))
        self.assertNotIn("example.com/sub/p::TestTable", res.tests, "parent failing only via subtest is dropped")
        self.assertEqual(res.tests["example.com/sub/p::TestTable/two_bad"].status, model.FAIL)
        self.assertEqual(res.tests["example.com/sub/p::TestTable/one_ok"].status, model.PASS)
        self.assertEqual(res.tests["example.com/sub/p::TestPanics"].status, model.FAIL)
        self.assertIn("assignment to entry in nil map", res.tests["example.com/sub/p::TestPanics"].key)
        self.assertEqual(res.load_errors, {})

    def test_old_go_build_errors_on_stderr(self):
        out = ('{"Action":"start","Package":"m/a"}\n'
               '{"Action":"output","Package":"m/a","Output":"FAIL\\tm/a [build failed]\\n"}\n'
               '{"Action":"fail","Package":"m/a","Elapsed":0}\n')
        err = "# m/a [m/a.test]\na/a_test.go:5:2: undefined: Gone\n"
        res = self.r.parse(base.Invocation([]), proc(out, err), side("/src"))
        self.assertEqual(res.load_errors["m/a"].key, "a/a_test.go:<L>:<C>: undefined: Gone")

    def test_no_json_is_crash(self):
        res = self.r.parse(base.Invocation([]), proc("", "go: go.mod file not found", rc=1), side("/src"))
        self.assertFalse(res.ok)

    def test_subset_commands(self):
        u = model.Unit("m/a::TestX/sub_1", model.TEST, "m/a", "TestX/sub_1", {"pkg": "m/a", "test": "TestX/sub_1"})
        u2 = model.Unit("m/b::TestY", model.TEST, "m/b", "TestY", {"pkg": "m/b", "test": "TestY"})
        le = model.Unit("m/c", model.LOAD_ERROR, "m/c")
        invs = self.r.subset([u, u2, le], side("/src"), "s")
        self.assertEqual(len(invs), 2)
        self.assertIn("m/c", invs[0].argv)
        self.assertNotIn("-run", invs[0].argv)
        a = invs[1].argv
        self.assertEqual(a[a.index("-run") + 1], "^(?:TestX|TestY)$")
        self.assertIn("-json", a)
        self.assertIn("-count=1", a)

    def test_user_flags_kept_count_and_json_normalised(self):
        r = runners.GoRunner(["go", "test", "-v", "-count", "3", "-tags", "integration", "-run", "X", "./pkg/..."],
                             Path("/nonexistent"))
        argv = r.full(side("/src"), "f")[0].argv
        self.assertEqual(argv[:5], ["go", "test", "-json", "-count=1", "-tags"])
        self.assertIn("./pkg/...", argv)
        self.assertNotIn("3", argv)
        sub = r.subset([model.Unit("m::TestA", model.TEST, "m", "TestA", {"pkg": "m", "test": "TestA"})],
                       side("/src"), "s")[0].argv
        self.assertEqual(sub.count("-run"), 1)


class CargoRspecGenericTest(unittest.TestCase):
    def test_cargo_text(self):
        r = runners.CargoRunner(["cargo", "test"], Path("/x"))
        out = """   Compiling shop v0.1.0 (/x)
    Finished test [unoptimized + debuginfo] target(s) in 0.5s
     Running unittests src/lib.rs (target/debug/deps/shop-1234)

running 3 tests
test cart::tests::total ... ok
test cart::tests::discount ... FAILED
test tax::tests::ignored_one ... ignored

failures:

---- cart::tests::discount stdout ----
thread 'cart::tests::discount' panicked at src/cart.rs:20:9:
assertion `left == right` failed
  left: 99.9
 right: 90.0
note: run with `RUST_BACKTRACE=1` environment variable to display a backtrace

failures:
    cart::tests::discount

test result: FAILED. 1 passed; 1 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s
"""
        res = r.parse(base.Invocation([]), proc(out), side("/x"))
        t = res.tests["src/lib.rs::cart::tests::discount"]
        self.assertEqual(t.status, model.FAIL)
        self.assertIn("left: 99.9", t.key)
        self.assertEqual(res.tests["src/lib.rs::tax::tests::ignored_one"].status, model.SKIP)
        argv = r.subset([model.Unit(t.id, model.TEST, t.group, t.name, t.locator)], side("/x"), "s")[0].argv
        self.assertEqual(argv[-2:], ["--exact", "cart::tests::discount"])
        err = "error[E0425]: cannot find function `total2` in module `cart`\nerror: could not compile `shop` (lib test)"
        res2 = r.parse(base.Invocation([]), proc("", err, rc=101), side("/x"))
        self.assertIn("(crate shop)", res2.load_errors)

    def test_rspec_json(self):
        tmp = Path(tempfile.mkdtemp())
        r = runners.RspecRunner(["bundle", "exec", "rspec"], tmp)
        data = {"examples": [
            {"id": "./spec/cart_spec.rb[1:1]", "description": "totals", "full_description": "Cart totals",
             "status": "failed", "file_path": "./spec/cart_spec.rb",
             "exception": {"class": "RSpec::Expectations::ExpectationNotMetError",
                           "message": "expected: 90\n     got: 99.9", "backtrace": ["./spec/cart_spec.rb:5"]}},
            {"id": "./spec/cart_spec.rb[1:2]", "description": "ok", "full_description": "Cart ok",
             "status": "passed", "file_path": "./spec/cart_spec.rb"}],
            "messages": ["An error occurred while loading ./spec/report_spec.rb.\nFailure/Error: require 'nope'\n\n"
                         "LoadError:\n  cannot load such file -- nope"]}
        (tmp / "r.json").write_text(json.dumps(data))
        res = r.parse(base.Invocation([], tmp / "r.json"), proc(), base.Side("head", tmp, tmp, tmp, {}))
        t = res.tests["spec/cart_spec.rb > Cart totals"]
        self.assertEqual(t.locator["index"], "[1:1]")
        self.assertIn("got: 99.9", t.key)
        self.assertIn("spec/report_spec.rb", res.load_errors)

    def test_generic_whole_command(self):
        r = runners.GenericRunner(["make", "test"], Path("/x"))
        ok = r.parse(base.Invocation([]), proc("all good", rc=0), side("/x"))
        self.assertEqual(ok.tests["make test"].status, model.PASS)
        bad = r.parse(base.Invocation([]), proc("boom", rc=2), side("/x"))
        self.assertEqual(bad.tests["make test"].key, "boom")
        self.assertEqual(bad.tests["make test"].raw, "boom")


class DetectionTest(unittest.TestCase):
    def test_from_command(self):
        p = Path(tempfile.mkdtemp())
        cases = {
            ("python", "-m", "pytest", "-q"): "pytest", ("pytest",): "pytest", ("uv", "run", "pytest"): "pytest",
            ("npx", "vitest", "run"): "vitest", ("pnpm", "vitest"): "vitest", ("npx", "jest"): "jest",
            ("go", "test", "./..."): "go", ("cargo", "test"): "cargo", ("bundle", "exec", "rspec"): "rspec",
            ("make", "test"): "generic",
        }
        for argv, want in cases.items():
            self.assertEqual(runners.from_command(list(argv), p).runner, want, argv)
        self.assertEqual(runners.from_command(["python -m pytest -q"], p).argv, ["python", "-m", "pytest", "-q"])

    def test_npm_test_resolves_script(self):
        p = Path(tempfile.mkdtemp())
        (p / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run --silent"}}))
        d = runners.from_command(["npm", "test"], p)
        self.assertEqual((d.runner, d.argv), ("vitest", ["vitest", "run", "--silent"]))
        # (was: generic) a script that starts with the test runner runs its first part, with a note
        (p / "package.json").write_text(json.dumps({"scripts": {"test": "NODE_ENV=x vitest && echo"}}))
        d = runners.from_command(["npm", "test"], p)
        self.assertEqual((d.runner, d.argv, d.env), ("vitest", ["vitest"], {"NODE_ENV": "x"}))
        self.assertIn("echo", d.note)
        (p / "package.json").write_text(json.dumps({"scripts": {"test": "echo x | vitest"}}))
        self.assertEqual(runners.from_command(["npm", "test"], p).runner, "generic")

    def test_auto_detect(self):
        p = Path(tempfile.mkdtemp())
        self.assertEqual(runners.auto_detect(p), [])
        (p / "pytest.ini").write_text("[pytest]\n")
        (p / "go.mod").write_text("module x\n")
        (p / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^3"}}))
        names = [d.runner for d in runners.auto_detect(p)]
        self.assertEqual(names, ["pytest", "vitest", "go"])
        q = Path(tempfile.mkdtemp())
        (q / "tests").mkdir()
        (q / "tests" / "test_a.py").write_text("")
        self.assertEqual(runners.auto_detect(q)[0].runner, "pytest")
        j = Path(tempfile.mkdtemp())
        (j / "package.json").write_text(json.dumps({"devDependencies": {"jest": "^29"}}))
        self.assertEqual(runners.auto_detect(j)[0].argv, ["npx", "jest"])


if __name__ == "__main__":
    unittest.main()
