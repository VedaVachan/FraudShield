"""Exercise model quality gates with isolated temporary artifacts."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_model_gate.py"


class ModelGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.model = self.folder / "model.joblib"
        self.model.write_bytes(b"gate-test-fixture-not-a-real-model")
        self.report = {
            "model": "xgboost",
            "evaluation_split": "validation",
            "test_evaluated": False,
            "serialization_check": "PASS",
            "model_sha256": hashlib.sha256(
                self.model.read_bytes()
            ).hexdigest(),
            "average_precision": 0.95,
            "at_selected_threshold": {
                "precision": 0.90,
                "recall": 0.95,
                "threshold": 0.80,
            },
        }

    def run_gate(self, *args):
        (self.folder / "validation.json").write_text(
            json.dumps(self.report), encoding="utf-8"
        )
        result = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--model-dir", str(self.folder), *args],
            capture_output=True, text=True, check=False,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_accepts_valid_report(self):
        code, output = self.run_gate()
        self.assertEqual(code, 0)
        self.assertEqual(output["status"], "PASS")
        self.assertEqual(output["failures"], [])

    def test_rejects_each_metric_below_floor(self):
        for option in ("--min-ap", "--min-precision", "--min-recall"):
            with self.subTest(option=option):
                code, output = self.run_gate(option, "1.0")
                self.assertEqual(code, 1)
                self.assertEqual(output["status"], "FAIL")

    def test_rejects_changed_model(self):
        self.model.write_bytes(b"changed-model")
        code, output = self.run_gate()
        self.assertEqual(code, 1)
        self.assertTrue(any(
            "checksum" in message for message in output["failures"]
        ))

    def test_rejects_test_evaluation(self):
        self.report["evaluation_split"] = "test"
        self.report["test_evaluated"] = True
        code, output = self.run_gate()
        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "FAIL")

    def test_rejects_nonfinite_metric(self):
        self.report["average_precision"] = float("nan")
        code, output = self.run_gate()
        self.assertEqual(code, 1)
        self.assertTrue(any(
            "finite" in message for message in output["failures"]
        ))

    def test_rejects_missing_field(self):
        del self.report["at_selected_threshold"]["recall"]
        code, output = self.run_gate()
        self.assertEqual(code, 1)
        self.assertEqual(output["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
