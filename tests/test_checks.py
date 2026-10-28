"""Check commands (type check / lint / format check) compared as error sets, and the UNKNOWN rule for
unreadable whole-command failures. Fake tools are tiny shell scripts: no node, no network."""
import json
import unittest
from pathlib import Path

from _support import commit, git_repo, mod, rmtree, run_cli, write

checks = mod("runners.checks")
runners = mod("runners")
base_mod = mod("runners.base")
model = mod("model")

OLD = [("src/app/layout.tsx", 12, "TS2304", "Cannot find name 'LayoutProps'."),
       ("src/app/page.tsx", 3, "TS2304", "Cannot find name 'PageProps'.")]
NEW5 = [("src/lib/a.ts", 4, "TS2322", "Type 'string' is not assignable to type 'number'."),
        ("src/lib/a.ts", 9, "TS2322", "Type 'number' is not assignable to type 'string'."),
        ("src/lib/b.ts", 1, "TS2307", "Cannot find module './nope' or its corresponding type declarations."),
        ("src/lib/c.ts", 7, "TS2339", "Property 'foo' does not exist on type '{}'."),
        ("src/lib/c.ts", 8, "TS7006", "Parameter 'x' implicitly has an 'any' type.")]

FAKE = "#!/bin/sh\n[ -s {out} ] || exit 0\ncat {out}\nexit {code}\n"


def tsc_lines(errs, shift=0):
    return "".join(f"{f}({line + shift},5): error {code}: {msg}\n" for f, line, code, msg in errs)


def check_repo(tool: str, out_name: str, base_out: str, head_out: str, code: int = 2) -> Path:
    repo = git_repo({tool: FAKE.format(out=out_name, code=code), out_name: base_out})
    (repo / tool).chmod(0o755)
    commit(repo, "x")  # executable bit
    write(repo, {out_name: head_out})
    return repo


def check(repo: Path, tool: str, *extra):
    p = run_cli(["check", "--json"] + list(extra) + ["--", f"./{tool}"], repo)
    return p, json.loads(p.stdout) if p.stdout.strip().startswith("{") else None


class ParserTest(unittest.TestCase):
    def test_tsc_both_forms_and_continuation(self):
        text = ("src/a.ts(3,1): error TS2322: Type 'A' is not assignable to type 'B'.\n"
                "  Property 'x' is missing in type 'A'.\n"
                "src/b.ts:4:2 - error TS2304: Cannot find name 'Foo'.\n\n4 Foo\n  ~~~\n"
                "error TS5058: The specified path does not exist: 'tsconfig.json'.\n"
                "Found 3 errors in 2 files.\n")
        got = checks.parse_tsc(text)
        self.assertEqual([g[:2] for g in got], [("src/a.ts", "TS2322"), ("src/b.ts", "TS2304"), ("", "TS5058")])
        self.assertIn("Property 'x' is missing", got[0][2])

    def test_eslint_stylish_and_json(self):
        stylish = ("\n/repo/src/a.ts\n  3:7  error    'x' is assigned a value but never used  no-unused-vars\n"
                   "  5:1  warning  Unexpected console statement  no-console\n\n"
                   "✖ 2 problems (1 error, 1 warning)\n")
        self.assertEqual(checks.parse_eslint(stylish),
                         [("/repo/src/a.ts", "no-unused-vars", "'x' is assigned a value but never used")])
        js = json.dumps([{"filePath": "/repo/src/a.ts", "messages": [
            {"ruleId": "eqeqeq", "severity": 2, "message": "Expected '==='."},
            {"ruleId": "no-console", "severity": 1, "message": "nope"}]}])
        self.assertEqual(checks.parse_eslint(js), [("/repo/src/a.ts", "eqeqeq", "Expected '==='.")])

    def test_prettier(self):
        text = ("Checking formatting...\n[warn] src/a.ts\n[warn] REVIEW.md\n"
                "[warn] Code style issues found in 2 files. Run Prettier with --write to fix.\n")
        self.assertEqual([f for f, _, _ in checks.parse_prettier(text)], ["src/a.ts", "REVIEW.md"])

    def test_next_build_type_error(self):
        text = "Failed to compile.\n\n./src/app/page.tsx:12:3\nType error: Cannot find name 'Foo'.\n"
        self.assertEqual(checks.parse_next_build(text), [("src/app/page.tsx", "type", "Cannot find name 'Foo'.")])

    def test_detection(self):
        for argv, tool in ((["tsc", "--noEmit"], "tsc"), (["npx", "tsc", "-p", "."], "tsc"),
                           (["node_modules/.bin/eslint", "."], "eslint"), (["prettier", "--check", "."], "prettier"),
                           (["next", "build"], "next-build"), (["prettier", "--write", "."], None),
                           (["tsc", "--watch"], None), (["eslint", "--fix", "."], None)):
            self.assertEqual(checks.check_tool(argv), tool, argv)
        self.assertEqual(runners.runner_for_argv(["tsc", "--noEmit"]), "tsc")

    def test_eslint_json_injected_only_without_format(self):
        r = checks.CheckRunner(["eslint", "."], Path("/x"), "eslint")
        self.assertEqual(r._argv()[-2:], ["--format", "json"])
        r2 = checks.CheckRunner(["eslint", "-f", "unix", "."], Path("/x"), "eslint")
        self.assertNotIn("json", r2._argv())


class CheckIntegrationTest(unittest.TestCase):
    def test_tsc_two_old_five_new(self):
        repo = check_repo("tsc", "errs.txt", tsc_lines(OLD), tsc_lines(OLD, shift=2) + tsc_lines(NEW5))
        try:
            p, rep = check(repo, "tsc")
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            verdicts = [v["verdict"] for v in rep["results"]]
            self.assertEqual(verdicts.count("new"), 5, verdicts)
            self.assertEqual(verdicts.count("pre_existing"), 2, verdicts)
            new_ids = [v["id"] for v in rep["results"] if v["verdict"] == "new"]
            self.assertIn("src/lib/a.ts: TS2322 Type 'string' is not assignable to type 'number'.", new_ids)
            text = run_cli(["check", "--", "./tsc"], repo).stdout
            self.assertIn("5 NEW", text)
            self.assertIn("type error", text)
            self.assertNotIn("No failure was caused", text)
        finally:
            rmtree(repo)

    def test_tsc_base_lacks_generated_types(self):
        # head: 5 new errors only (generated types exist here); base: 2 different errors (they don't)
        repo = check_repo("tsc", "errs.txt", tsc_lines(OLD), tsc_lines(NEW5))
        try:
            p, rep = check(repo, "tsc")
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertEqual([v["verdict"] for v in rep["results"]].count("new"), 5)
            self.assertTrue(any("base also failed" in n for n in rep["notes"]), rep["notes"])
        finally:
            rmtree(repo)

    def test_generated_types_copied_into_base(self):
        # a fake type check that fails while the git-ignored generated types are missing
        fake = ("#!/bin/sh\nrc=0\n[ -f .next/types/routes.d.ts ] || { printf '%s' \"$MISSING\"; rc=2; }\n"
                "[ -s errs.txt ] && { cat errs.txt; rc=2; }\nexit $rc\n")
        fake = fake.replace("$MISSING", tsc_lines(OLD).replace("\n", "\\n").replace("'", ""))
        repo = git_repo({"tsc": fake, "errs.txt": "", ".gitignore": ".next/\n"})
        try:
            (repo / "tsc").chmod(0o755)
            commit(repo, "x")
            write(repo, {"errs.txt": tsc_lines(NEW5), ".next/types/routes.d.ts": "export {}\n"})
            p, rep = check(repo, "tsc")
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertEqual({v["verdict"] for v in rep["results"]}, {"new"})
            self.assertTrue(any(".next/types" in n for n in rep["notes"]), rep["notes"])
        finally:
            rmtree(repo)

    def test_tsc_only_old_errors_pre_existing(self):
        repo = check_repo("tsc", "errs.txt", tsc_lines(OLD), tsc_lines(OLD, shift=5))
        try:
            p, rep = check(repo, "tsc")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual({v["verdict"] for v in rep["results"]}, {"pre_existing"})
            self.assertEqual(rep["test_runs"], 2)  # one run per side: checks are deterministic
        finally:
            rmtree(repo)

    def test_eslint_style(self):
        old = "\n/R/src/a.ts\n  3:7  error  'x' is assigned a value but never used  no-unused-vars\n"
        new = old + "\n/R/src/b.ts\n  1:1  error  Expected '==='  eqeqeq\n"
        repo = check_repo("eslint", "lint.txt", old, new, code=1)
        try:
            p = run_cli(["check", "--json", "--", "./eslint", "-f", "stylish"], repo)
            rep = json.loads(p.stdout)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertEqual(sorted(v["verdict"] for v in rep["results"]), ["new", "pre_existing"])
        finally:
            rmtree(repo)

    def test_prettier_style(self):
        old = "Checking formatting...\n[warn] REVIEW.md\n[warn] Code style issues found in the above file.\n"
        new = "Checking formatting...\n[warn] REVIEW.md\n[warn] src/bad.ts\n[warn] Code style issues found.\n"
        repo = check_repo("prettier", "fmt.txt", old, new, code=1)
        try:
            p = run_cli(["check", "--json", "--", "./prettier", "--check", "."], repo)
            rep = json.loads(p.stdout)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            new_ids = [v["id"] for v in rep["results"] if v["verdict"] == "new"]
            self.assertEqual(new_ids, ["src/bad.ts: not formatted"])
            ok = check_repo("prettier", "fmt.txt", old, "")
            try:
                out = run_cli(["check", "--", "./prettier", "--check", "."], ok).stdout
                self.assertIn("check passed", out)
                self.assertNotIn("tests passed", out)
            finally:
                rmtree(ok)
        finally:
            rmtree(repo)

    def test_unparseable_fails_both_sides_unknown(self):
        repo = check_repo("tool.sh", "out.txt", "something broke\n", "something else broke\n", code=1)
        try:
            p, rep = check(repo, "tool.sh")
            self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
            v = rep["results"][0]
            self.assertEqual(v["verdict"], "unknown")
            self.assertEqual(v["id"], "./tool.sh")
            self.assertIn("something else broke", v["head"]["error_raw"])
            self.assertIn("something broke", v["base"]["error_raw"])
            text = run_cli(["check", "--", "./tool.sh"], repo).stdout
            self.assertNotIn("PRE-EXISTING", text)
            self.assertNotIn("No failure was caused", text)
            self.assertIn("can't tell", text.lower())
        finally:
            rmtree(repo)

    def test_tsc_unreadable_base_is_unknown(self):
        repo = check_repo("tsc", "errs.txt", "Segmentation fault\n", tsc_lines(NEW5[:1]))
        try:
            p, rep = check(repo, "tsc")
            self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
            self.assertEqual([v["verdict"] for v in rep["results"]], ["unknown"])
        finally:
            rmtree(repo)


if __name__ == "__main__":
    unittest.main()
