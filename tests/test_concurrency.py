"""Head and base running at the same time: the collision guard, --serial, the speculative base run,
and surfacing tests that flip between runs as FLAKY."""
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path

from _support import commit, git_repo, git_state, have_pytest, mod, rmtree, run_cli, sh

engine = mod("engine")
model = mod("model")
decide = mod("decide")
brand = mod("brand")


def rr(output="", fail_msg="", problem=""):
    r = model.RunResult(output_tail=output, problem=problem)
    if fail_msg:
        r.tests["t"] = model.TestResult("t", "f", model.FAIL, "t", {}, fail_msg, fail_msg, fail_msg)
    return r


class CollisionSignTest(unittest.TestCase):
    def test_signs(self):
        for text in ("OSError: [Errno 98] Address already in use", "Error: listen EADDRINUSE: address already in use :::3000",
                     "sqlite3.OperationalError: database is locked", "bind: address already in use",
                     "port 5432 is already in use", "could not obtain lock on relation", "Text file busy",
                     "FileExistsError: [Errno 17] File exists: '/tmp/x'", "Error: EBUSY: resource busy or locked"):
            self.assertTrue(engine.collision_sign(rr(fail_msg=text)), text)
            self.assertTrue(engine.collision_sign(rr(output="noise\n" + text)), text)

    def test_no_false_alarm_on_ordinary_failures(self):
        for text in ("AssertionError: assert 99.9 == 90", "connect ECONNREFUSED 127.0.0.1:59999",
                     "ConnectionRefusedError: [Errno 111] Connection refused", "KeyError: 'DATABASE_URL'",
                     "ModuleNotFoundError: No module named 'psycopg2'", "expected 1 to be 2", "panic: runtime error"):
            self.assertEqual(engine.collision_sign(rr(fail_msg=text, output=text)), "", text)
        self.assertEqual(engine.collision_sign(None), "")


class FlipResultTest(unittest.TestCase):
    O = model.Obs

    def test_head_flip(self):
        st = decide.UnitState(model.Unit("t", "test", "f"), head=[self.O("pass"), self.O("fail", "k"), self.O("pass")])
        v = decide.flip_result(st)
        self.assertEqual((v["verdict"], v["flaky_side"], v["surfaced"]), ("flaky", "head", True))
        self.assertEqual((v["head"]["passed"], v["head"]["failed"]), (2, 1))

    def test_base_flip(self):
        st = decide.UnitState(model.Unit("t", "test", "f"), head=[self.O("pass")] * 3,
                              base=[self.O("fail", "k"), self.O("pass")])
        v = decide.flip_result(st)
        self.assertEqual((v["verdict"], v["flaky_side"]), ("flaky", "base"))
        self.assertIn("not fixed or broken", v["reason"])


FLIP = """import hashlib, os, pathlib

def _count():
    p = pathlib.Path(os.environ["BL_FLIP_DIR"]) / hashlib.md5(os.getcwd().encode()).hexdigest()
    n = int(p.read_text()) if p.exists() else 0
    p.write_text(str(n + 1))
    return n

"""


@unittest.skipUnless(have_pytest(), "pytest not installed")
class ConcurrencyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.flipdir = tempfile.mkdtemp()
        self.env = dict(os.environ, BL_FLIP_DIR=self.flipdir)
        self.env.pop(brand.ENV_PREFIX + "SERIAL", None)
        self.repos = []

    def tearDown(self):
        rmtree(self.flipdir)
        for r in self.repos:
            rmtree(r)

    def repo(self, base, head):
        r = git_repo(base)
        self.repos.append(r)
        for rel, body in head.items():
            (r / rel).parent.mkdir(parents=True, exist_ok=True)
            (r / rel).write_text(body)
        return r

    def check(self, repo, *extra):
        import sys
        p = run_cli(["check", "--json", "--no-cache", *extra, "--", sys.executable, "-m", "pytest", "-q"], repo,
                    env=self.env)
        try:
            return p.returncode, json.loads(p.stdout)
        except ValueError:
            self.fail(f"no JSON (exit {p.returncode}): {p.stderr[-2000:]}")

    def test_port_collision_is_redone_serially(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        hold = (f"import socket, time\n\ndef test_port():\n    s = socket.socket()\n"
                f"    s.bind(('127.0.0.1', {port}))\n    s.listen(1)\n    time.sleep(3)\n    s.close()\n")
        repo = self.repo({"tests/test_port.py": hold, "tests/test_n.py": "def test_n():\n    assert 1\n"},
                         {"tests/test_n.py": "def test_n():\n    assert 0\n"})
        before = git_state(repo)
        code, rep = self.check(repo)
        self.assertEqual(code, 1)
        self.assertTrue(rep["concurrency"]["serial"], rep["notes"])
        self.assertRegex(rep["concurrency"]["collision"], r"in use|Errno 98|Errno 48")
        verdicts = {v["id"]: v["verdict"] for v in rep["results"]}
        self.assertEqual(verdicts, {"tests/test_n.py::test_n": "new"}, "the collision must not leak into verdicts")
        self.assertTrue(any("redone serially" in n for n in rep["notes"]))
        self.assertEqual(git_state(repo), before)

    def test_serial_switches(self):
        repo = self.repo({"tests/test_n.py": "def test_n():\n    assert 1\n\ndef test_p():\n    assert 0\n"},
                         {"tests/test_n.py": "def test_n():\n    assert 0\n\ndef test_p():\n    assert 0\n"})
        _, conc = self.check(repo)
        _, ser = self.check(repo, "--serial")
        self.env[brand.ENV_PREFIX + "SERIAL"] = "1"
        _, ser_env = self.check(repo)
        self.assertFalse(conc["concurrency"]["serial"])
        self.assertTrue(ser["concurrency"]["serial"] and ser["concurrency"]["forced"])
        self.assertTrue(ser_env["concurrency"]["serial"])
        verdicts = [{v["id"]: v["verdict"] for v in r["results"]} for r in (conc, ser, ser_env)]
        self.assertEqual(verdicts[0], {"tests/test_n.py::test_n": "new", "tests/test_n.py::test_p": "pre_existing"})
        self.assertEqual(verdicts[0], verdicts[1])
        self.assertEqual(verdicts[0], verdicts[2])

    def test_speculative_base_run_is_cancelled_when_head_passes(self):
        repo = self.repo({"tests/test_a.py": "def test_a():\n    assert 1\n"}, {"x.txt": "dirty\n"})
        before = git_state(repo)
        code, rep = self.check(repo)
        self.assertEqual((code, rep["results"]), (0, []))
        self.assertEqual(git_state(repo), before)
        common = Path(sh(["git", "rev-parse", "--git-common-dir"], repo).stdout.strip())
        common = common if common.is_absolute() else repo / common
        self.assertTrue((common / brand.CACHE_DIR_NAME / "timing.json").exists())
        left = [d for d in os.listdir(tempfile.gettempdir()) if d.startswith(brand.TMP_PREFIX)
                and str(repo) in (Path(tempfile.gettempdir(), d, "owner.json").read_text()
                                  if Path(tempfile.gettempdir(), d, "owner.json").exists() else "")]
        self.assertEqual(left, [])

    def test_fixed_candidate_that_flips_on_base_is_surfaced_flaky(self):
        base_flip = FLIP + "def test_flip():\n    assert _count() != 0\n"   # fails only the first time
        repo = self.repo({"tests/test_f.py": base_flip, "tests/test_n.py": "def test_n():\n    assert 1\n"},
                         {"tests/test_f.py": "def test_flip():\n    pass\n",
                          "tests/test_n.py": "def test_n():\n    assert 0\n"})
        for extra in ([], ["--serial"]):
            for f in os.listdir(self.flipdir):
                os.unlink(os.path.join(self.flipdir, f))
            code, rep = self.check(repo, *extra)
            self.assertEqual(code, 1)
            by = {v["id"]: v for v in rep["results"]}
            v = by.get("tests/test_f.py::test_flip")
            self.assertIsNotNone(v, (extra, rep["results"]))
            self.assertEqual((v["verdict"], v["flaky_side"], v["surfaced"]), ("flaky", "base", True))
            self.assertEqual(rep["fixed"], [])
            self.assertEqual(rep["summary"]["flaky_surfaced"], 1)
            self.assertEqual(by["tests/test_n.py::test_n"]["verdict"], "new")

    def test_base_failure_does_not_cut_head_reruns_short(self):
        flip = FLIP + "def test_once():\n    assert _count() != 0\n"   # fails only on its first run
        repo = self.repo({"a.txt": "1\n"}, {"proj/tests/test_o.py": flip, "proj/tests/test_x.py":
                                            "def test_x():\n    assert 0\n"})
        import sys
        for extra in ([], ["--serial"]):
            for f in os.listdir(self.flipdir):
                os.unlink(os.path.join(self.flipdir, f))
            p = run_cli(["check", "--json", "--no-cache", "--cwd", str(repo / "proj"), *extra, "--", sys.executable,
                         "-m", "pytest", "-q"], repo, env=self.env)
            rep = json.loads(p.stdout)
            by = {v["id"]: v["verdict"] for v in rep["results"]}
            self.assertEqual(by.get("proj/tests/test_o.py::test_once"), "flaky", (extra, rep))
            self.assertEqual(by.get("proj/tests/test_x.py::test_x"), "unknown")
            self.assertEqual(p.returncode, 3)

    def test_flaky_hunt_surfaces_head_flips(self):
        flip = FLIP + "def test_h():\n    assert _count() != 1\n"   # fails only on the 2nd run of a side
        repo = self.repo({"tests/test_h.py": flip, "tests/test_n.py": "def test_n():\n    assert 1\n"},
                         {"tests/test_n.py": "def test_n():\n    assert 0\n"})
        code, rep = self.check(repo)
        self.assertNotIn("tests/test_h.py::test_h", {v["id"] for v in rep["results"]},
                         "without hunting, a test that passed in the head run is not rerun")
        for f in os.listdir(self.flipdir):
            os.unlink(os.path.join(self.flipdir, f))
        code, rep = self.check(repo, "--flaky-hunt", "2")
        self.assertEqual(code, 1)
        by = {v["id"]: v for v in rep["results"]}
        v = by["tests/test_h.py::test_h"]
        self.assertEqual((v["verdict"], v["flaky_side"], v["head"]["passed"], v["head"]["failed"]),
                         ("flaky", "head", 2, 1))
        self.assertEqual(by["tests/test_n.py::test_n"]["verdict"], "new")
        self.assertEqual(rep["policy"]["flaky_hunt"], 2)


if __name__ == "__main__":
    unittest.main()
