"""Verify gateway metrics against actual request and routing outcomes."""

import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.model_client import ModelServerError


class GatewayMetricsTests(unittest.TestCase):
    def make_client(self, weight=0):
        prediction = {
            "score": 0.9, "threshold": 0.8, "predicted_fraud": True,
        }
        self.champion = Mock()
        self.challenger = Mock()
        self.champion.predict.return_value = prediction.copy()
        self.challenger.predict.return_value = prediction.copy()
        self.routing = CanaryRouter(
            self.champion, self.challenger,
            weight=weight, rollback_on_failure=True,
        )
        self.client = TestClient(create_app(self.routing, "k" * 32))
        self.addCleanup(self.client.close)
        return self.client

    def samples(self):
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        return [
            sample
            for family in text_string_to_metric_families(response.text)
            for sample in family.samples
        ]

    def value(self, name, **labels):
        matches = [
            sample.value for sample in self.samples()
            if sample.name == name and sample.labels == labels
        ]
        self.assertEqual(len(matches), 1, (name, labels, matches))
        return matches[0]

    def predict(self):
        return self.client.post("/predict", json={
            "type": "TRANSFER",
            "amount": 100.0,
            "oldbalanceOrg": 100.0,
        })

    def test_success_counts_request_duration_and_route(self):
        self.make_client()
        self.assertEqual(self.predict().status_code, 200)
        self.assertEqual(self.value(
            "fraudshield_gateway_requests_total", status="200",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_request_duration_seconds_count",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_scoring_results_total",
            requested_route="champion", route="champion", fallback="false",
        ), 1)

    def test_invalid_input_is_not_a_scoring_result(self):
        self.make_client()
        self.assertEqual(
            self.client.post("/predict", json={}).status_code, 422,
        )
        self.assertEqual(self.value(
            "fraudshield_gateway_requests_total", status="422",
        ), 1)
        self.champion.predict.assert_not_called()
        self.challenger.predict.assert_not_called()
        self.assertFalse(any(
            sample.name == "fraudshield_gateway_scoring_results_total"
            for sample in self.samples()
        ))

    def test_fallback_and_automatic_removal_are_counted(self):
        self.make_client(weight=100)
        self.challenger.predict.side_effect = ModelServerError("unavailable")
        self.assertEqual(self.predict().status_code, 200)
        self.assertEqual(self.value(
            "fraudshield_gateway_scoring_results_total",
            requested_route="challenger", route="champion", fallback="true",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_fallback_attempts_total",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_automatic_removals_total",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_canary_weight_percent",
        ), 0)

    def test_both_upstreams_fail_without_success_result(self):
        self.make_client(weight=100)
        self.challenger.predict.side_effect = ModelServerError("unavailable")
        self.champion.predict.side_effect = ModelServerError("unavailable")
        self.assertEqual(self.predict().status_code, 503)
        self.assertEqual(self.value(
            "fraudshield_gateway_requests_total", status="503",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_fallback_attempts_total",
        ), 1)
        self.assertEqual(self.value(
            "fraudshield_gateway_automatic_removals_total",
        ), 1)
        self.assertFalse(any(
            sample.name == "fraudshield_gateway_scoring_results_total"
            for sample in self.samples()
        ))

    def test_metrics_scrapes_do_not_count_as_predictions(self):
        self.make_client()
        self.samples()
        self.samples()
        self.assertEqual(self.value(
            "fraudshield_gateway_request_duration_seconds_count",
        ), 0)

    def test_app_registries_are_isolated(self):
        first = self.make_client()
        self.assertEqual(self.predict().status_code, 200)
        second = self.make_client()
        self.assertIsNot(first, second)
        self.assertEqual(self.value(
            "fraudshield_gateway_request_duration_seconds_count",
        ), 0)


if __name__ == "__main__":
    unittest.main()
