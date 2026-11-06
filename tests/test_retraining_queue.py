"""Verify drift queue filtering, deduplication and cooldown."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from backend.retraining_queue import (
    COOLDOWN_SECONDS, connect, enqueue_notification,
)


class RetrainingQueueTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "queue.db"

    def record(self, status="firing", name="FraudShieldInputDrift"):
        return {
            "delivery_id": str(uuid4()),
            "received_utc": "2026-11-06T09:00:00+00:00",
            "status": status,
            "alerts": [{
                "status": status,
                "labels": {
                    "alertname": name,
                    "job": "fraudshield-api",
                    "instance": "api:8000",
                    "feature": "amount",
                },
            }],
        }

    def rows(self):
        connection = connect(self.database)
        try:
            return [
                dict(row)
                for row in connection.execute("SELECT * FROM requests")
            ]
        finally:
            connection.close()

    def finish(self, status):
        connection = connect(self.database)
        try:
            connection.execute("UPDATE requests SET status = ?", (status,))
        finally:
            connection.close()

    def test_queues_drift_once(self):
        record = self.record()
        first = enqueue_notification(self.database, record, now=100)
        second = enqueue_notification(self.database, record, now=101)
        self.assertEqual(first[0]["decision"], "queued")
        self.assertEqual(second[0]["decision"], "duplicate_delivery")
        self.assertEqual(len(self.rows()), 1)

    def test_ignores_resolved_and_synthetic_notifications(self):
        for record in (
            self.record(status="resolved"),
            self.record(name="FraudShieldDeliveryCheck"),
        ):
            with self.subTest(record=record):
                result = enqueue_notification(self.database, record, now=100)
                self.assertEqual(result[0]["decision"], "ignored")
        self.assertEqual(self.rows(), [])

    def test_active_request_blocks_new_delivery(self):
        first = enqueue_notification(self.database, self.record(), now=100)
        result = enqueue_notification(self.database, self.record(), now=101)
        self.assertEqual(result[0]["decision"], "already_active")
        self.assertEqual(result[0]["request_id"], first[0]["request_id"])
        self.assertEqual(len(self.rows()), 1)

    def test_running_request_still_blocks_after_cooldown(self):
        enqueue_notification(self.database, self.record(), now=100)
        self.finish("running")
        result = enqueue_notification(
            self.database, self.record(), now=100 + COOLDOWN_SECONDS + 1,
        )
        self.assertEqual(result[0]["decision"], "already_active")
        self.assertEqual(len(self.rows()), 1)

    def test_finished_request_obeys_cooldown(self):
        enqueue_notification(self.database, self.record(), now=100)
        self.finish("succeeded")
        blocked = enqueue_notification(
            self.database, self.record(), now=101,
        )
        self.assertEqual(blocked[0]["decision"], "cooldown")
        allowed = enqueue_notification(
            self.database, self.record(), now=100 + COOLDOWN_SECONDS,
        )
        self.assertEqual(allowed[0]["decision"], "queued")
        self.assertEqual(len(self.rows()), 2)

    def test_combines_features_for_same_instance(self):
        record = self.record()
        other = deepcopy(record["alerts"][0])
        other["labels"]["feature"] = "type"
        record["alerts"].append(other)
        enqueue_notification(self.database, record, now=100)
        import json
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        evidence = json.loads(rows[0]["evidence_json"])
        self.assertEqual(evidence["features"], ["amount", "type"])
        self.assertIs(evidence["automatic_promotion"], False)

    def test_separate_instances_have_separate_requests(self):
        enqueue_notification(self.database, self.record(), now=100)
        other = self.record()
        other["alerts"][0]["labels"]["instance"] = "another-api:8000"
        result = enqueue_notification(self.database, other, now=101)
        self.assertEqual(result[0]["decision"], "queued")
        self.assertEqual(len(self.rows()), 2)

    def test_rejects_invalid_id_and_timestamp(self):
        bad = self.record()
        bad["delivery_id"] = "invalid"
        with self.assertRaises(ValueError):
            enqueue_notification(self.database, bad, now=100)
        for clock in (float("nan"), float("inf"), -1, True):
            with self.subTest(clock=clock):
                with self.assertRaises(ValueError):
                    enqueue_notification(
                        self.database, self.record(), now=clock,
                    )
        self.assertFalse(self.database.exists())

    def test_ignores_wrong_job_and_unknown_feature(self):
        for key, value in (
            ("job", "another-service"),
            ("feature", "unknown"),
        ):
            record = self.record()
            record["alerts"][0]["labels"][key] = value
            result = enqueue_notification(self.database, record, now=100)
            self.assertEqual(result[0]["decision"], "ignored")
        self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
