"""Verify notification recording, validation and storage errors."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.alert_receiver import create_app, MAX_BYTES


class AlertReceiverTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = Path(folder.name) / "notifications.jsonl"
        self.client = TestClient(create_app(self.history))
        self.addCleanup(self.client.close)

    def payload(self, status="firing"):
        return {
            "status": status,
            "alerts": [{
                "status": status,
                "labels": {
                    "alertname": "FraudShieldDeliveryCheck",
                    "severity": "info",
                },
            }],
        }

    def test_records_firing_and_resolved(self):
        for status in ("firing", "resolved"):
            response = self.client.post("/alerts", json=self.payload(status))
            self.assertEqual(response.status_code, 200)
        records = [
            json.loads(line) for line in self.history.read_text().splitlines()
        ]
        self.assertEqual(
            [row["status"] for row in records], ["firing", "resolved"]
        )
        self.assertEqual(len({row["delivery_id"] for row in records}), 2)

    def test_rejects_invalid_json(self):
        response = self.client.post(
            "/alerts", content=b"{",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertFalse(self.history.exists())

    def test_rejects_invalid_payloads(self):
        bad_label = self.payload()
        bad_label["alerts"][0]["labels"]["severity"] = 123
        for payload in (
            [],
            {},
            {"status": "unknown", "alerts": []},
            {"status": "firing", "alerts": []},
            {"status": "firing", "alerts": [{}]},
            bad_label,
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/alerts", json=payload)
                self.assertEqual(response.status_code, 422)
        self.assertFalse(self.history.exists())

    def test_rejects_too_many_alerts(self):
        payload = self.payload()
        payload["alerts"] *= 21
        self.assertEqual(
            self.client.post("/alerts", json=payload).status_code, 422
        )
        self.assertFalse(self.history.exists())

    def test_rejects_oversized_body(self):
        response = self.client.post("/alerts", content=b"x" * (MAX_BYTES + 1))
        self.assertEqual(response.status_code, 413)
        self.assertFalse(self.history.exists())

    def test_omits_unapproved_labels_and_annotations(self):
        payload = self.payload()
        payload["alerts"][0]["labels"]["private_detail"] = "omit-this"
        payload["alerts"][0]["annotations"] = {"description": "omit-this-too"}
        self.assertEqual(
            self.client.post("/alerts", json=payload).status_code, 200
        )
        text = self.history.read_text()
        self.assertNotIn("omit-this", text)
        self.assertNotIn("annotations", text)

    def test_storage_failure_returns_generic_503(self):
        with patch.object(
            Path, "open", side_effect=OSError("private filesystem detail")
        ):
            response = self.client.post("/alerts", json=self.payload())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private filesystem detail", response.text)

    def test_health_does_not_create_history(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertFalse(self.history.exists())


if __name__ == "__main__":
    unittest.main()
