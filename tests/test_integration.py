"""End-to-end: tiny temp git repos with pytest (skipped without pytest) and go (skipped without go).

Each repo has the fixture cases: NEW, NEW load error, PRE, PRE (error changed), ENV, plus a passing test,
in both modes (change committed on a feature branch / uncommitted on main). Every run asserts that
`git status`, `git worktree list`, stash, refs and HEAD are unchanged afterwards.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

from _support import brand, commit, git_repo, git_state, have_pytest, rmtree, run_cli, sh, write

PY_BASE = {
    ".gitignore": "__pycache__/\n",
    "pytest.ini": "[pytest]\ntestpaths = tests\n",
    "shop/__init__.py": "",
    "shop/cart.py": "def total(p, d=0):\n    return round(p * (1 - d), 2)\n",
    "shop/ship.py": "def ship(kg):\n    return 5\n",
    "shop/report.py": "def summary(o):\n    return f'{len(o)} items'\n",
    "tests/__init__.py": "",
    "tests/test_cart.py": "from shop.cart import total\n\ndef test_ok():\n    assert total(10) == 10\n\n"
                          "def test_new():\n    assert total(100, 0.1) == 90\n",
    "tests/test_ship.py": "from shop.ship import ship\n\ndef test_changed():\n    assert ship(2) == 7\n",
    "tests/test_pre.py": "import pytest\n\n@pytest.mark.parametrize('x', [1, 2])\n"
                         "def test_pre(x):\n    assert x == 3\n",
    "tests/test_report.py": "from shop.report import summary\n\ndef test_summary():\n"
                            "    assert summary([1]) == '1 items'\n",
    "tests/test_env.py": "import os\n\ndef test_env():\n    assert os.environ['BL_TEST_DB_URL']\n",
}
PY_HEAD = {
    "shop/cart.py": "def total(p, d=0):\n    return round(p - d, 2)\n",
    "shop/ship.py": "def ship(kg):\n    return 6\n",
    "shop/report.py": None,
    "shop/report2.py": "def summary(o):\n    return f'{len(o)} items'\n",
}
PY_EXPECT = {
    "tests/test_cart.py::test_new": "new",
    "tests/test_report.py": "new",
    "tests/test_ship.py::test_changed": "pre_existing_error_changed",
    "tests/test_pre.py::test_pre[1]": "pre_existing",
    "tests/test_pre.py::test_pre[2]": "pre_existing",
    "tests/test_env.py::test_env": "environment",
}


def make(base, head, mode):
    repo = git_repo(base)
    if mode == "branch":
        sh(["git", "checkout", "-q", "-b", "feature"], repo)
        write(repo, head)
        commit(repo, "feature")
    else:
        write(repo, head)
    return repo


def check(repo, cmd, extra=()):
    before = git_state(repo)
    p = run_cli(["check", "--json", "--no-cache"] + list(extra) + ["--"] + cmd, repo)
    try:
        rep = json.loads(p.stdout)
    except ValueError:
        raise AssertionError(f"no JSON (exit {p.returncode}): {p.stdout}\n{p.stderr}")
    after = git_state(repo)
    return p, rep, before, after


@unittest.skipUnless(have_pytest(), "pytest not installed")
class PytestIntegrationTest(unittest.TestCase):
    def run_mode(self, mode):
        repo = make(PY_BASE, PY_HEAD, mode)
        try:
            p, rep, before, after = check(repo, [sys.executable, "-m", "pytest", "-q"], ["--runs", "3"])
            got = {v["id"]: v["verdict"] for v in rep["results"]}
            self.assertEqual(got, PY_EXPECT, json.dumps(rep["results"], indent=1)[:3000])
            self.assertEqual(p.returncode, 1)
            self.assertEqual(before, after, "git state changed")
            le = next(v for v in rep["results"] if v["id"] == "tests/test_report.py")
            self.assertEqual(le["kind"], "load_error")
            self.assertEqual(le["blocked_tests"], ["tests/test_report.py::test_summary"])
            self.assertFalse(any(n.startswith("__pycache__") or "/__pycache__" in n
                                 for n in sh(["git", "status", "--porcelain", "--ignored"], repo).stdout.split()),
                             "pyc files must not land in the working tree")
            self.assertEqual(rep["head"]["dirty"], mode == "dirty")
        finally:
            rmtree(repo)

    def test_branch_mode(self):
        self.run_mode("branch")

    def test_dirty_mode(self):
        self.run_mode("dirty")

    def test_fixed_missing_and_text_report(self):
        base = {"tests/test_a.py": "def test_fixme():\n    assert 0\n\ndef test_gone():\n    pass\n\n"
                                   "def test_broken():\n    assert 0\n"}
        head = {"tests/test_a.py": "def test_fixme():\n    assert 1\n\ndef test_broken():\n    assert 0\n"}
        repo = make(base, head, "dirty")
        try:
            p = run_cli(["check", "--no-cache", "--runs", "2", "--", sys.executable, "-m", "pytest"], repo)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn("Fixed (failed every base run, passed every head run): tests/test_a.py::test_fixme",
                          p.stdout)
            self.assertIn("Missing on head", p.stdout)
            self.assertIn("tests/test_a.py::test_gone", p.stdout)
            self.assertTrue(p.stdout.splitlines()[0].startswith(f"{brand.NAME}: 1 pre-existing"),
                            p.stdout.splitlines()[0])
        finally:
            rmtree(repo)

    def test_new_test_added_on_head(self):
        repo = make({"tests/test_a.py": "def test_old():\n    pass\n"},
                    {"tests/test_a.py": "def test_old():\n    pass\n\ndef test_added():\n    assert 0\n",
                     "tests/test_b.py": "def test_newfile():\n    assert 0\n"}, "dirty")
        try:
            p, rep, before, after = check(repo, [sys.executable, "-m", "pytest"], ["--runs", "2", "--full-base",
                                                                                    "never"])
            got = {v["id"]: v["verdict"] for v in rep["results"]}
            self.assertEqual(got, {"tests/test_a.py::test_added": "new", "tests/test_b.py::test_newfile": "new"})
            self.assertEqual(before, after)
        finally:
            rmtree(repo)

    def test_cache_reuses_base(self):
        repo = make(PY_BASE, PY_HEAD, "dirty")
        try:
            cmd = ["check", "--json", "--runs", "2", "--", sys.executable, "-m", "pytest", "-q"]
            r1 = json.loads(run_cli(cmd, repo).stdout)
            r2 = json.loads(run_cli(cmd, repo).stdout)
            self.assertFalse(r1["cache"]["hit"])
            self.assertTrue(r2["cache"]["hit"])
            self.assertEqual({v["id"]: v["verdict"] for v in r1["results"]},
                             {v["id"]: v["verdict"] for v in r2["results"]})
            self.assertTrue(any(v["base"]["cached_runs"] for v in r2["results"]))
            env = dict(os.environ, BL_TEST_DB_URL="")
            env["SOME_NEW_VAR"] = "1"
            r3 = json.loads(run_cli(cmd, repo, env=env).stdout)
            self.assertFalse(r3["cache"]["hit"], "a different environment must not reuse cached base results")
        finally:
            rmtree(repo)

    def test_editable_install_base_imports_its_own_code(self):
        """With `pip install -e .`, base would import HEAD's code unless we intervene."""
        base = {"src/calc/__init__.py": "def f():\n    return 1\n",
                "tests/test_calc.py": "import calc\n\ndef test_f():\n    assert calc.f() == 1\n"}
        repo = make(base, {"src/calc/__init__.py": "def f():\n    return 2\n"}, "dirty")
        venv = repo.parent / (repo.name + "-venv")
        try:
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
            site = subprocess.run([str(venv / "bin" / "python"), "-c",
                                   "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                  capture_output=True, text=True, check=True).stdout.strip()
            import pytest as _p
            deps = os.path.dirname(os.path.dirname(_p.__file__))
            with open(os.path.join(site, "_deps.pth"), "w") as f:
                f.write(deps + "\n")
            with open(os.path.join(site, "__editable__.calc-0.1.pth"), "w") as f:
                f.write(str(repo / "src") + "\n")  # what setuptools writes for a src-layout editable install
            env = dict(os.environ, PATH=str(venv / "bin") + os.pathsep + os.environ["PATH"], VIRTUAL_ENV=str(venv))
            env.pop("PYTHONPATH", None)
            p = run_cli(["check", "--json", "--no-cache", "--runs", "2", "--", "python", "-m", "pytest"], repo,
                        env=env)
            rep = json.loads(p.stdout)
            self.assertEqual([v["verdict"] for v in rep["results"]], ["new"], json.dumps(rep, indent=1)[:2000])
            self.assertTrue(any("editable install" in n for n in rep["notes"]))
        finally:
            rmtree(repo)
            rmtree(venv)


GO_BASE = {
    "go.mod": "module example.com/m\n\ngo 1.21\n",
    "cart/cart.go": "package cart\n\nfunc Total(p, d float64) float64 { return p * (1 - d) }\n",
    "cart/cart_test.go": "package cart\n\nimport \"testing\"\n\nfunc TestOK(t *testing.T) {\n\tif Total(10, 0) != 10 "
                         "{\n\t\tt.Fatal(\"bad\")\n\t}\n}\n\nfunc TestNew(t *testing.T) {\n\tif got := Total(100, 0.5); "
                         "got != 50 {\n\t\tt.Fatalf(\"got %v want 50\", got)\n\t}\n}\n",
    "ship/ship.go": "package ship\n\nfunc Ship() int { return 5 }\n",
    "ship/ship_test.go": "package ship\n\nimport \"testing\"\n\nfunc TestChanged(t *testing.T) {\n\tif got := Ship(); "
                         "got != 7 {\n\t\tt.Fatalf(\"got %d want 7\", got)\n\t}\n}\n",
    "rep/rep.go": "package rep\n\nfunc Summary() string { return \"x\" }\n",
    "rep/rep_test.go": "package rep\n\nimport \"testing\"\n\nfunc TestSummary(t *testing.T) {\n\tif Summary() != \"x\" "
                       "{\n\t\tt.Fatal(\"bad\")\n\t}\n}\n",
    "envt/env_test.go": "package envt\n\nimport (\n\t\"os\"\n\t\"testing\"\n)\n\nfunc TestEnv(t *testing.T) {\n"
                        "\tif os.Getenv(\"BL_TEST_DB_URL\") == \"\" {\n\t\tt.Fatal(\"BL_TEST_DB_URL not set\")\n\t}\n}\n",
}
GO_HEAD = {
    "cart/cart.go": "package cart\n\nfunc Total(p, d float64) float64 { return p - d }\n",
    "ship/ship.go": "package ship\n\nfunc Ship() int { return 6 }\n",
    "rep/rep.go": "package rep\n\nfunc SummaryV2() string { return \"x\" }\n",
}
GO_EXPECT = {
    "example.com/m/cart::TestNew": "new",
    "example.com/m/rep": "new",
    "example.com/m/ship::TestChanged": "pre_existing_error_changed",
    "example.com/m/envt::TestEnv": "environment",
}


@unittest.skipUnless(shutil.which("go"), "go not installed")
class GoIntegrationTest(unittest.TestCase):
    def run_mode(self, mode):
        repo = make(GO_BASE, GO_HEAD, mode)
        try:
            p, rep, before, after = check(repo, ["go", "test", "./..."], ["--runs", "3"])
            got = {v["id"]: v["verdict"] for v in rep["results"]}
            self.assertEqual(got, GO_EXPECT, json.dumps(rep["results"], indent=1)[:3000])
            self.assertEqual(p.returncode, 1)
            self.assertEqual(before, after)
            le = next(v for v in rep["results"] if v["id"] == "example.com/m/rep")
            self.assertEqual(le["blocked_tests"], ["example.com/m/rep::TestSummary"])
        finally:
            rmtree(repo)

    def test_branch_mode(self):
        self.run_mode("branch")

    def test_dirty_mode(self):
        self.run_mode("dirty")


if __name__ == "__main__":
    unittest.main()
