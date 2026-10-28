import unittest

from _support import mod

n = mod("normalize")


class NormalizeTest(unittest.TestCase):
    def test_repo_and_worktree_roots_become_relative(self):
        head = n.normalize("ImportError while importing test module '/home/u/repo/tests/test_x.py'",
                           ["/home/u/repo"])
        base = n.normalize("ImportError while importing test module '/tmp/bl-wt-abc/wt/tests/test_x.py'",
                           ["/tmp/bl-wt-abc/wt", "/home/u/repo"])
        self.assertEqual(head, base)
        self.assertIn("tests/test_x.py", head)

    def test_other_temp_paths_and_pytest_tmp(self):
        a = n.normalize("FileNotFoundError: '/tmp/pytest-of-u/pytest-12/test_a0/out.txt'")
        b = n.normalize("FileNotFoundError: '/tmp/pytest-of-u/pytest-13/test_a1/out.txt'")
        self.assertEqual(a, b)

    def test_addresses(self):
        a = n.normalize("<built-in method random of Random object at 0x2311a9c0>")
        b = n.normalize("<built-in method random of Random object at 0x7ffeef00>")
        self.assertEqual(a, b)

    def test_durations_and_timestamps(self):
        self.assertEqual(n.normalize("1 failed in 0.12s"), n.normalize("1 failed in 3.40s"))
        self.assertEqual(n.normalize("took 3ms"), n.normalize("took 120ms"))
        self.assertEqual(n.normalize("--- FAIL: TestX (0.00s)"), n.normalize("--- FAIL: TestX (1.25s)"))
        self.assertEqual(n.normalize("at 2026-10-28T08:52:54.898192+02:00 boom"),
                         n.normalize("at 2025-01-01T00:00:00Z boom"))

    def test_uuid_ansi_whitespace(self):
        self.assertEqual(n.normalize("id 123e4567-e89b-12d3-a456-426614174000"),
                         n.normalize("id 00000000-0000-0000-0000-000000000000"))
        self.assertEqual(n.normalize("\x1b[31mAssertionError\x1b[0m:   x"), "AssertionError: x")

    def test_ephemeral_ports_and_pids(self):
        self.assertEqual(n.normalize("connect ECONNREFUSED 127.0.0.1:54321"),
                         n.normalize("connect ECONNREFUSED 127.0.0.1:41234"))
        self.assertNotEqual(n.normalize("port 127.0.0.1:5432"), n.normalize("port 127.0.0.1:5433"))
        self.assertEqual(n.normalize("killed pid 4242"), n.normalize("killed pid 17"))

    def test_line_numbers_in_frames(self):
        self.assertEqual(n.normalize("cart_test.go:13: got 99.9 want 90"),
                         n.normalize("cart_test.go:15: got 99.9 want 90"))
        self.assertEqual(n.normalize('File "x.py", line 3, in f'), n.normalize('File "x.py", line 9, in f'))
        self.assertEqual(n.normalize("at test/a.test.js:4:69"), n.normalize("at test/a.test.js:7:1"))
        self.assertNotEqual(n.normalize("cart_test.go:13: x", line_numbers=False),
                            n.normalize("cart_test.go:15: x", line_numbers=False))

    def test_assertion_values_are_kept(self):
        self.assertNotEqual(n.normalize("assert 1.57 == 1.58"), n.normalize("assert 1.56 == 1.58"))
        self.assertNotEqual(n.normalize("AssertionError: expected 5 to be 7"),
                            n.normalize("AssertionError: expected 6 to be 7"))

    def test_identifiers_not_mangled(self):
        s = n.normalize("NameError: name 'template_name' is not defined; tmp_value=3")
        self.assertIn("template_name", s)
        self.assertIn("tmp_value", s)

    def test_temp_names(self):
        self.assertEqual(n.normalize("open tmpab12cd3x failed"), n.normalize("open tmp9zzq1k2_ failed"))

    def test_loose_masks_numbers(self):
        self.assertEqual(n.loose("assert 0.3686 > 0.5"), n.loose("assert 0.12 > 0.5"))
        self.assertNotEqual(n.loose("assert 0.3 > 0.5"), n.loose("assert 0.3 < 0.5"))

    def test_similarity(self):
        self.assertEqual(n.similarity("a", "a"), 1.0)
        self.assertEqual(n.similarity("", "a"), 0.0)


if __name__ == "__main__":
    unittest.main()
