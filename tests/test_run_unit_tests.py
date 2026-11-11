"""Verify the DVC unit-test runner records accurate results."""

import importlib.util
import io
import json
import os
import platform
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_unit_tests_under_test", ROOT / "scripts/run_unit_tests.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)

PASSING = '''
import unittest

class Passing(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertEqual(1 + 1, 2)
'''

MIXED = '''
import unittest

class Mixed(unittest.TestCase):
    def test_passes(self):
        pass

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise RuntimeError("deliberate")

    @unittest.skip("deliberate skip")
    def test_skipped(self):
        pass
'''


class RunUnitTestsTests(unittest.TestCase):
    def suite_folder(self, source):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / "test_generated.py").write_text(source)
        self.addCleanup(lambda: sys.modules.pop("test_generated", None))
        self.addCleanup(
            lambda: sys.path.remove(folder.name) if folder.name in sys.path else None
        )
        return Path(folder.name)

    def test_environment_matches_the_values_used_by_run_ci(self):
        environment = runner.spark_environment("/x/python", "/repo")
        self.assertEqual(environment, {
            "PYTHONPATH": "/repo/src:/repo",
            "PYSPARK_PYTHON": "/x/python",
            "PYSPARK_DRIVER_PYTHON": "/x/python",
            "SPARK_LOCAL_IP": "127.0.0.1",
            "PYSPARK_SUBMIT_ARGS": "--driver-memory 1g pyspark-shell",
        })
        ci = (ROOT / "scripts/run_ci.py").read_text()
        for literal in ('"127.0.0.1"', '"--driver-memory 1g pyspark-shell"',
                        '"PYSPARK_PYTHON"', '"PYSPARK_DRIVER_PYTHON"'):
            self.assertIn(literal, ci)

    def test_passing_suite_writes_accurate_result(self):
        folder = self.suite_folder(PASSING)
        output = folder / "result.json"
        code = runner.run(folder, output, ROOT, stream=io.StringIO())
        record = json.loads(output.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(record["tests_run"], 2)
        self.assertEqual(record["failures"], 0)
        self.assertEqual(record["errors"], 0)
        self.assertIs(record["successful"], True)
        self.assertEqual(record["python_version"], platform.python_version())
        self.assertRegex(record["git_commit"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(record["git_dirty"], bool)
        self.assertGreaterEqual(record["duration_seconds"], 0)

    def test_failures_errors_and_skips_are_recorded_with_nonzero_exit(self):
        folder = self.suite_folder(MIXED)
        output = folder / "nested" / "result.json"
        code = runner.run(folder, output, ROOT, stream=io.StringIO())
        record = json.loads(output.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(record["tests_run"], 4)
        self.assertEqual(record["failures"], 1)
        self.assertEqual(record["errors"], 1)
        self.assertEqual(record["skipped"], 1)
        self.assertIs(record["successful"], False)

    def test_main_sets_environment_and_targets_the_project_tests(self):
        calls = []

        def fake_run(test_dir, result_path, root, stream=None):
            calls.append((test_dir, result_path, root))
            return 0

        original_path = list(sys.path)
        original_cwd = os.getcwd()
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), original_path))
        self.addCleanup(os.chdir, original_cwd)
        with patch.dict(os.environ), patch.object(runner, "run", fake_run):
            self.assertEqual(runner.main(), 0)
            self.assertEqual(os.environ["SPARK_LOCAL_IP"], "127.0.0.1")
            self.assertEqual(os.environ["PYSPARK_PYTHON"], sys.executable)
            self.assertTrue(re.search(r"src:.+", os.environ["PYTHONPATH"]))
        self.assertEqual(calls, [(
            ROOT / "tests", ROOT / "reports/unit_tests_result.json", ROOT,
        )])


if __name__ == "__main__":
    unittest.main()
