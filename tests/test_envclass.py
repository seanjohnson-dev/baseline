import tempfile
import unittest
from pathlib import Path

from _support import mod

e = mod("envclass")


def ctx(env=None, files=None, reader=None):
    d = Path(tempfile.mkdtemp())
    for rel in files or []:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("")
    return e.EnvContext(str(d), env={} if env is None else env, source_reader=reader)


class EnvClassTest(unittest.TestCase):
    def cls(self, key, raw="", c=None):
        r = e.classify(key, raw, c or ctx())
        return r and r["class"]

    def test_python_missing_third_party_module(self):
        self.assertEqual(self.cls("ModuleNotFoundError: No module named 'psycopg2'"), "missing_module")

    def test_python_missing_first_party_module_is_code_error(self):
        c = ctx(files=["shop/__init__.py"])
        self.assertIsNone(self.cls("ModuleNotFoundError: No module named 'shop.report'", c=c))
        c2 = ctx(files=["src/app/__init__.py"])
        self.assertIsNone(self.cls("ModuleNotFoundError: No module named 'app'", c=c2))

    def test_js_modules(self):
        self.assertEqual(self.cls("Error: Cannot find package 'pg-not-installed' imported from 'test/x.js'"),
                         "missing_module")
        self.assertIsNone(self.cls("Error: Cannot find module '../src/report.js' imported from 'test/x.js'"))
        self.assertIsNone(self.cls("Error: Failed to resolve import \"@/lib/x\" from \"src/a.ts\""))
        self.assertEqual(self.cls("Error: Failed to resolve import \"lodash-es\" from \"src/a.ts\""),
                         "missing_module")

    def test_go_missing_module_and_executable(self):
        self.assertEqual(self.cls("x.go:3:2: no required module provides package github.com/a/b; to add it"),
                         "missing_module")
        self.assertEqual(self.cls('env_test.go:<L>: required tool missing: exec: "psql": executable file not '
                                  'found in $PATH'), "missing_executable")

    def test_node_spawn_and_python_subprocess(self):
        self.assertEqual(self.cls("Error: spawn psql ENOENT"), "missing_executable")
        self.assertEqual(self.cls("FileNotFoundError: [Errno 2] No such file or directory: 'psql'",
                                  "subprocess.py: in _execute_child"), "missing_executable")
        self.assertIsNone(self.cls("FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'"))

    def test_env_var_requires_really_unset(self):
        self.assertEqual(self.cls("KeyError: 'DATABASE_URL'", "os.environ[...]"), "missing_env_var")
        self.assertIsNone(self.cls("KeyError: 'DATABASE_URL'", "os.environ", ctx(env={"DATABASE_URL": "x"})))
        self.assertIsNone(self.cls("KeyError: 'NOT_A_VAR'", "d['NOT_A_VAR']"))
        self.assertEqual(self.cls("env_test.go:<L>: DATABASE_URL not set"), "missing_env_var")
        self.assertEqual(self.cls("RuntimeError: environment variable API_TOKEN is required"), "missing_env_var")
        self.assertIsNone(self.cls("RuntimeError: some_value not set"))

    def test_js_env_var_from_source_line(self):
        def reader(path, line):
            return "  expect(process.env.DATABASE_URL.startsWith('postgres')).toBe(true);" if line == 4 else ""
        c = ctx(reader=reader)
        raw = ("TypeError: Cannot read properties of undefined (reading 'startsWith')\n"
               "    at /repo/test/env.test.js:4:57\n    at /repo/node_modules/x.js:1:1")
        self.assertEqual(self.cls("TypeError: Cannot read properties of undefined (reading 'startsWith')", raw, c),
                         "missing_env_var")
        c2 = e.EnvContext("/repo", env={"DATABASE_URL": "postgres://"}, source_reader=reader)
        self.assertIsNone(self.cls("TypeError: Cannot read properties of undefined (reading 'startsWith')", raw, c2))

    def test_services(self):
        self.assertEqual(self.cls("ConnectionRefusedError: [Errno 111] Connection refused"), "service_unavailable")
        self.assertEqual(self.cls("Error: connect ECONNREFUSED 127.0.0.1:<PORT>"), "service_unavailable")
        self.assertEqual(self.cls("dial: dial tcp 127.0.0.1:<PORT>: connect: connection refused"),
                         "service_unavailable")
        self.assertEqual(self.cls("dial tcp 10.0.0.1:5432: i/o timeout"), "service_timeout")
        self.assertEqual(self.cls("Error: getaddrinfo ENOTFOUND db.internal"), "dns_failure")

    def test_config_files(self):
        self.assertEqual(self.cls("FileNotFoundError: [Errno 2] No such file or directory: '.env'"),
                         "missing_config")
        self.assertEqual(self.cls("open config/credentials.json: no such file or directory"), "missing_config")

    def test_plain_assertions_are_never_environment(self):
        self.assertIsNone(self.cls("AssertionError: assert 1.57 == 1.58"))
        self.assertIsNone(self.cls("AssertionError: expected 'connection refused' to be 'ok'"))
        self.assertIsNone(self.cls("assert 'ECONNREFUSED' == ''"))
        self.assertIsNone(self.cls("AssertionError: expected undefined to be defined",
                                   "process.env.DATABASE_URL"))


if __name__ == "__main__":
    unittest.main()
