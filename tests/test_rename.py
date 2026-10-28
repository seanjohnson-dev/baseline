"""The product name lives only in brand.py, and scripts/rename.py can change it everywhere."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from _support import PKG, ROOT, brand

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc")


def occurrences(root: Path, word: str):
    hits = []
    rx = re.compile(re.escape(word), re.I)
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for f in files:
            p = Path(dirpath) / f
            if p.relative_to(root).as_posix() == "SPEC.md":
                continue
            if rx.search(p.relative_to(root).as_posix()):
                hits.append(str(p.relative_to(root)))
                continue
            try:
                if rx.search(p.read_text(encoding="utf-8")):
                    hits.append(str(p.relative_to(root)))
            except (UnicodeDecodeError, OSError):
                pass
    return hits


class NameHygieneTest(unittest.TestCase):
    def test_name_not_hard_coded_in_code(self):
        """Inside the package, bin/ and scripts/, only brand.py may spell the name."""
        offenders = []
        for top in (ROOT / PKG, ROOT / "bin", ROOT / "scripts"):
            for p in top.rglob("*"):
                if p.is_file() and p.suffix in (".py", "") and p.name != "brand.py" and "__pycache__" not in p.parts:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    if re.search(r"\b" + re.escape(brand.NAME) + r"\b", text, re.I):
                        offenders.append(str(p.relative_to(ROOT)))
        self.assertEqual(offenders, [])


class RenameTest(unittest.TestCase):
    def test_rename_on_temp_copy(self):
        tmp = Path(tempfile.mkdtemp())
        copy = tmp / "repo"
        shutil.copytree(ROOT, copy, ignore=IGNORE, symlinks=True)
        try:
            new = "ownfault"
            p = subprocess.run([sys.executable, str(copy / "scripts" / "rename.py"), new, "--root", str(copy)],
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((copy / new / "brand.py").exists())
            self.assertFalse((copy / brand.NAME).exists())
            self.assertTrue(os.access(copy / "bin" / new, os.X_OK))
            self.assertIn(f'NAME = "{new}"', (copy / new / "brand.py").read_text())
            self.assertEqual(occurrences(copy, brand.NAME), [], "old name left behind")
            # the Claude Code plugin follows the rename
            plugin = json.loads((copy / ".claude-plugin" / "plugin.json").read_text())
            market = json.loads((copy / ".claude-plugin" / "marketplace.json").read_text())
            self.assertEqual(plugin["name"], new)
            self.assertEqual([p["name"] for p in market["plugins"]], [new])
            hook = json.loads((copy / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUseFailure"][0]["hooks"][0]
            self.assertEqual(hook["command"], "${CLAUDE_PLUGIN_ROOT}/bin/" + new)
            self.assertEqual(hook["args"], ["hook", "post-tool-failure"])
            skill = (copy / "skills" / "failing-tests" / "SKILL.md").read_text()
            self.assertIn(f"/bin/{new}\" check", skill)
            self.assertIn(f"[{new}] Per-test verdicts", skill)
            self.assertTrue((copy / "commands" / f"{new}.md").exists())
            self.assertFalse((copy / "commands" / f"{brand.NAME}.md").exists())
            out = subprocess.run([sys.executable, str(copy / "bin" / new), "version"], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertTrue(out.stdout.startswith(new + " "))
            out = subprocess.run([sys.executable, "-m", new, "version"], cwd=str(copy), capture_output=True, text=True)
            self.assertTrue(out.stdout.startswith(new + " "), out.stderr)
            # the renamed copy's own unit tests still pass (a fast subset)
            t = subprocess.run([sys.executable, "-m", "unittest", "test_normalize", "test_decide", "test_rename.NameHygieneTest"],
                               cwd=str(copy / "tests"), capture_output=True, text=True)
            self.assertEqual(t.returncode, 0, t.stderr[-2000:])
            again = subprocess.run([sys.executable, str(copy / "scripts" / "rename.py"), "Bad-Name", "--root", str(copy)],
                                   capture_output=True, text=True)
            self.assertNotEqual(again.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
