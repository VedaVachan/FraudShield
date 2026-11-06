"""Verify recent notification scanning and replay protection."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from scripts.enqueue_drift_requests import MAX_BYTES, scan_once


class DriftScannerTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = Path(folder.name) / "notifications.jsonl"
        self.database = Path(folder.name) / "queue.db"
        self.clock = 1800000000

    def record(self, age=0):
        return {
            "delivery_id": str(uuid4()),
            "received_utc": datetime.fromtimestamp(
                self.clock - age, timezone.utc
            ).isoformat(),
            "status": "firing",
            "alerts": [{
                "status": "firing",
                "labels": {
                    "alertname": "FraudShieldInputDrift",
                    "job": "fraudshield-api",
                    "instance": "api:8000",
                    "feature": "amount",
                },
            }],
        }

    def write(self, records):
        self.history.write_text(
            "".join(json.dumps(row) + "\n" for row in records)
        )

    def scan(self):
        return scan_once(self.history, self.database, now=self.clock)

    def test_missing_history_does_not_create_database(self):
        result = self.scan()
        self.assertFalse(result["history_available"])
        self.assertFalse(self.database.exists())

    def test_recent_drift_is_queued_and_replay_is_duplicate(self):
        self.write([self.record()])
        first = self.scan()
        second = self.scan()
        self.assertEqual(first["decisions"], {"queued": 1})
        self.assertEqual(len(first["new_requests"]), 1)
        self.assertEqual(second["decisions"], {"duplicate_delivery": 1})
        self.assertEqual(second["new_requests"], [])

    def test_old_and_future_notifications_are_skipped(self):
        self.write([self.record(age=901), self.record(age=-61)])
        result = self.scan()
        self.assertEqual(result["outside_time_window"], 2)
        self.assertEqual(result["new_requests"], [])
        self.assertFalse(self.database.exists())

    def test_malformed_partial_and_naive_timestamps_are_skipped(self):
        naive = self.record()
        naive["received_utc"] = "2026-11-06T09:00:00"
        self.history.write_bytes(
            b"{bad json}\n"
            + (json.dumps(naive) + "\n").encode()
            + json.dumps(self.record()).encode()
        )
        result = self.scan()
        self.assertEqual(result["invalid_records"], 3)
        self.assertEqual(result["new_requests"], [])

    def test_large_history_keeps_complete_recent_tail(self):
        recent = (json.dumps(self.record()) + "\n").encode()
        self.history.write_bytes(b"x" * (MAX_BYTES + 100) + b"\n" + recent)
        result = self.scan()
        self.assertEqual(result["decisions"], {"queued": 1})
        self.assertEqual(result["invalid_records"], 0)

    def test_synthetic_delivery_check_is_ignored(self):
        record = self.record()
        record["alerts"][0]["labels"]["alertname"] = "FraudShieldDeliveryCheck"
        self.write([record])
        result = self.scan()
        self.assertEqual(result["decisions"], {"ignored": 1})
        self.assertEqual(result["new_requests"], [])


if __name__ == "__main__":
    unittest.main()
