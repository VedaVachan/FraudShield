"""Verify gateway HTTP contracts, access control and automatic removal."""

import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.model_client import ModelServerError


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.key = "test-only-key-" + "x" * 32
        self.champion = Mock()
        self.challenger = Mock()
        prediction = {
            "score": 0.9, "threshold": 0.8, "predicted_fraud": True
        }
        self.champion.predict.return_value = prediction
        self.challenger.predict.return_value = prediction
        self.router = CanaryRouter(
            self.champion, self.challenger,
            weight=0, rollback_on_failure=True,
        )
        self.client = TestClient(create_app(self.router, self.key))
        self.addCleanup(self.client.close)
        self.payload = {
            "type": "TRANSFER", "amount": 100.0, "oldbalanceOrg": 100.0
        }

    def test_health_is_configuration_status(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "configured")
        self.champion.predict.assert_not_called()

    def test_prediction_uses_champion_and_preserves_id(self):
        response = self.client.post(
            "/predict", json=self.payload,
            headers={"X-Transaction-ID": "gateway-example"},
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["transaction_id"], "gateway-example")
        self.assertEqual(result["route"], "champion")
        self.champion.predict.assert_called_once_with(self.payload)

    def test_prediction_generates_id(self):
        response = self.client.post("/predict", json=self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["transaction_id"])

    def test_admin_rejects_missing_and_wrong_keys(self):
        for headers in ({}, {"X-API-Key": "wrong"}):
            with self.subTest(headers=headers):
                response = self.client.post(
                    "/admin/canary", json={"weight": 100}, headers=headers,
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.router.snapshot()["canary_weight"], 0)

    def test_authorized_weight_change_routes_to_challenger(self):
        response = self.client.post(
            "/admin/canary", json={"weight": 100},
            headers={"X-API-Key": self.key},
        )
        self.assertEqual(response.status_code, 200)
        result = self.client.post("/predict", json=self.payload)
        self.assertEqual(result.json()["route"], "challenger")

    def test_bad_weight_rejected(self):
        for weight in (-1, 101, True, "10"):
            with self.subTest(weight=weight):
                response = self.client.post(
                    "/admin/canary", json={"weight": weight},
                    headers={"X-API-Key": self.key},
                )
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.router.snapshot()["canary_weight"], 0)

    def test_challenger_failure_disables_traffic(self):
        self.router.set_weight(100)
        self.challenger.predict.side_effect = ModelServerError("private-error")
        first = self.client.post("/predict", json=self.payload)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["fallback"])
        self.assertTrue(first.json()["rollback_triggered"])
        self.assertEqual(self.client.get("/routing").json()["canary_weight"], 0)
        second = self.client.post("/predict", json=self.payload)
        self.assertEqual(second.json()["requested_route"], "champion")
        self.challenger.predict.assert_called_once()

    def test_champion_failure_returns_generic_503(self):
        self.champion.predict.side_effect = ModelServerError("private-error")
        response = self.client.post("/predict", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-error", response.text)

    def test_invalid_transaction_never_calls_models(self):
        response = self.client.post(
            "/predict", json={**self.payload, "amount": -1.0},
        )
        self.assertEqual(response.status_code, 422)
        self.champion.predict.assert_not_called()
        self.challenger.predict.assert_not_called()

    def test_blank_transaction_id_rejected(self):
        response = self.client.post(
            "/predict", json=self.payload,
            headers={"X-Transaction-ID": " "},
        )
        self.assertEqual(response.status_code, 422)
        self.champion.predict.assert_not_called()

    def test_missing_challenger_configuration_returns_409(self):
        router = CanaryRouter(self.champion)
        with TestClient(create_app(router, self.key)) as client:
            response = client.post(
                "/admin/canary", json={"weight": 10},
                headers={"X-API-Key": self.key},
            )
        self.assertEqual(response.status_code, 409)

    def test_short_admin_key_rejected(self):
        with self.assertRaises(ValueError):
            create_app(self.router, "short")


if __name__ == "__main__":
    unittest.main()
