"""Verify bounded, read-only pipeline history responses."""

import json
from pathlib import Path
import tempfile
import unittest

from backend.pipeline import MAX_BYTES, read_history


class PipelineHistoryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "history.jsonl"

    def record(self, name="example", status="success"):
        return {
            "run_id": name,
            "stage": "automated_tests",
            "status": status,
            "duration_seconds": 1.25,
            "git_commit": "a" * 40,
            "git_dirty": False,
        }

    def write(self, records):
        self.path.write_text(
            "".join(json.dumps(row) + "\n" for row in records)
        )

    def test_missing_history(self):
        result = read_history(self.path, 10)
        self.assertFalse(result["history_available"])
        self.assertEqual(result["runs"], [])

    def test_newest_first_and_limit(self):
        self.write([self.record(str(i)) for i in range(5)])
        result = read_history(self.path, 2)
        self.assertEqual(
            [row["run_id"] for row in result["runs"]], ["4", "3"]
        )

    def test_invalid_and_partial_records(self):
        self.path.write_text(
            json.dumps(self.record()) + '\ninvalid\n{"unfinished":'
        )
        result = read_history(self.path, 10)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["invalid_records"], 2)

    def test_nonfinite_duration_rejected(self):
        row = self.record()
        row["duration_seconds"] = float("inf")
        self.write([row])
        result = read_history(self.path, 10)
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["invalid_records"], 1)

    def test_omits_raw_command_and_logs(self):
        row = self.record(status="failed")
        row.update({
            "command": ["private-command"],
            "stderr": "private-log",
            "gate": {"failures": ["Quality floor not met."]},
        })
        self.write([row])
        actual = read_history(self.path, 10)["runs"][0]
        self.assertNotIn("command", actual)
        self.assertNotIn("stderr", actual)
        self.assertEqual(
            actual["failure_reasons"], ["Quality floor not met."]
        )

    def test_large_history_is_bounded(self):
        self.path.write_bytes(
            b"x" * (MAX_BYTES + 100) + b"\n"
            + json.dumps(self.record("latest")).encode() + b"\n"
        )
        result = read_history(self.path, 10)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["runs"][0]["run_id"], "latest")


if __name__ == "__main__":
    unittest.main()
