"""Verify identity failures propagate through the real client and router."""
import json
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.model_client import ModelClient

CHAMPION = "a" * 64
CHALLENGER = "b" * 64


def upstream(expected_hash, returned_hash):
    client = ModelClient(
        "http://127.0.0.1:5001", expected_threshold=0.7,
        expected_sha256=expected_hash,
    )
    response = MagicMock()
    response.status = 200
    response.read.return_value = json.dumps({
        "predictions": [{
            "score": 0.8, "threshold": 0.7, "predicted_fraud": True,
            "model_sha256": returned_hash,
        }]
    }).encode()
    response.__enter__.return_value = response
    client.opener = MagicMock()
    client.opener.open.return_value = response
    return client


class GatewayIdentityTests(unittest.TestCase):
    transaction = {
        "type": "TRANSFER", "amount": 100.0, "oldbalanceOrg": 100.0,
    }

    def test_champion_identity_failure_returns_generic_503(self):
        champion = upstream(CHAMPION, CHALLENGER)
        router = CanaryRouter(champion, rollback_on_failure=True)
        with TestClient(create_app(router, "k" * 32)) as client:
            response = client.post("/predict", json=self.transaction)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {
            "detail": "Scoring service is temporarily unavailable."
        })
        self.assertEqual(router.snapshot()["fallback_attempts"], 0)

    def test_challenger_identity_failure_removes_traffic_and_falls_back(self):
        champion = upstream(CHAMPION, CHAMPION)
        challenger = upstream(CHALLENGER, CHAMPION)
        router = CanaryRouter(
            champion, challenger, weight=100, rollback_on_failure=True,
        )
        with TestClient(create_app(router, "k" * 32)) as client:
            first = client.post("/predict", json=self.transaction)
            second = client.post("/predict", json=self.transaction)

        self.assertEqual(first.status_code, 200)
        body = first.json()
        self.assertEqual(body["requested_route"], "challenger")
        self.assertEqual(body["route"], "champion")
        self.assertEqual(body["model_sha256"], CHAMPION)
        self.assertTrue(body["fallback"])
        self.assertTrue(body["rollback_triggered"])

        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["requested_route"], "champion")
        self.assertFalse(second.json()["fallback"])
        self.assertEqual(router.snapshot()["canary_weight"], 0)
        self.assertEqual(router.snapshot()["automatic_rollbacks"], 1)
        self.assertEqual(challenger.opener.open.call_count, 1)
        self.assertEqual(champion.opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
