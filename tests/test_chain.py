"""Chained commands (`a && b; c`, or an npm script that is one): every part judged on its own, combined;
a part that can't be split or read is UNKNOWN; never the first part alone. Fake jest/react-scripts shim:
`--config ...` reads api-spec.json, otherwise jest-spec.json."""
import json
import os
import unittest
from pathlib import Path

from _support import commit, git_repo, mod, rmtree, run_cli, write
from test_mixed import SHIM

chain = mod("chain")
runners = mod("runners")
hook = mod("hook")
report = mod("report")

API_SHIM = SHIM.replace('spec = json.load(open("jest-spec.json"))',
                        'spec = json.load(open("api-spec.json" if "--config" in args else "jest-spec.json"))')
assert API_SHIM != SHIM
CHECK = "import sys\nsys.exit(0)\n"
ALL = ("CI=true react-scripts test --watchAll=false && jest --config jest.api.config.js && "
       "npm run check:env")
SCRIPTS = {"test": "react-scripts test", "test:api": "jest --config jest.api.config.js",
           "check:env": "python3 check_env.py && python3 check_flow.py", "test:all": ALL}
WEB_OK = {"src/a.test.js": {"adds": "pass", "bills": "pass"}}
API_OK = {"api/e.test.js": {"escapes": "pass", "quotes": "pass"}}


def repo_with(web, api, scripts=None):
    files = {"package.json": json.dumps({"scripts": scripts or SCRIPTS, "dependencies": {"react-scripts": "5"}}),
             ".gitignore": "node_modules\n", "jest-spec.json": json.dumps(web), "api-spec.json": json.dumps(api),
             "jest.api.config.js": "module.exports = {};\n", "check_env.py": CHECK, "check_flow.py": CHECK}
    for f in list(web) + list(api):
        files[f] = "test('x', () => {});\n"
    repo = git_repo(files)
    for tool in ("react-scripts", "jest"):
        p = repo / "node_modules" / ".bin" / tool
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(API_SHIM)
        p.chmod(0o755)
    return repo


def head(repo, web=None, api=None, files=None):
    out = dict(files or {})
    if web is not None:
        out["jest-spec.json"] = json.dumps(web)
    if api is not None:
        out["api-spec.json"] = json.dumps(api)
    write(repo, out)
    commit(repo, "head")


def check(repo, *cmd, fmt="--json"):
    e = dict(os.environ)
    e.pop("CI", None)
    p = run_cli(["check", fmt, "--base", "main~1", "--", *cmd], repo, env=e)
    if fmt != "--json":
        return p, None
    try:
        return p, json.loads(p.stdout)
    except ValueError:
        raise AssertionError(p.stdout + p.stderr)


def by_part(rep):
    return {(v.get("part"), v["id"]): v["verdict"] for v in rep["results"]}


class ChainEndToEndTest(unittest.TestCase):
    def tearDown(self):
        rmtree(self.repo)

    def test_a_new_failure_in_part_2_only(self):
        self.repo = repo_with(WEB_OK, API_OK)
        head(self.repo, api={"api/e.test.js": {"escapes": "fail:Error: expect(received).toBe(expected) &#96;",
                                               "quotes": "pass"}})
        p, rep = check(self.repo, "npm", "run", "test:all")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(by_part(rep), {(2, "api/e.test.js > escapes"): "new"})
        r = rep["runner"]
        self.assertEqual((r["name"], r["per_test"]), ("chain", False))
        self.assertEqual([x["command"] for x in r["parts"]],
                         ["CI=true react-scripts test --watchAll=false", "jest --config jest.api.config.js",
                          "python3 check_env.py", "python3 check_flow.py"])
        self.assertTrue(all(x["judged"] for x in r["parts"]))
        self.assertEqual([x["runner"]["name"] for x in r["parts"]], ["jest", "jest", "generic", "generic"])
        self.assertEqual(rep["schema"], 1)
        p, _ = check(self.repo, "npm", "run", "test:all", fmt="--format=text")
        self.assertIn("[part 2: jest --config jest.api.config.js] api/e.test.js > escapes", p.stdout)
        self.assertNotIn("nothing to attribute", p.stdout)
        self.assertEqual(p.returncode, 1)

    def test_b_new_failure_in_part_1(self):
        self.repo = repo_with(WEB_OK, API_OK)
        head(self.repo, web={"src/a.test.js": {"adds": "fail:Error: boom", "bills": "pass"}})
        p, rep = check(self.repo, "npm", "run", "test:all")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(by_part(rep), {(1, "src/a.test.js > adds"): "new"})
        # part 1 fails on head; the shell would stop there, but every later part was judged anyway
        self.assertTrue(all(x["judged"] for x in rep["runner"]["parts"]))
        self.assertTrue(any("judged independently" in n for n in rep["notes"]), rep["notes"])

    def test_c_pre_existing_in_part_2_is_clean_and_lists_both_parts(self):
        bad = {"api/e.test.js": {"escapes": "fail:Error: expect(received).toBe(expected) old", "quotes": "pass"}}
        self.repo = repo_with(WEB_OK, bad)
        head(self.repo, files={"README": "x\n"})
        p, rep = check(self.repo, "npm", "run", "test:all")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(by_part(rep), {(2, "api/e.test.js > escapes"): "pre_existing"})
        p, _ = check(self.repo, "npm", "run", "test:all", fmt="--format=text")
        self.assertEqual(p.returncode, 0)
        for i in (1, 2, 3, 4):
            self.assertIn(f"[part {i}: ", p.stdout)
        self.assertIn("4 of 4 parts judged", p.stdout)
        self.assertIn("No failure was caused by this change", p.stdout)

    def test_all_parts_pass(self):
        self.repo = repo_with(WEB_OK, API_OK)
        head(self.repo, files={"README": "x\n"})
        p, _ = check(self.repo, "npm", "run", "test:all", fmt="--format=text")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("all 4 parts of", p.stdout)
        self.assertIn("every part of the chained command was judged and passes", p.stdout)

    def test_d_e_unsplittable_parts_are_unknown(self):
        self.repo = repo_with(WEB_OK, API_OK)
        head(self.repo, files={"README": "x\n"})
        for body, why in (("jest --config jest.api.config.js && react-scripts test | tee out.log", "piped"),
                          ("jest --config jest.api.config.js || true", "||"),
                          ("jest --config jest.api.config.js; echo $(date)", "subshell"),
                          ("jest --config jest.api.config.js && react-scripts test > /dev/null", "redirect"),
                          ("jest --config jest.api.config.js && react-scripts test &", "background"),
                          ("jest --config 'jest.api.config.js && react-scripts test", "unbalanced")):
            with self.subTest(body=body):
                write(self.repo, {"package.json": json.dumps({"scripts": {"t": body}})})
                p, rep = check(self.repo, "npm", "run", "t")
                self.assertEqual(p.returncode, 3, (body, p.stdout, p.stderr))
                self.assertEqual(rep["summary"]["unknown"], 1, rep["results"])
                self.assertIn(why, json.dumps(rep["runner"]["parts"]))
                p, _ = check(self.repo, "npm", "run", "t", fmt="--format=text")
                self.assertNotIn("nothing to attribute", p.stdout)
                self.assertNotIn("passed on head", p.stdout)
                self.assertIn("NOT JUDGED", p.stdout)

    def test_chain_given_directly_on_the_command_line(self):
        self.repo = repo_with(WEB_OK, API_OK)
        head(self.repo, api={"api/e.test.js": {"escapes": "fail:Error: x", "quotes": "pass"}})
        p, rep = check(self.repo, "node_modules/.bin/react-scripts test --watchAll=false && "
                                  "node_modules/.bin/jest --config jest.api.config.js")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(by_part(rep), {(2, "api/e.test.js > escapes"): "new"})


class ChainParseTest(unittest.TestCase):
    def setUp(self):
        self.p = Path(git_repo({"package.json": json.dumps({"scripts": {
            "test:all": "CI=true react-scripts test --watchAll=false && npm run test:api",
            "test:api": "jest --config jest.api.config.js 2>&1 && npm run lint",
            "lint": "eslint .", "loop:a": "npm run loop:b && eslint .", "loop:b": "npm run loop:a; tsc",
            "plain": "jest --ci"}}), "api/x": ""}))
        (self.p / "node_modules" / ".bin").mkdir(parents=True)
        for t in ("react-scripts", "jest"):
            (self.p / "node_modules" / ".bin" / t).write_text("")

    def tearDown(self):
        rmtree(self.p)

    def test_f_env_prefix_and_npm_run_recursion(self):
        parts = chain.expand(["npm", "run", "test:all"], self.p, self.p)
        self.assertEqual([(x.argv, x.env, x.problem) for x in parts], [
            (["node_modules/.bin/react-scripts", "test", "--watchAll=false"], {"CI": "true"}, ""),
            (["node_modules/.bin/jest", "--config", "jest.api.config.js"], {}, ""),
            (["npm", "run", "lint"], {}, "")])   # simple body: resolved by the ordinary detection
        self.assertEqual(parts[1].via, "npm run test:all > npm run test:api")
        self.assertEqual(parts[0].command()[:2], ["CI=true", "node_modules/.bin/react-scripts"])
        # an env prefix on the npm call reaches every part
        parts = chain.expand(["NODE_ENV=test npm run test:api"], self.p, self.p)
        self.assertTrue(all(x.env.get("NODE_ENV") == "test" for x in parts))
        # `cd` moves the later parts; `export` reaches them
        parts = chain.expand(["export A=1; cd api && jest -x"], self.p, self.p)
        self.assertEqual((parts[0].cwd, parts[0].env), (self.p / "api", {"A": "1"}))

    def test_recursion_limit(self):
        parts = chain.expand(["npm", "run", "loop:a"], self.p, self.p)
        self.assertTrue(any("nested more than" in x.problem for x in parts), [x.problem for x in parts])

    def test_detection(self):
        self.assertEqual(runners.from_command(["npm", "run", "test:all"], self.p).runner, "chain")
        self.assertEqual(runners.from_command(["pnpm", "test:all"], self.p).runner, "chain")
        self.assertEqual(runners.from_command(["jest a && jest b"], self.p).runner, "chain")
        self.assertEqual(runners.from_command(["npm", "run", "plain"], self.p).runner, "jest")
        self.assertEqual(runners.from_command(["jest", "-t", "a && b"], self.p).runner, "jest")

    def test_hook_joins_test_commands_on_one_line(self):
        seg = hook.find_test_segment("pytest tests/a.py; npm install && pytest tests/b.py | tail", "/w")
        self.assertEqual((seg.kind, seg.argv), ("chain", ["pytest tests/a.py ; pytest tests/b.py"]))
        self.assertEqual(hook.find_test_segment("cd x && pytest -q", "/w").argv, ["pytest", "-q"])

    def test_hook_context_names_parts(self):
        v = {"id": "e > x", "kind": "test", "verdict": "new", "head": {"failed": 1, "runs": 1, "message": "boom"},
             "base": {"passed": 2, "runs": 2}, "part": 2, "part_command": "jest --config api.js"}
        rep = {"summary": {"new": 1, "parts": 3, "parts_judged": 3}, "base": {"short": "abc"}, "head": {"short": "d"},
               "runner": {"name": "chain", "per_test": False}, "results": [v]}
        ctx = hook.format_context(rep, "npm run test:all", "baseline")
        self.assertIn("[part 2: jest --config api.js] e > x", ctx)
        self.assertIn("Per-part (3 of 3 parts judged", ctx)


if __name__ == "__main__":
    unittest.main()
