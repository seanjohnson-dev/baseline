"""Load/setup failures (no such npm script, command not found, Node too old) are ENVIRONMENT or a usage error,
never PRE-EXISTING."""
import json
import unittest

from _support import commit, git_repo, mod, rmtree, run_cli, write

envclass = mod("envclass")
runners = mod("runners")


def classify(key, raw=""):
    return envclass.classify(key, raw, envclass.EnvContext("/nonexistent"))


class EnvPatternTest(unittest.TestCase):
    def test_patterns(self):
        cases = {
            'TypeError [ERR_UNKNOWN_FILE_EXTENSION]: Unknown file extension ".ts" for /r/src/lib/x.ts':
                "unsupported_runtime",
            "error: this tool requires Node.js >= 22": "unsupported_runtime",
            'npm error Missing script: "test"': "no_such_script",
            "sh: 1: tsc: not found": "missing_executable",
            "/bin/sh: eslint: not found": "missing_executable",
            "bash: prettier: command not found": "missing_executable",
            "Error: spawn tsc ENOENT": "missing_executable",
            "Error: Cannot find module 'typescript'": "missing_module",
        }
        for key, cls in cases.items():
            got = classify(key)
            self.assertIsNotNone(got, key)
            self.assertEqual(got["class"], cls, key)

    def test_node_version_named(self):
        got = classify('TypeError [ERR_UNKNOWN_FILE_EXTENSION]: Unknown file extension ".ts" for /r/a.ts')
        self.assertIn("Node ", got["detail"])
        self.assertIn("22.6", got["detail"])

    def test_assertions_never_environment(self):
        for key in ("AssertionError: expected 'Missing script: x' to equal 'y'",
                    "expected 'sh: 1: tsc: not found' to be empty"):
            self.assertIsNone(classify(key), key)

    def test_missing_npm_script(self):
        repo = git_repo({"package.json": json.dumps({"scripts": {"lint": "eslint ."}})})
        try:
            self.assertEqual(runners.missing_npm_script(["npm", "test"], repo), "test")
            self.assertEqual(runners.missing_npm_script(["npm", "run", "typecheck"], repo), "typecheck")
            self.assertIsNone(runners.missing_npm_script(["npm", "run", "lint"], repo))
            p = run_cli(["check", "--", "npm", "test"], repo)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("no such npm script: 'test'", p.stderr)
            self.assertNotIn("PRE-EXISTING", p.stdout)
        finally:
            rmtree(repo)


class EnvIntegrationTest(unittest.TestCase):
    def test_command_not_found_on_both_sides_is_environment(self):
        script = "#!/bin/sh\necho 'sh: 1: nosuchtool-xyz: not found' >&2\nexit 127\n"
        repo = git_repo({"t.sh": script})
        (repo / "t.sh").chmod(0o755)
        commit(repo, "x")
        write(repo, {"t.sh": script + "# edited\n"})
        try:
            p = run_cli(["check", "--json", "--", "./t.sh"], repo)
            rep = json.loads(p.stdout)
            self.assertEqual(rep["results"][0]["verdict"], "environment", p.stdout)
            self.assertEqual(p.returncode, 0)
        finally:
            rmtree(repo)

    def test_ts_import_on_old_node_both_sides_environment(self):
        script = ("#!/bin/sh\necho 'TypeError [ERR_UNKNOWN_FILE_EXTENSION]: Unknown file extension \".ts\" "
                  "for /r/src/lib/x.ts' >&2\nexit 1\n")
        repo = git_repo({"t.sh": script})
        (repo / "t.sh").chmod(0o755)
        commit(repo, "x")
        write(repo, {"t.sh": script + "# edited\n"})
        try:
            p = run_cli(["check", "--", "./t.sh"], repo)
            self.assertIn("ENVIRONMENT", p.stdout)
            self.assertIn("22.6", p.stdout)
            self.assertNotIn("PRE-EXISTING", p.stdout)
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
