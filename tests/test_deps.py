"""A change that edits dependency manifests/lockfiles: base runs with the new dependencies (linked
node_modules, the active Python environment), so nothing that fails on base too may be called
pre-existing, environmental or flaky on base. NEW stays NEW. Go and Cargo build each side from its own
go.mod / Cargo.lock and are not affected."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from _support import commit, git_repo, have_pytest, mod, rmtree, run_cli, sh, write
from test_chain import head as chain_head
from test_chain import repo_with, WEB_OK
from test_mixed import cra_repo, set_head

m = mod("model")
d = mod("decide")
deps = mod("deps")
report = mod("report")
hook = mod("hook")


# ------------------------------------------------------------------------------------------- detector
class DetectorTest(unittest.TestCase):
    def test_every_family_at_any_depth(self):
        names = ["package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
                 "pnpm-workspace.yaml", "bun.lockb", "bun.lock",
                 "requirements.txt", "requirements-dev.txt", "requirements_test.txt", "dev-requirements.txt",
                 "requirements.in", "constraints.txt", "constraints-py39.txt",
                 "pyproject.toml", "poetry.lock", "Pipfile", "Pipfile.lock", "uv.lock", "pdm.lock", "setup.py",
                 "setup.cfg", "Gemfile", "Gemfile.lock", "gems.rb", "gems.locked", "shop.gemspec"]
        for name in names:
            for prefix in ("", "packages/web/", "services/api/backend/"):
                with self.subTest(path=prefix + name):
                    self.assertTrue(deps.is_manifest(prefix + name))
        for path in ("requirements/base.txt", "requirements/dev.in", "backend/requirements/prod.txt"):
            with self.subTest(path=path):
                self.assertTrue(deps.is_manifest(path))

    def test_not_dependency_files(self):
        for path in ("requirements.md", "docs/requirements.rst", "src/package.json.bak", "package.json5",
                     "src/package.ts", "tests/fixtures/yarn.lock.txt.orig", "Gemfile.bak", "lib/setup_py.py",
                     "requirements/README.md", "notes/base.txt", "pyproject.toml.orig",
                     # each worktree builds from its own copy (see the module docstring)
                     "go.mod", "go.sum", "go.work", "svc/go.mod", "Cargo.toml", "Cargo.lock", "crates/x/Cargo.toml",
                     "README.md", "src/app.py"):
            with self.subTest(path=path):
                self.assertFalse(deps.is_manifest(path))

    def test_package_json_only_safe_keys(self):
        old = json.dumps({"name": "a", "version": "1.0.0", "scripts": {"test": "jest"},
                          "dependencies": {"left-pad": "^1.0.0"}})

        def edit(**kw):
            o = json.loads(old)
            o.update(kw)
            return json.dumps(o, indent=2)
        self.assertFalse(deps.package_json_deps_differ(old, edit(scripts={"test": "jest", "lint": "eslint ."})))
        self.assertFalse(deps.package_json_deps_differ(old, edit(description="x", keywords=["y"], license="MIT")))
        self.assertFalse(deps.package_json_deps_differ(old, old.replace(", ", ",\n  ")))  # formatting only
        for changed in (edit(dependencies={"left-pad": "^2.0.0"}), edit(devDependencies={"jest": "29"}),
                        edit(overrides={"x": "1"}), edit(resolutions={"x": "1"}), edit(workspaces=["packages/*"]),
                        edit(packageManager="pnpm@9.0.0"), edit(version="2.0.0"), edit(name="b"),
                        edit(pnpm={"overrides": {"x": "1"}})):
            with self.subTest(changed=changed):
                self.assertTrue(deps.package_json_deps_differ(old, changed))
        self.assertTrue(deps.package_json_deps_differ(None, old), "added")
        self.assertTrue(deps.package_json_deps_differ(old, None), "deleted")
        self.assertTrue(deps.package_json_deps_differ(old, "{not json"), "unreadable")
        self.assertTrue(deps.package_json_deps_differ(old, "[]"))

    def test_changed_manifests_against_git(self):
        pkg = {"name": "a", "scripts": {"test": "jest"}, "dependencies": {"left-pad": "1"}}
        repo = git_repo({"package.json": json.dumps(pkg), "web/package.json": json.dumps(pkg),
                         "requirements.txt": "a==1\n", "README.md": "x\n", "go.mod": "module m\n"})
        try:
            base = sh(["git", "rev-parse", "HEAD"], repo).stdout.strip()
            write(repo, {"package.json": json.dumps(dict(pkg, scripts={"test": "jest", "t2": "x"})),  # scripts only
                         "web/package.json": json.dumps(dict(pkg, dependencies={"left-pad": "2"})),
                         "README.md": "y\n", "go.mod": "module m\n\ngo 1.21\n"})
            gu = mod("gitutil")
            got = deps.changed_manifests(repo, base, gu.changed_files(repo, base))
            self.assertEqual(got, ["web/package.json"])
            # staged, unstaged, untracked and deleted manifests all count
            write(repo, {"requirements.txt": "a==2\n"})
            sh(["git", "add", "requirements.txt"], repo)
            write(repo, {"api/requirements-dev.txt": "pytest\n", "Gemfile.lock": "x\n"})
            os.unlink(repo / "web" / "package.json")
            got = deps.changed_manifests(repo, base, gu.changed_files(repo, base))
            self.assertEqual(got, ["Gemfile.lock", "api/requirements-dev.txt", "requirements.txt", "web/package.json"])
        finally:
            rmtree(repo)

    def test_describe(self):
        self.assertEqual(deps.describe(["a", "b"]), "a, b")
        self.assertEqual(deps.describe(list("abcdef")), "a, b, c, d (+2 more)")


# ------------------------------------------------------------------------------------------- decision
def F(key="AssertionError: assert 1 == 2", env=None):
    return m.Obs(m.FAIL, key, key, key, env)


def P():
    return m.Obs(m.PASS)


ENV_MOD = {"class": "missing_module", "detail": "Python module 'gone' is not installed"}
DEPS = ["package.json", "package-lock.json"]


def verdict(head, base, kind=m.TEST, dep=DEPS, **kw):
    u = m.Unit("t" if kind != m.COMMAND else "npm test", kind, "tests/test_a.py", "t", {})
    return d.deps_rule(d.decide(d.UnitState(u, list(head), list(base), **kw)), dep)


class DepsRuleTest(unittest.TestCase):
    CASES = [
        # name, head, base, kind, kwargs, verdict without deps, verdict with deps
        ("same error on base", [F()] * 5, [F()] * 5, m.TEST, {}, m.PRE, m.UNKNOWN),
        ("error changed", [F("assert 6 == 7")] * 5, [F("assert 5 == 7")] * 5, m.TEST, {}, m.PRE_CHANGED, m.UNKNOWN),
        ("environment both sides (a removed package)", [F("No module named 'gone'", ENV_MOD)] * 5,
         [F("No module named 'gone'", ENV_MOD)] * 5, m.TEST, {}, m.ENVIRONMENT, m.UNKNOWN),
        ("flaky on base", [F()] * 5, [P(), F(), P()], m.TEST, {}, m.FLAKY, m.UNKNOWN),
        ("load error same on both sides", [F("ImportError: x")] * 5, [F("ImportError: x")] * 5, m.LOAD_ERROR, {},
         m.PRE, m.UNKNOWN),
        ("check error on both sides", [F("src/a.ts: TS2322 x")], [F("src/a.ts: TS2322 x")], m.CHECK, {}, m.PRE,
         m.UNKNOWN),
        ("passes on base: NEW stays NEW", [F()] * 5, [P()] * 5, m.TEST, {}, m.NEW, m.NEW),
        ("new test: NEW stays NEW", [F()] * 5, [m.Obs(m.ABSENT)], m.TEST, {"exists_on_base": False}, m.NEW, m.NEW),
        ("check error only on head: NEW", [F("src/a.ts: TS2322 x")], [P()], m.CHECK, {}, m.NEW, m.NEW),
        ("flaky on head stays FLAKY", [F(), P()], [F()] * 5, m.TEST, {}, m.FLAKY, m.FLAKY),
        ("unknown stays unknown", [F()] * 5, [], m.TEST, {"base_problem": "base run failed"}, m.UNKNOWN, m.UNKNOWN),
        ("whole command failing on both sides", [F("FAIL x")] * 2, [F("FAIL x")], m.COMMAND, {}, m.UNKNOWN,
         m.UNKNOWN),
        ("whole command passing on base", [F("FAIL x")] * 2, [P()] * 2, m.COMMAND, {}, m.NEW, m.NEW),
    ]

    def test_table(self):
        for name, head, base, kind, kw, without, with_deps in self.CASES:
            with self.subTest(name):
                self.assertEqual(verdict(head, base, kind, [], **kw)["verdict"], without)
                v = verdict(head, base, kind, DEPS, **kw)
                self.assertEqual(v["verdict"], with_deps)
                if without != with_deps:
                    self.assertTrue(v["reason"].startswith("Dependencies changed (package.json, package-lock.json), "
                                                           "so this can't be called "), v["reason"])
                    self.assertIn("base ran with the new dependencies", v["reason"])
                elif without != m.FLAKY:
                    self.assertNotIn("Dependencies", v["reason"])

    def test_reasons_keep_the_evidence(self):
        v = verdict([F()] * 5, [F()] * 4)
        self.assertIn("fails the same way on base (4/4) and head (5/5)", v["reason"])
        self.assertNotIn("already broken", v["reason"])
        v = verdict([F("No module named 'gone'", ENV_MOD)] * 3, [F("No module named 'gone'", ENV_MOD)] * 3)
        self.assertIn("can't be called environmental", v["reason"])
        self.assertIn("Python module 'gone' is not installed", v["reason"])
        v = verdict([F(), P()], [F()] * 5)
        self.assertIn("Dependencies changed too (package.json, package-lock.json)", v["reason"])

    def test_surfaced_flaky_is_left_alone(self):
        st = d.UnitState(m.Unit("t", m.TEST, "g", ""), head=[P()], base=[F(), P()])
        v = d.flip_result(st)
        self.assertEqual(d.deps_rule(dict(v), DEPS), v)


# ------------------------------------------------------------------------------------------- wording
def rep_with(results, deps_changed, kind="tests"):
    counts = {k: sum(1 for v in results if v["verdict"] == k) for k in
              (m.NEW, m.PRE, m.PRE_CHANGED, m.FLAKY, m.ENVIRONMENT, m.UNKNOWN)}
    return {"summary": dict(counts, tests_total=3, tests_passed=1, tests_failed=2, tests_skipped=0, load_errors=0),
            "base": {"short": "abc1234", "how": "merge-base of HEAD and main"},
            "head": {"short": "def5678", "dirty": False}, "deps_changed": deps_changed,
            "runner": {"name": "pytest", "command": ["pytest"], "per_test": kind == "tests"},
            "results": results, "test_runs": 20, "duration_s": 1.0}


def res(verdict_, reason="r", **kw):
    side = {"runs": 5, "failed": 5, "passed": 0, "message": "AssertionError: x", "error": "AssertionError: x",
            "error_raw": ""}
    v = {"id": f"tests/test_a.py::test_{verdict_}", "kind": "test", "verdict": verdict_, "reason": reason,
         "head": dict(side), "base": dict(side), "env_class": None, "blocked_tests": [], "flaky_side": "head"}
    v.update(kw)
    return v


class WordingTest(unittest.TestCase):
    def test_text_report_and_agent_line(self):
        unknown = verdict([F()] * 5, [F()] * 5, dep=["requirements.txt"])
        text = report.render(rep_with([dict(res("unknown"), reason=unknown["reason"])], ["requirements.txt"]))
        self.assertIn("Dependencies changed: requirements.txt — base ran with the new ones", text)
        self.assertIn("why: Dependencies changed (requirements.txt), so this can't be called pre-existing", text)
        self.assertNotIn("No failure was caused", text)
        self.assertIn("This change edits dependencies (requirements.txt)", report.agent_line(
            rep_with([res("unknown")], ["requirements.txt"])))
        # FLAKY on head alone stays FLAKY, but with new dependency versions it is not "not caused by this change"
        line = report.agent_line(rep_with([res("flaky")], ["yarn.lock"]))
        self.assertNotIn("No failure was caused", line)
        self.assertIn("yarn.lock", line)
        self.assertIn("No failure was caused", report.agent_line(rep_with([res("flaky")], [])))
        self.assertNotIn("Dependencies changed", report.render(rep_with([res("new")], [])))

    def test_hook_context(self):
        rep = rep_with([res("unknown", reason="Dependencies changed (package.json), so this can't be called "
                                              "pre-existing: ...")], ["package.json"])
        text = hook.format_context(rep, "npx jest", "/p/bin/x")
        self.assertIn("Dependencies changed in this diff (package.json): base ran with the new dependencies", text)
        self.assertNotIn("None of these failures was caused", text)
        self.assertIn("so this can't be called pre-existing", text)
        text = hook.format_context(rep_with([res("flaky")], ["package.json"]), "npx jest", "/p/bin/x")
        self.assertNotIn("None of these failures was caused", text)
        self.assertNotIn("not caused by this change", text)
        self.assertIn("the new dependency versions may be why", text)
        # unchanged without a dependency change
        text = hook.format_context(rep_with([res("flaky")], []), "npx jest", "/p/bin/x")
        self.assertIn("None of these failures was caused", text)
        self.assertNotIn("Dependencies changed", text)


# ------------------------------------------------------------------------------------------- end to end
GREETLIB = {"1.0": 'def greet(name):\n    return f"Hello, {name}!"\n',
            "2.0": 'def greet(name):\n    return f"Hi {name}"\n'}
PY_BASE = {"requirements.txt": "greetlib==1.0\n", ".gitignore": "__pycache__/\n",
           "app/__init__.py": "import greetlib\n\n\ndef welcome(name):\n    return greetlib.greet(name)\n\n\n"
                              "def add(a, b):\n    return a + b\n",
           "tests/test_app.py": "from app import add, welcome\n\n\ndef test_welcome():\n"
                                "    assert welcome('Ann') == 'Hello, Ann!'\n\n\ndef test_add():\n"
                                "    assert add(2, 2) == 4\n",
           "tests/test_old.py": "import oldlib\n\n\ndef test_old():\n    assert oldlib.ok()\n"}


@unittest.skipUnless(have_pytest(), "pytest not installed")
class PytestDepsTest(unittest.TestCase):
    """The 'installed packages' are a directory outside the repo on PYTHONPATH, shared by both sides like a
    venv: the change bumps greetlib in requirements.txt and greetlib 2.0 is 'installed'; oldlib is gone."""

    def setUp(self):
        self.site = Path(tempfile.mkdtemp(prefix="t-site-"))
        self.install("2.0")
        self.repo = git_repo(PY_BASE)

    def tearDown(self):
        rmtree(self.repo)
        rmtree(self.site)

    def install(self, version):
        write(self.site, {"greetlib/__init__.py": GREETLIB[version]})

    def check(self, *extra):
        env = dict(os.environ, PYTHONPATH=str(self.site))
        p = run_cli(["check", "--json", "--runs", "2", *extra, "--", sys.executable, "-m", "pytest", "-q"], self.repo,
                    env=env)
        try:
            return p, json.loads(p.stdout)
        except ValueError:
            raise AssertionError(p.stdout + p.stderr)

    def verdicts(self, rep):
        return {v["id"]: v["verdict"] for v in rep["results"]}

    def test_requirements_bump_on_a_branch(self):
        sh(["git", "checkout", "-q", "-b", "bump"], self.repo)
        write(self.repo, {"requirements.txt": "greetlib==2.0\n"})
        commit(self.repo, "bump greetlib")
        p, rep = self.check("--no-cache")
        self.assertEqual(rep["deps_changed"], ["requirements.txt"])
        self.assertEqual(self.verdicts(rep), {"tests/test_app.py::test_welcome": "unknown",
                                              "tests/test_old.py": "unknown"}, json.dumps(rep["results"])[:2000])
        self.assertEqual(p.returncode, 3)
        for v in rep["results"]:
            self.assertTrue(v["reason"].startswith("Dependencies changed (requirements.txt), so this can't be called"),
                            v["reason"])
        old = next(v for v in rep["results"] if v["id"] == "tests/test_old.py")
        self.assertIn("can't be called environmental", old["reason"])
        # NEW stays NEW next to them
        write(self.repo, {"app/__init__.py": PY_BASE["app/__init__.py"].replace("a + b", "a - b")})
        p, rep = self.check("--no-cache")
        self.assertEqual(self.verdicts(rep)["tests/test_app.py::test_add"], "new")
        self.assertEqual(p.returncode, 1)
        p = run_cli(["check", "--no-cache", "--runs", "2", "--", sys.executable, "-m", "pytest", "-q"], self.repo,
                    env=dict(os.environ, PYTHONPATH=str(self.site)))
        self.assertIn("Dependencies changed: requirements.txt — base ran with the new ones", p.stdout)
        self.assertNotIn("No failure was caused", p.stdout)

    def test_no_dependency_change_is_unchanged(self):
        write(self.repo, {"README.md": "x\n"})
        p, rep = self.check("--no-cache")
        self.assertEqual(rep["deps_changed"], [])
        self.assertEqual(self.verdicts(rep), {"tests/test_app.py::test_welcome": "pre_existing",
                                              "tests/test_old.py": "environment"})
        self.assertEqual(p.returncode, 0)

    def test_dirty_tree_staged_unstaged_and_untracked(self):
        for name, change in (("unstaged", {"requirements.txt": "greetlib==2.0\n"}),
                             ("staged", {"requirements.txt": "greetlib==2.0\n"}),
                             ("untracked new manifest", {"api/requirements-dev.txt": "greetlib==2.0\n"})):
            with self.subTest(name):
                sh(["git", "reset", "-q", "--hard"], self.repo)
                sh(["git", "clean", "-qfd"], self.repo)
                write(self.repo, change)
                if name == "staged":
                    sh(["git", "add", "-A"], self.repo)
                p, rep = self.check("--no-cache")
                self.assertEqual(rep["deps_changed"], sorted(change))
                self.assertTrue(rep["head"]["dirty"])
                self.assertEqual(set(self.verdicts(rep).values()), {"unknown"})
                self.assertEqual(p.returncode, 3)

    def test_cached_base_result_still_gets_the_rule(self):
        write(self.repo, {"requirements.txt": "greetlib==2.0\n"})
        p1, r1 = self.check()
        p2, r2 = self.check()
        self.assertFalse(r1["cache"]["hit"])
        self.assertTrue(r2["cache"]["hit"])
        self.assertTrue(any(v["base"]["cached_runs"] for v in r2["results"]))
        self.assertEqual(self.verdicts(r1), self.verdicts(r2))
        self.assertEqual(set(self.verdicts(r2).values()), {"unknown"})
        self.assertEqual((p1.returncode, p2.returncode), (3, 3))
        self.assertEqual(r2["deps_changed"], ["requirements.txt"])
        # the same cached base result without the dependency change: the rule follows the diff, not the cache
        write(self.repo, {"requirements.txt": PY_BASE["requirements.txt"]})
        p3, r3 = self.check()
        self.assertTrue(r3["cache"]["hit"])
        self.assertEqual(r3["deps_changed"], [])
        self.assertEqual(self.verdicts(r3)["tests/test_app.py::test_welcome"], "pre_existing")


class JestDepsTest(unittest.TestCase):
    """package.json + package-lock.json change, with the stand-in jest of test_mixed (no network)."""

    BASE = {"src/a.test.js": {"adds": "pass", "old bug": "fail:Error: expect(received).toBe(expected) 1 2"},
            "src/b.test.js": {"db": "fail:Error: connect ECONNREFUSED 127.0.0.1:5432"}}

    def setUp(self):
        self.repo = cra_repo(self.BASE, {"package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {}})})

    def tearDown(self):
        rmtree(self.repo)

    def check(self):
        e = dict(os.environ)
        e.pop("CI", None)
        p = run_cli(["check", "--json", "--no-cache", "--base", "main~1", "--", "npm", "test"], self.repo, env=e)
        try:
            return p, json.loads(p.stdout)
        except ValueError:
            raise AssertionError(p.stdout + p.stderr)

    def head(self, files):
        spec = json.loads(json.dumps(self.BASE))
        spec["src/a.test.js"]["adds"] = "fail:Error: expect(received).toBe(expected) 3 4"
        set_head(self.repo, spec, files)

    def test_package_json_and_lockfile(self):
        pkg = json.loads((self.repo / "package.json").read_text())
        pkg["dependencies"]["left-pad"] = "^2.0.0"
        self.head({"package.json": json.dumps(pkg), "package-lock.json": json.dumps({"lockfileVersion": 3,
                                                                                     "packages": {"x": 1}})})
        p, rep = self.check()
        self.assertEqual(rep["deps_changed"], ["package-lock.json", "package.json"])
        self.assertEqual({v["id"]: v["verdict"] for v in rep["results"]},
                         {"src/a.test.js > adds": "new", "src/a.test.js > old bug": "unknown",
                          "src/b.test.js > db": "unknown"})
        self.assertEqual(p.returncode, 1)

    def test_scripts_only_edit_does_not_count(self):
        pkg = json.loads((self.repo / "package.json").read_text())
        pkg["scripts"]["lint"] = "eslint ."
        self.head({"package.json": json.dumps(pkg, indent=2)})
        p, rep = self.check()
        self.assertEqual(rep["deps_changed"], [])
        self.assertEqual({v["id"]: v["verdict"] for v in rep["results"]},
                         {"src/a.test.js > adds": "new", "src/a.test.js > old bug": "pre_existing",
                          "src/b.test.js > db": "environment"})


class ChainDepsTest(unittest.TestCase):
    def test_each_part_gets_the_rule(self):
        bad = {"api/e.test.js": {"escapes": "fail:Error: expect(received).toBe(expected) old", "quotes": "pass"}}
        self.repo = repo_with(WEB_OK, bad)
        try:
            chain_head(self.repo, files={"yarn.lock": "# new\n"})
            e = dict(os.environ)
            e.pop("CI", None)
            p = run_cli(["check", "--json", "--base", "main~1", "--", "npm", "run", "test:all"], self.repo, env=e)
            rep = json.loads(p.stdout)
            self.assertEqual(rep["deps_changed"], ["yarn.lock"])
            self.assertEqual({(v.get("part"), v["id"]): v["verdict"] for v in rep["results"]},
                             {(2, "api/e.test.js > escapes"): "unknown"})  # was pre_existing (test_chain c)
            self.assertEqual(p.returncode, 3)
            p = run_cli(["check", "--base", "main~1", "--", "npm", "run", "test:all"], self.repo, env=e)
            self.assertIn("Dependencies changed: yarn.lock", p.stdout)
            self.assertNotIn("No failure was caused", p.stdout)
        finally:
            rmtree(self.repo)


def go_proxy(root: Path) -> None:
    """A file:// GOPROXY with example.com/lib v1.0.0 (Value() = 1) and v1.1.0 (Value() = 2)."""
    mod_path = "example.com/lib"
    d_ = root / mod_path / "@v"
    d_.mkdir(parents=True)
    for ver, n in (("v1.0.0", 1), ("v1.1.0", 2)):
        gomod = f"module {mod_path}\n\ngo 1.21\n"
        (d_ / f"{ver}.mod").write_text(gomod)
        (d_ / f"{ver}.info").write_text(json.dumps({"Version": ver, "Time": "2026-01-01T00:00:00Z"}))
        with zipfile.ZipFile(d_ / f"{ver}.zip", "w") as z:
            z.writestr(f"{mod_path}@{ver}/go.mod", gomod)
            z.writestr(f"{mod_path}@{ver}/lib.go", f"package lib\n\nfunc Value() int {{ return {n} }}\n")
    (d_ / "list").write_text("v1.0.0\nv1.1.0\n")


GO_TEST = ('package v\n\nimport (\n\t"testing"\n\n\t"example.com/lib"\n)\n\nfunc TestValue(t *testing.T) {\n'
           '\tif got := lib.Value(); got != 1 {\n\t\tt.Fatalf("lib.Value() = %d, want 1", got)\n\t}\n}\n')


@unittest.skipUnless(shutil.which("go"), "go not installed")
class GoModTest(unittest.TestCase):
    """go.mod/go.sum are read per worktree: base builds with v1.0.0 although head bumped to v1.1.0, so the
    failure is NEW and go.mod is not a 'dependency change' for the rule. Offline: a file:// module proxy."""

    def test_go_mod_bump_is_new(self):
        tmp = Path(tempfile.mkdtemp(prefix="t-go-"))
        try:
            go_proxy(tmp / "proxy")
            env = dict(os.environ, GOPROXY=f"file://{tmp / 'proxy'}", GOSUMDB="off", GOMODCACHE=str(tmp / "modcache"),
                       GOTOOLCHAIN="local", GOFLAGS="-modcacherw")
            env.pop("GOPATH", None)
            repo = git_repo({"go.mod": "module example.com/m\n\ngo 1.21\n\nrequire example.com/lib v1.0.0\n",
                             "v/v_test.go": GO_TEST})
            shutil.move(str(repo), str(tmp / "repo"))
            repo = tmp / "repo"
            sh(["go", "mod", "tidy"], repo, env=env)
            commit(repo, "go.sum")
            sh(["git", "checkout", "-q", "-b", "bump"], repo)
            sh(["go", "get", "example.com/lib@v1.1.0"], repo, env=env)
            commit(repo, "bump lib")
            self.assertIn("v1.1.0", (repo / "go.mod").read_text())
            p = run_cli(["check", "--json", "--no-cache", "--runs", "2", "--", "go", "test", "./..."], repo, env=env)
            rep = json.loads(p.stdout)
            self.assertEqual(rep["deps_changed"], [])
            self.assertEqual({v["id"]: v["verdict"] for v in rep["results"]}, {"example.com/m/v::TestValue": "new"})
            self.assertEqual(rep["results"][0]["base"]["passed"], 2)
            self.assertEqual(p.returncode, 1)
        finally:
            rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
