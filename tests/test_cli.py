"""CLI behaviour and exit codes, using the generic runner (shell scripts) so no test framework is needed."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from _support import commit, git_repo, git_state, mod, rmtree, run_cli, sh, write

brand = mod("brand")
pkg = __import__(mod("brand").__name__.split(".")[0])


def script_repo(body_base, body_head=None, dirty=True):
    repo = git_repo({"t.sh": body_base})
    os.chmod(repo / "t.sh", 0o755)
    commit(repo, "exec")
    if body_head is not None:
        (repo / "t.sh").write_text(body_head)
        os.chmod(repo / "t.sh", 0o755)
        if not dirty:
            sh(["git", "checkout", "-q", "-b", "feat"], repo)
            commit(repo, "change")
    return repo


class CliTest(unittest.TestCase):
    def test_version(self):
        p = run_cli(["version"], ".")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), f"{brand.NAME} {pkg.__version__}")

    def test_no_args_and_bad_option(self):
        self.assertEqual(run_cli([], ".").returncode, 2)
        self.assertEqual(run_cli(["check", "--nope"], ".").returncode, 2)

    def test_not_a_repo(self):
        d = tempfile.mkdtemp()
        p = run_cli(["check", "--json", "--", "true"], d)
        self.assertEqual(p.returncode, 2)
        self.assertIn("error", json.loads(p.stdout))

    def test_command_not_found(self):
        repo = git_repo({"a": "1"})
        try:
            p = run_cli(["check", "--", "./definitely-missing"], repo)
            self.assertEqual(p.returncode, 2)
            self.assertIn("not found", p.stderr)
        finally:
            rmtree(repo)

    def test_bad_base_is_usage_error(self):
        repo = git_repo({"a": "1"})
        try:
            self.assertEqual(run_cli(["check", "--base", "nope", "--", "true"], repo).returncode, 2)
        finally:
            rmtree(repo)

    def test_unborn_repo_cannot_establish_base(self):
        d = Path(tempfile.mkdtemp())
        try:
            sh(["git", "init", "-q"], d)
            p = run_cli(["check", "--", "true"], d)
            self.assertEqual(p.returncode, 3, p.stderr)
        finally:
            rmtree(d)

    def test_all_pass_exit_0(self):
        repo = script_repo("#!/bin/sh\nexit 0\n")
        try:
            p = run_cli(["check", "--", "./t.sh"], repo)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("all 1 tests passed", p.stdout)
        finally:
            rmtree(repo)

    def test_generic_new_failure_exit_1_and_last_json(self):
        repo = script_repo("#!/bin/sh\nexit 0\n", "#!/bin/sh\necho broken; exit 1\n")
        try:
            before = git_state(repo)
            p = run_cli(["check", "--runs", "2", "--", "./t.sh"], repo)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("can't attribute per test", p.stdout)
            self.assertIn("NEW", p.stdout.splitlines()[0])
            self.assertTrue(p.stdout.strip().splitlines()[-2].startswith("Agent:"))
            last = json.loads((repo / ".git" / brand.CACHE_DIR_NAME / "last.json").read_text())
            self.assertEqual(last["results"][0]["verdict"], "new")
            self.assertEqual(git_state(repo), before, "check must not change git state")
        finally:
            rmtree(repo)

    def test_generic_pre_existing_exit_0_json_only(self):
        repo = script_repo("#!/bin/sh\nexit 3\n", "#!/bin/sh\n# edited\nexit 3\n")
        try:
            p = run_cli(["check", "--json", "--runs", "2", "--", "./t.sh"], repo)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            rep = json.loads(p.stdout)  # stdout is pure JSON
            v = rep["results"][0]
            self.assertEqual(v["verdict"], "pre_existing")
            self.assertTrue(v["dirty"])
            self.assertEqual(v["base_sha"], rep["base"]["sha"])
            for k in ("reason", "head", "base", "runs_total"):
                self.assertIn(k, v)
        finally:
            rmtree(repo)

    def test_changed_error_and_strict_flag(self):
        repo = script_repo("#!/bin/sh\nexit 3\n", "#!/bin/sh\nexit 4\n")
        try:
            p = run_cli(["check", "--json", "--runs", "2", "--", "./t.sh"], repo)
            self.assertEqual(p.returncode, 0)
            self.assertEqual(json.loads(p.stdout)["results"][0]["verdict"], "pre_existing_error_changed")
            p2 = run_cli(["check", "--json", "--runs", "2", "--strict-changed", "--", "./t.sh"], repo)
            self.assertEqual(p2.returncode, 1)
        finally:
            rmtree(repo)

    def test_committed_change_on_branch(self):
        repo = script_repo("#!/bin/sh\nexit 0\n", "#!/bin/sh\nexit 1\n", dirty=False)
        try:
            p = run_cli(["check", "--json", "--runs", "2", "--", "./t.sh"], repo)
            rep = json.loads(p.stdout)
            self.assertEqual(p.returncode, 1)
            self.assertFalse(rep["head"]["dirty"])
            self.assertEqual(rep["base"]["ref"], "main")
        finally:
            rmtree(repo)

    def test_flaky_whole_command(self):
        repo = git_repo({"t.sh": "#!/bin/sh\nif [ -f .once ]; then exit 0; fi\ntouch .once; exit 1\n",
                         ".gitignore": ".once\n"})
        try:
            os.chmod(repo / "t.sh", 0o755)
            commit(repo, "x")
            p = run_cli(["check", "--json", "--", "./t.sh"], repo)
            self.assertEqual(json.loads(p.stdout)["results"][0]["verdict"], "flaky")
            self.assertEqual(p.returncode, 0)
        finally:
            rmtree(repo)

    def test_detect(self):
        d = Path(tempfile.mkdtemp())
        (d / "go.mod").write_text("module x\n")
        p = run_cli(["detect", "--json", "--cwd", str(d)], ".")
        self.assertEqual(json.loads(p.stdout)["runner"], "go")
        p2 = run_cli(["detect", "--", "npx", "vitest", "run"], d)
        self.assertTrue(p2.stdout.startswith("vitest:"))
        e = Path(tempfile.mkdtemp())
        self.assertEqual(run_cli(["detect"], e).returncode, 2)

    def test_module_entry_point(self):
        import subprocess
        import sys
        from _support import PKG, ROOT
        p = subprocess.run([sys.executable, "-m", PKG, "version"], cwd=str(ROOT), capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn(brand.NAME, p.stdout)


class BaseUntrustedTest(unittest.TestCase):
    def test_base_crash_is_exit_3_not_pre_existing(self):
        from _support import have_pytest
        if not have_pytest():
            self.skipTest("pytest not installed")
        import sys
        # base has a broken pytest.ini (pytest exits with a usage error and writes no report)
        repo = git_repo({"pytest.ini": "[pytest]\naddopts = --definitely-not-an-option\n",
                         "tests/test_a.py": "def test_a():\n    assert 1 == 2\n"})
        try:
            write(repo, {"pytest.ini": "[pytest]\n"})
            p = run_cli(["check", "--json", "--no-cache", "--", sys.executable, "-m", "pytest"], repo)
            rep = json.loads(p.stdout)
            self.assertEqual(p.returncode, 3, p.stdout)
            self.assertEqual(rep["results"][0]["verdict"], "unknown")
            self.assertIn("must not be reported as pre-existing", rep["results"][0]["reason"])
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
