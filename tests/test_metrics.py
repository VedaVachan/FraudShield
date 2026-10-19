"""Test API metrics without changing the running Docker application."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import app
from backend.metrics import REGISTRY


PAYLOAD = {
    "type": "TRANSFER",
    "amount": 100000.0,
    "oldbalanceOrg": 100000.0,
}


def metric(name, **labels):
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def decisions(endpoint):
    return sum(
        metric(
            "fraudshield_decisions_total",
            endpoint=endpoint,
            decision=decision,
        )
        for decision in ("flagged", "not_flagged")
    )


class MetricsTests(unittest.TestCase):
    def test_success_and_endpoint_separation(self):
        with TestClient(app) as client:
            for endpoint in ("/predict", "/explain"):
                with self.subTest(endpoint=endpoint):
                    other = (
                        "/explain" if endpoint == "/predict" else "/predict"
                    )
                    other_before = decisions(other)
                    decisions_before = decisions(endpoint)
                    requests_before = metric(
                        "fraudshield_requests_total",
                        endpoint=endpoint,
                        status="200",
                    )
                    latency_before = metric(
                        "fraudshield_request_duration_seconds_count",
                        endpoint=endpoint,
                    )
                    decision_before = {
                        decision: metric(
                            "fraudshield_decisions_total",
                            endpoint=endpoint,
                            decision=decision,
                        )
                        for decision in ("flagged", "not_flagged")
                    }

                    response = client.post(endpoint, json=PAYLOAD)
                    self.assertEqual(response.status_code, 200)
                    selected = (
                        "flagged"
                        if response.json()["predicted_fraud"]
                        else "not_flagged"
                    )

                    self.assertEqual(
                        metric(
                            "fraudshield_requests_total",
                            endpoint=endpoint,
                            status="200",
                        ),
                        requests_before + 1,
                    )
                    self.assertEqual(
                        metric(
                            "fraudshield_request_duration_seconds_count",
                            endpoint=endpoint,
                        ),
                        latency_before + 1,
                    )
                    self.assertEqual(decisions(endpoint), decisions_before + 1)
                    self.assertEqual(decisions(other), other_before)
                    for decision, before in decision_before.items():
                        self.assertEqual(
                            metric(
                                "fraudshield_decisions_total",
                                endpoint=endpoint,
                                decision=decision,
                            ),
                            before + int(decision == selected),
                        )

            # Health checks and scrapes must not inflate scoring metrics.
            before = REGISTRY.get_sample_value(
                "fraudshield_request_duration_seconds_count",
                {"endpoint": "/predict"},
            )
            decisions_before = decisions("/predict") + decisions("/explain")

            self.assertEqual(client.get("/health").status_code, 200)
            response = client.get("/metrics")
            self.assertEqual(response.status_code, 200)
            self.assertIn("fraudshield_requests_total", response.text)
            self.assertIn('le="0.05"', response.text)
            self.assertNotIn("oldbalanceOrg", response.text)

            self.assertEqual(
                metric(
                    "fraudshield_request_duration_seconds_count",
                    endpoint="/predict",
                ),
                before,
            )
            self.assertEqual(
                decisions("/predict") + decisions("/explain"),
                decisions_before,
            )

    def test_invalid_inputs_are_not_decisions(self):
        with TestClient(app) as client:
            for endpoint in ("/predict", "/explain"):
                with self.subTest(endpoint=endpoint):
                    before = decisions(endpoint)
                    errors_before = metric(
                        "fraudshield_requests_total",
                        endpoint=endpoint,
                        status="422",
                    )

                    response = client.post(
                        endpoint, json={**PAYLOAD, "amount": -1.0}
                    )

                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(decisions(endpoint), before)
                    self.assertEqual(
                        metric(
                            "fraudshield_requests_total",
                            endpoint=endpoint,
                            status="422",
                        ),
                        errors_before + 1,
                    )

    def test_server_failure_is_counted(self):
        with TestClient(app, raise_server_exceptions=False) as client:
            before = decisions("/predict")
            errors_before = metric(
                "fraudshield_requests_total",
                endpoint="/predict",
                status="500",
            )

            with patch.object(
                app.state.pipeline,
                "predict_proba",
                side_effect=RuntimeError("Simulated test failure"),
            ):
                response = client.post("/predict", json=PAYLOAD)

            self.assertEqual(response.status_code, 500)
            self.assertEqual(decisions("/predict"), before)
            self.assertEqual(
                metric(
                    "fraudshield_requests_total",
                    endpoint="/predict",
                    status="500",
                ),
                errors_before + 1,
            )


if __name__ == "__main__":
    unittest.main()
