"""The hook speaks for node --test and for check commands (type check, lint, format check, build)."""
import json
import tempfile
import unittest
from pathlib import Path

from _support import git_repo, mod, rmtree
from test_hook import payload

hook = mod("hook")

SCRIPTS = {"typecheck": "tsc --noEmit", "lint": "eslint .", "format:check": "prettier --check .",
           "build": "next build", "test": "node --test", "check": "npm run lint && npm run typecheck",
           "dev": "next dev"}


class CheckSegmentTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        Path(self.d, "package.json").write_text(json.dumps({"scripts": SCRIPTS}))

    def tearDown(self):
        rmtree(self.d)

    def test_kinds(self):
        cases = {
            "node --test": "node-test", "node --test --experimental-strip-types tests/": "node-test",
            "npm test": "npm-test", "npm run test": "npm-test",
            "npm run typecheck": "check", "pnpm run type-check": "check", "yarn lint": "check",
            "npm run format:check": "check", "npm run build": "check", "pnpm run check": "check",
            "npx tsc --noEmit": "check", "tsc -p .": "check", "eslint src": "check",
            "prettier --check .": "check", "npx next build": "check",
        }
        for cmd, kind in cases.items():
            seg = hook.find_test_segment(cmd, self.d)
            self.assertIsNotNone(seg, cmd)
            self.assertEqual(seg.kind, kind, cmd)
        for cmd in ("npm run dev", "prettier --write .", "tsc --watch", "node server.js"):
            self.assertIsNone(hook.find_test_segment(cmd, self.d), cmd)


class CheckHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = git_repo({"package.json": json.dumps({"scripts": SCRIPTS})})
        self.cache = tempfile.mkdtemp()

    def tearDown(self):
        rmtree(self.repo)
        rmtree(self.cache)

    def test_failing_check_gets_hint_with_exact_command(self):
        for cmd in ("npm run typecheck", "npm run lint", "npm run format:check", "npm run build", "npx tsc",
                    "tsc", "eslint .", "prettier --check ."):
            ctx = hook.handle(payload(cmd, self.repo), session_dir=Path(self.cache))
            self.assertIsNotNone(ctx, cmd)
            self.assertIn(f"check -- {cmd}`", ctx)

    def test_passing_check_is_silent(self):
        ok = payload("npm run typecheck", self.repo, hook_event_name="PostToolUse",
                     tool_response={"stdout": "", "stderr": ""})
        ok.pop("error")
        self.assertIsNone(hook.handle(ok, session_dir=Path(self.cache)))

    def test_piped_tsc_failure_gets_hint(self):
        p = payload("npx tsc --noEmit | tail -3", self.repo, hook_event_name="PostToolUse",
                    tool_response={"stdout": "src/a.ts(1,1): error TS2322: x\nFound 1 error in src/a.ts:1\n"})
        p.pop("error")
        ctx = hook.handle(p, session_dir=Path(self.cache))
        self.assertIsNotNone(ctx)
        self.assertIn("check -- npx tsc --noEmit`", ctx)


if __name__ == "__main__":
    unittest.main()
