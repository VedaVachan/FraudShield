"""Guard the contract of scripts/e2e_smoke.py without needing the running stack.

The smoke script itself is never run here against real services; it is kept out of
CI. These tests only check how it behaves when nothing answers, and that CI does not
call it.
"""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "e2e_smoke_under_test", ROOT / "scripts/e2e_smoke.py"
)
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)

NOTHING_LISTENS = "http://127.0.0.1:9"


class E2eSmokeContractTests(unittest.TestCase):
    def run_against_nothing(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        output = Path(folder.name) / "nested/result.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = smoke.main([
                "--frontend", NOTHING_LISTENS, "--api", NOTHING_LISTENS,
                "--prometheus", NOTHING_LISTENS, "--grafana", NOTHING_LISTENS,
                "--timeout", "1", "--scrape-wait", "0", "--output", str(output),
            ])
        return code, json.loads(output.read_text())

    def test_unreachable_stack_exits_non_zero_and_records_every_failure(self):
        code, record = self.run_against_nothing()
        self.assertEqual(code, 1)
        self.assertFalse(record["passed"])
        self.assertEqual(record["checks_run"], 9)
        self.assertEqual(record["checks_failed"], 9)
        self.assertTrue(all("not reachable" in c["detail"] for c in record["checks"]))

    def test_result_file_states_it_is_not_part_of_ci(self):
        _, record = self.run_against_nothing()
        self.assertIn("not part of unit tests or scripts/run_ci.py", record["scope"])
        self.assertEqual(record["schema_version"], 1)

    def test_result_file_is_replaced_not_appended(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "result.json"
        smoke.write_result({"first": True}, path)
        smoke.write_result({"second": True}, path)
        self.assertEqual(json.loads(path.read_text()), {"second": True})
        self.assertEqual([p.name for p in Path(folder.name).iterdir()], ["result.json"])

    def test_http_error_statuses_are_returned_not_raised(self):
        # Port 9 refuses the connection: that is "unavailable", never a fake status.
        with self.assertRaises(smoke.StackUnavailable):
            smoke.fetch(NOTHING_LISTENS + "/health", timeout=1)

    def test_ci_and_dvc_do_not_call_the_smoke_script(self):
        for name in ("scripts/run_ci.py", "dvc.yaml", "scripts/run_unit_tests.py"):
            with self.subTest(file=name):
                self.assertNotIn("e2e_smoke", (ROOT / name).read_text())

    def test_script_name_is_not_a_unittest_discovery_match(self):
        self.assertFalse(Path("scripts/e2e_smoke.py").match("test*.py"))
        self.assertFalse(list((ROOT / "tests").glob("test_e2e_smoke.py")))


if __name__ == "__main__":
    unittest.main()
