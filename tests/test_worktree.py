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
        self.assertIn(str(w.tmp), removed)
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


HOLDER = """
import sys, time
sys.path.insert(0, {root!r})
from {pkg} import worktree
from pathlib import Path
w = worktree.BaseWorktree(Path({repo!r}), {sha!r})
w.create()
print(w.tmp, flush=True)
time.sleep(60)
"""


class StaleCleanupTest(unittest.TestCase):
    """Leftovers of killed runs are removed; live runs and anything not ours are never touched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="t-tmproot-")).resolve()
        self.old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(self.tmp)
        self.repo = git_repo({"a.txt": "1\n"})
        self.sha = sh(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            if p.stdout:
                p.stdout.close()
        tempfile.tempdir = self.old_tempdir
        rmtree(self.tmp)
        rmtree(self.repo)

    def holder(self):
        from _support import PKG, ROOT
        env = dict(os.environ, TMPDIR=str(self.tmp))
        p = subprocess.Popen([sys.executable, "-c", HOLDER.format(root=str(ROOT), pkg=PKG, repo=str(self.repo),
                                                                   sha=self.sha)],
                             stdout=subprocess.PIPE, text=True, env=env)
        self.procs.append(p)
        path = Path(p.stdout.readline().strip())
        self.assertTrue((path / "wt" / "a.txt").exists())
        return p, path

    def test_owner_file(self):
        w = wt.BaseWorktree(self.repo, self.sha)
        with w:
            owner = json.loads((w.tmp / wt.OWNER_FILE).read_text())
            self.assertEqual(owner["pid"], os.getpid())
            self.assertEqual(owner["start"], wt.proc_start(os.getpid()))
            self.assertEqual(Path(owner["git_dir"]), (self.repo / ".git").resolve())
            self.assertTrue(str(w.tmp).startswith(str(self.tmp)))

    @unittest.skipIf(os.name != "posix", "posix signals")
    def test_sigkilled_run_is_cleaned_and_live_runs_are_not(self):
        p1, path1 = self.holder()
        p2, path2 = self.holder()
        mine = wt.BaseWorktree(self.repo, self.sha)
        mine.create()
        try:
            self.assertEqual(len(g.worktree_list(self.repo)), 4)
            self.assertEqual(wt.find_stale(), [], "live runs (incl. this one) are not stale")
            self.assertEqual(wt.cleanup_stale(self.repo), [])
            self.assertTrue(path1.exists() and path2.exists() and mine.path.exists())
            p1.send_signal(signal.SIGKILL)
            p1.wait()
            stale = wt.find_stale()
            self.assertEqual([s.path for s in stale], [path1])
            self.assertIn("is gone", stale[0].reason)
            removed = wt.cleanup_stale(self.repo)
            self.assertEqual(removed, [str(path1)])
            self.assertFalse(path1.exists())
            self.assertTrue(path2.exists() and mine.path.exists(), "a concurrent run's worktree was touched")
            listed = g.worktree_list(self.repo)
            self.assertEqual(len(listed), 3)
            self.assertNotIn(str(path1 / "wt"), listed)
        finally:
            mine.remove()

    def test_reused_pid_is_not_alive(self):
        self.assertTrue(wt.owner_alive({"pid": os.getpid(), "start": wt.proc_start(os.getpid())}))
        if wt.proc_start(os.getpid()) is not None:
            self.assertFalse(wt.owner_alive({"pid": os.getpid(), "start": "jiffies:1"}))
        self.assertFalse(wt.owner_alive({"pid": 999999999}))
        self.assertFalse(wt.owner_alive({}))

    def _legacy(self, age):
        d = Path(tempfile.mkdtemp(prefix=wt.wt_marker()))
        (d / "wt").mkdir()
        (d / "wt" / ".git").write_text(f"gitdir: {self.repo}/.git/worktrees/wt9\n")
        old = time.time() - age
        os.utime(d, (old, old))
        return d

    def test_legacy_dirs_without_owner(self):
        fresh = self._legacy(60)
        old = self._legacy(2 * 3600)
        stale = [s.path for s in wt.find_stale()]
        self.assertEqual(stale, [old])
        if Path("/proc").is_dir():
            busy = subprocess.Popen(["sleep", "30"], cwd=str(old / "wt"))
            self.procs.append(busy)
            time.sleep(0.2)
            self.assertEqual(wt.find_stale(), [], "a process works inside it")
            busy.kill()
            busy.wait()
        self.assertEqual(wt.cleanup_stale(), [str(old)])
        self.assertTrue(fresh.exists())

    def test_never_touches_what_is_not_ours(self):
        dead = json.dumps({"pid": 999999999})
        odd = Path(tempfile.mkdtemp(prefix=wt.wt_marker()))
        (odd / wt.OWNER_FILE).write_text(dead)
        (odd / "precious.txt").write_text("x")                  # unexpected content
        notwt = Path(tempfile.mkdtemp(prefix=wt.wt_marker()))
        (notwt / wt.OWNER_FILE).write_text(dead)
        (notwt / "wt").mkdir()
        (notwt / "wt" / "file").write_text("x")                 # wt/ that is not a linked worktree
        other = Path(tempfile.mkdtemp(prefix="someone-else-wt-"))
        (other / wt.OWNER_FILE).write_text(dead)
        self.assertEqual(wt.find_stale(), [])
        self.assertEqual(wt.cleanup_stale(), [])
        self.assertTrue(odd.exists() and notwt.exists() and other.exists())
        outside = Path(tempfile.mkdtemp(dir=str(self.repo), prefix=wt.wt_marker()))
        self.assertFalse(wt.remove_stale(wt.Stale(outside, "wt-", "test")), "only dirs directly in the tmp dir")
        self.assertTrue(outside.exists())
        rmtree(outside)

    def test_clean_subcommand(self):
        w = wt.BaseWorktree(self.repo, self.sha)
        w.create()
        (w.tmp / wt.OWNER_FILE).write_text(json.dumps({"pid": 999999999, "git_dir": str(self.repo / ".git")}))
        env = dict(os.environ, TMPDIR=str(self.tmp))
        p = subprocess.run([sys.executable, str(LAUNCHER), "clean", "--dry-run"], env=env, capture_output=True,
                           text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("would remove: " + str(w.tmp), p.stdout)
        self.assertTrue(w.tmp.exists())
        p = subprocess.run([sys.executable, str(LAUNCHER), "clean", "--json"], env=env, capture_output=True, text=True)
        rows = json.loads(p.stdout)["stale"]
        self.assertEqual([(r["path"], r["removed"]) for r in rows], [(str(w.tmp), True)])
        self.assertFalse(w.tmp.exists())
        self.assertEqual(len(g.worktree_list(self.repo)), 1)
        p = subprocess.run([sys.executable, str(LAUNCHER), "clean"], env=env, capture_output=True, text=True)
        self.assertIn("no stale", p.stdout)


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
            p.communicate(timeout=30)
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
