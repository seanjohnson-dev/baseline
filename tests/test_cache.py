import tempfile
import unittest
from pathlib import Path

from _support import mod

c = mod("cache")
m = mod("model")


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()) / "cache"

    def test_roundtrip(self):
        bc = c.BaseCache(self.dir, "abc", "key")
        self.assertFalse(bc.hit)
        bc.add("t1", m.Obs(m.FAIL, "k", "msg", "raw"))
        bc.set_full({"tests": {"t1": "fail"}})
        bc.save()
        again = c.BaseCache(self.dir, "abc", "key")
        self.assertTrue(again.hit)
        obs = again.obs("t1")
        self.assertEqual((obs[0].status, obs[0].key, obs[0].cached), (m.FAIL, "k", True))
        self.assertEqual(again.full["tests"], {"t1": "fail"})
        self.assertTrue(again.path.name.startswith("abc-"))

    def test_key_and_sha_invalidate(self):
        bc = c.BaseCache(self.dir, "abc", "key")
        bc.add("t1", m.Obs(m.PASS))
        bc.save()
        self.assertFalse(c.BaseCache(self.dir, "abc", "other-key").hit)
        self.assertFalse(c.BaseCache(self.dir, "def", "key").hit)

    def test_disabled(self):
        bc = c.BaseCache(self.dir, "abc", "key", enabled=False)
        bc.add("t1", m.Obs(m.PASS))
        bc.save()
        self.assertFalse(self.dir.exists())
        self.assertEqual(bc.obs("t1"), [])

    def test_corrupt_file_is_ignored(self):
        self.dir.mkdir(parents=True)
        bc = c.BaseCache(self.dir, "abc", "key")
        bc.path.write_text("{not json")
        self.assertFalse(c.BaseCache(self.dir, "abc", "key").hit)

    def test_prune(self):
        for i in range(c.KEEP_FILES + 5):
            b = c.BaseCache(self.dir, f"sha{i}", "k")
            b.save()
        self.assertLessEqual(len(list(self.dir.glob("*.json"))), c.KEEP_FILES)

    def test_env_fingerprint(self):
        a = c.env_fingerprint({"PATH": "/bin", "PWD": "/x", "DATABASE_URL": "pg"})
        self.assertEqual(a, c.env_fingerprint({"PATH": "/bin", "PWD": "/y", "DATABASE_URL": "pg"}))
        self.assertNotEqual(a, c.env_fingerprint({"PATH": "/bin", "PWD": "/x"}), "env var presence matters")
        self.assertEqual(c.env_fingerprint({"PATH": "/bin", "X_RUNS": "3"}, ignore_prefix="X_"),
                         c.env_fingerprint({"PATH": "/bin"}))
        f = Path(tempfile.mkdtemp()) / "lock"
        f.write_text("1")
        self.assertNotEqual(c.env_fingerprint({}, [f]), c.env_fingerprint({}))


if __name__ == "__main__":
    unittest.main()
