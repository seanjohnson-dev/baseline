"""Confirmation runs are capped on big failing sets: a counting fake jest records every invocation and every
test it ran; many NEW failures cost a bounded number of invocations and test runs, and a test that flips is
still FLAKY (never NEW)."""
import json
import os
import unittest

from _support import commit, git_repo, mod, rmtree, run_cli, write
from test_mixed import SHIM

decide = mod("decide")

# "flip": fails the first time it runs in a directory, passes after that (a flaky test, deterministically)
FLIP_SHIM = SHIM.replace('        ok = st == "pass"\n',
                         '        if st == "flip":\n'
                         '            ok = os.path.exists(".flip-seen")\n'
                         '            open(".flip-seen", "w").close()\n'
                         '            st = "pass" if ok else "fail:Error: timed out"\n'
                         '        ok = st == "pass"\n'
                         '        with open(os.environ.get("SHIM_LOG", os.devnull) + ".tests", "a") as lf:\n'
                         '            lf.write(t + "\\n")\n')
assert FLIP_SHIM != SHIM
N = 24


class ConfirmCapTest(unittest.TestCase):
    def setUp(self):
        base = {f"src/f{i}.test.js": {f"t{i}-{k}": "pass" for k in range(6)} for i in range(4)}
        base["src/z.test.js"] = {"flips": "flip", "steady": "pass"}
        files = {"package.json": json.dumps({"scripts": {"test": "react-scripts test"}}),
                 ".gitignore": "node_modules\n.flip-seen\n", "jest-spec.json": json.dumps(base)}
        for f in base:
            files[f] = "test('x', () => {});\n"
        self.repo = git_repo(files)
        p = self.repo / "node_modules" / ".bin" / "react-scripts"
        p.parent.mkdir(parents=True)
        p.write_text(FLIP_SHIM)
        p.chmod(0o755)
        head = {f: dict(t) for f, t in base.items()}
        for i in range(4):
            for k in range(6):
                head[f"src/f{i}.test.js"][f"t{i}-{k}"] = f"fail:Error: expect(received).toBe(expected) {i} {k}"
        write(self.repo, {"jest-spec.json": json.dumps(head)})
        commit(self.repo, "head")
        self.log = self.repo.parent / (self.repo.name + "-calls.log")

    def tearDown(self):
        rmtree(self.repo)
        for f in (self.log, self.log.with_name(self.log.name + ".tests")):
            if f.exists():
                f.unlink()

    def test_cap_holds_and_flaky_stays_flaky(self):
        e = dict(os.environ, SHIM_LOG=str(self.log))
        e.pop("CI", None)
        p = run_cli(["check", "--json", "--no-cache", "--base", "main~1", "--", "npm", "test"], self.repo, env=e)
        rep = json.loads(p.stdout)
        self.assertEqual(p.returncode, 1, p.stderr)
        v = {r["id"]: r for r in rep["results"]}
        self.assertEqual(rep["summary"]["new"], N)
        self.assertEqual(v["src/z.test.js > flips"]["verdict"], "flaky")
        for r in rep["results"]:
            if r["verdict"] == "new":
                # 5 runs per side behind each NEW (was up to 10 on base)
                self.assertEqual((r["head"]["runs"], r["base"]["runs"]), (5, 5), r["id"])
        calls = self.log.read_text().splitlines()
        # full head + full base, 4 head reruns (as 2 rounds of 2 copies), 4 base rounds; a few to spare
        self.assertLessEqual(len(calls), 12, calls)
        ran = self.log.with_name(self.log.name + ".tests").read_text().splitlines()
        total = 26
        # two full runs, then only the failing tests: 5 + 5 runs per NEW, a couple for the flaky one
        self.assertLessEqual(len(ran), 2 * total + 8 * N + 8, len(ran))
        self.assertEqual(rep["policy"]["base_confirm"], 5)

    def test_confirm_runs_option_raises_the_cap(self):
        pol = decide.Policy.from_runs(5, confirm_runs=8)
        self.assertEqual((pol.base_required, pol.base_runs), (5, 8))
        self.assertEqual(decide.Policy.from_runs(5, confirm_runs=2).base_runs, 5)  # never below required
        self.assertEqual(decide.Policy.from_runs(5).base_runs, 10)  # the pure default is unchanged


if __name__ == "__main__":
    unittest.main()
