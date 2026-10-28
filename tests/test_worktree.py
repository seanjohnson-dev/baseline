import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from _support import LAUNCHER, commit, git_repo, git_state, mod, rmtree, sh, write

wt = mod("worktree")
g = mod("gitutil")
brand = mod("brand")


class WorktreeLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo({"src/a.js": "export const a = 1;\n", ".gitignore": "node_modules/\n.env\n",
                              "package.json": json.dumps({"name": "r", "workspaces": ["packages/*"]}),
                              "packages/lib/index.js": "export const v = 'base';\n"})
        self.sha = sh(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()

    def tearDown(self):
        rmtree(self.repo)

    def test_create_and_remove_leave_no_trace(self):
        before = git_state(self.repo)
        w = wt.BaseWorktree(self.repo, self.sha)
        with w:
            self.assertTrue((w.path / "src" / "a.js").exists())
            self.assertFalse(str(w.path).startswith(str(self.repo)))
            tmp = w.tmp
        self.assertFalse(tmp.exists())
        self.assertEqual(git_state(self.repo), before)

    def test_cleanup_on_exception(self):
        before = git_state(self.repo)
        w = wt.BaseWorktree(self.repo, self.sha)
        with self.assertRaises(RuntimeError):
            with w:
                raise RuntimeError("boom")
        self.assertEqual(git_state(self.repo), before)

    def test_keep(self):
        w = wt.BaseWorktree(self.repo, self.sha, keep=True)
        with w:
            path = w.path
        self.assertTrue(path.exists())
        self.assertIn("kept base worktree", w.notes[0])
        sh(["git", "worktree", "remove", "--force", str(path)], self.repo)

    def test_prune_stale_from_dead_process(self):
        w = wt.BaseWorktree(self.repo, self.sha)
        w.create()
        (w.tmp / wt.OWNER_FILE).write_text(json.dumps({"pid": 999999999}))
        removed = wt.prune_stale(self.repo)
        self.assertEqual(len(removed), 1)
        self.assertFalse(w.tmp.exists())
        self.assertEqual(len(g.worktree_list(self.repo)), 1)

    def test_prune_keeps_live_worktrees(self):
        w = wt.BaseWorktree(self.repo, self.sha)
        with w:
            self.assertEqual(wt.prune_stale(self.repo), [])
            self.assertTrue(w.path.exists())

    def test_node_modules_mirror_and_workspace_links(self):
        nm = self.repo / "node_modules"
        (nm / "vitest").mkdir(parents=True)
        (nm / "vitest" / "package.json").write_text("{}")
        (nm / "@scope" / "pkg").mkdir(parents=True)
        (nm / ".vite").mkdir()
        os.symlink(self.repo / "packages" / "lib", nm / "lib")  # workspace link
        (self.repo / "packages" / "lib" / "node_modules" / "dep").mkdir(parents=True)
        with wt.BaseWorktree(self.repo, self.sha) as w:
            m = w.path / "node_modules"
            self.assertTrue(m.is_dir() and not m.is_symlink())
            self.assertEqual(os.path.realpath(m / "vitest"), str((nm / "vitest").resolve()))
            self.assertTrue((m / "@scope" / "pkg").exists())
            self.assertFalse((m / ".vite").exists(), "caches are not shared")
            self.assertEqual(os.path.realpath(m / "lib"), os.path.realpath(w.path / "packages" / "lib"),
                             "workspace link must point at the base copy, not HEAD's")
            self.assertTrue((w.path / "packages" / "lib" / "node_modules" / "dep").exists())

    def test_copy_ignored_is_opt_in(self):
        (self.repo / ".env").write_text("SECRET=1\n")
        with wt.BaseWorktree(self.repo, self.sha) as w:
            self.assertFalse((w.path / ".env").exists(), "secrets are not copied by default")
            self.assertEqual(wt.missing_ignored(self.repo, w.path), [".env"])
        with wt.BaseWorktree(self.repo, self.sha, copy_ignored=[".env"]) as w:
            self.assertEqual((w.path / ".env").read_text(), "SECRET=1\n")
            self.assertEqual(w.copied, [".env"])

    def test_bad_sha_raises_and_cleans(self):
        before = git_state(self.repo)
        with self.assertRaises(g.GitError):
            wt.BaseWorktree(self.repo, "0" * 40).create()
        self.assertEqual(git_state(self.repo), before)


class InterruptCleanupTest(unittest.TestCase):
    """SIGINT / SIGTERM during the base run must still remove the worktree."""

    def _run_and_signal(self, sig):
        repo = git_repo({"slow.sh": "#!/bin/sh\nif [ -f marker ]; then exit 1; fi\nsleep 30\n"})
        try:
            os.chmod(repo / "slow.sh", 0o755)
            commit(repo, "exec bit")
            (repo / "marker").write_text("")  # head fails fast; base (no marker) sleeps
            before = git_state(repo)
            p = subprocess.Popen([sys.executable, str(LAUNCHER), "check", "--json", "--runs", "1", "--", "./slow.sh"],
                                 cwd=str(repo), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 start_new_session=True)
            deadline = time.time() + 30
            while time.time() < deadline:
                if len(g.worktree_list(repo)) > 1:
                    break
                time.sleep(0.05)
            self.assertGreater(len(g.worktree_list(repo)), 1, "base worktree never appeared")
            time.sleep(0.3)
            os.kill(p.pid, sig)
            p.wait(timeout=30)
            self.assertNotEqual(p.returncode, 0)
            self.assertEqual(git_state(repo), before)
            left = [d for d in os.listdir(tempfile.gettempdir()) if d.startswith(wt.wt_marker())
                    and (Path(tempfile.gettempdir()) / d / wt.OWNER_FILE).exists()
                    and json.loads((Path(tempfile.gettempdir()) / d / wt.OWNER_FILE).read_text()).get("repo")
                    == str(repo)]
            self.assertEqual(left, [])
        finally:
            rmtree(repo)

    @unittest.skipIf(os.name != "posix", "posix signals")
    def test_sigint(self):
        self._run_and_signal(signal.SIGINT)

    @unittest.skipIf(os.name != "posix", "posix signals")
    def test_sigterm(self):
        self._run_and_signal(signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
