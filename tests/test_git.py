import unittest
from pathlib import Path

from _support import commit, git_repo, mod, rmtree, sh, write

g = mod("gitutil")


class BaseResolutionTest(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo({"a.txt": "a\n"})
        self.main = sh(["git", "rev-parse", "HEAD"], self.repo).stdout.strip()

    def tearDown(self):
        rmtree(self.repo)

    def test_on_default_branch_base_is_head(self):
        b = g.resolve_base(self.repo, None)
        self.assertEqual(b.sha, self.main)
        self.assertEqual(b.ref, "HEAD")

    def test_feature_branch_uses_merge_base(self):
        sh(["git", "checkout", "-q", "-b", "feat"], self.repo)
        write(self.repo, {"b.txt": "b\n"})
        commit(self.repo, "feat")
        sh(["git", "checkout", "-q", "main"], self.repo)
        write(self.repo, {"c.txt": "main moved on\n"})
        commit(self.repo, "main2")
        sh(["git", "checkout", "-q", "feat"], self.repo)
        b = g.resolve_base(self.repo, None)
        self.assertEqual(b.sha, self.main, "merge-base, not main's tip")
        self.assertEqual(b.ref, "main")

    def test_branch_without_divergence_is_head(self):
        sh(["git", "checkout", "-q", "-b", "feat"], self.repo)
        b = g.resolve_base(self.repo, None)
        self.assertEqual(b.sha, self.main)
        self.assertEqual(b.ref, "HEAD")

    def test_master_and_origin_head(self):
        repo = git_repo({"x": "1"}, branch="master")
        try:
            self.assertEqual(g.default_branch(repo).name, "master")
            sh(["git", "checkout", "-q", "-b", "trunk"], repo)
            sh(["git", "branch", "-q", "-D", "master"], repo)
            sh(["git", "update-ref", "refs/remotes/origin/trunk", "HEAD"], repo)
            sh(["git", "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk"], repo)
            d = g.default_branch(repo)
            self.assertEqual((d.ref, d.name), ("origin/trunk", "trunk"))
        finally:
            rmtree(repo)

    def test_no_default_branch(self):
        sh(["git", "branch", "-m", "main", "dev"], self.repo)
        b = g.resolve_base(self.repo, None)
        self.assertEqual(b.sha, self.main)
        self.assertIn("no default branch", b.how)

    def test_explicit_base_and_errors(self):
        write(self.repo, {"b.txt": "b\n"})
        commit(self.repo, "second")
        b = g.resolve_base(self.repo, "HEAD~1")
        self.assertEqual(b.sha, self.main)
        self.assertEqual(b.warning, "")
        with self.assertRaises(g.GitError):
            g.resolve_base(self.repo, "no-such-ref")

    def test_explicit_base_not_ancestor_warns(self):
        sh(["git", "checkout", "-q", "-b", "other"], self.repo)
        write(self.repo, {"o.txt": "o\n"})
        commit(self.repo, "other")
        sh(["git", "checkout", "-q", "main"], self.repo)
        w = g.resolve_base(self.repo, "other").warning
        self.assertIn("`--base other` is ahead of HEAD", w)
        self.assertIn("--base HEAD", w)
        # diverged: says so, and names the merge-base by its sha
        write(self.repo, {"m.txt": "m\n"})
        commit(self.repo, "main moves on")
        w = g.resolve_base(self.repo, "other").warning
        self.assertEqual(w, "`--base other` is not part of this branch's history (the branches have diverged), so "
                            "the comparison may include changes that exist on other but not here. For just this "
                            f"branch's own changes use `--base {g.short(self.main)}` (the merge-base: "
                            "`git merge-base HEAD other`).")

    def test_no_warning_in_the_ordinary_cases(self):
        write(self.repo, {"b.txt": "b\n"})
        commit(self.repo, "second")
        for base in ("HEAD", "HEAD~1", "main", self.main):
            self.assertEqual(g.resolve_base(self.repo, base).warning, "", base)
        sh(["git", "checkout", "-q", "-b", "feature"], self.repo)
        write(self.repo, {"f.txt": "f\n"})
        commit(self.repo, "feature")
        self.assertEqual(g.resolve_base(self.repo, None).warning, "")
        self.assertEqual(g.resolve_base(self.repo, "main").warning, "")

    def test_unborn_head(self):
        repo = Path(__import__("tempfile").mkdtemp())
        try:
            sh(["git", "init", "-q"], repo)
            with self.assertRaises(g.GitError):
                g.resolve_base(repo, None)
        finally:
            rmtree(repo)

    def test_dirty_and_changed_files(self):
        self.assertFalse(g.is_dirty(self.repo))
        write(self.repo, {"a.txt": "changed\n", "new.py": "x"})
        self.assertTrue(g.is_dirty(self.repo))
        self.assertEqual(g.changed_files(self.repo, self.main), ["a.txt", "new.py"])
        (self.repo / "a.txt").unlink()
        self.assertIn("a.txt", g.changed_files(self.repo, self.main))
        self.assertTrue(g.exists_at(self.repo, self.main, "a.txt"))
        self.assertFalse(g.exists_at(self.repo, self.main, "new.py"))
        self.assertEqual(g.show(self.repo, self.main, "a.txt"), "a\n")

    def test_status_does_not_write_index(self):
        idx = self.repo / ".git" / "index"
        before = idx.stat().st_mtime_ns
        write(self.repo, {"a.txt": "a\n"})  # same content, new mtime -> stat-dirty index entry
        g.is_dirty(self.repo)
        g.changed_files(self.repo, self.main)
        self.assertEqual(idx.stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
