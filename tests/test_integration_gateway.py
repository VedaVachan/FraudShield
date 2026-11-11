"""Integration: gateway app, canary router and the real HTTP model client together.

The only fakes are the two model servers at the network edge: small HTTP servers on
127.0.0.1 that speak the MLflow /invocations contract. The champion server scores with
the real deployed model, so the gateway result can be compared with the direct API.
"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from backend.app import Transaction, app, frame_for
from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.model_client import ModelClient

KEY = "integration-test-key-" + "k" * 24
PAYLOAD = {"type": "TRANSFER", "amount": 100000.0, "oldbalanceOrg": 100000.0}
CHALLENGER_SHA = "b" * 64
CHALLENGER_THRESHOLD = 0.5
CHALLENGER_SCORE = 0.8


def scraped(text, name, **labels):
    """Value of one sample in a Prometheus text scrape, or None if it is absent."""
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(key) == value for key, value in labels.items()
            ):
                return sample.value
    return None


def start_upstream(score_for, threshold, sha256):
    """Start a fake model server; return (state, base_url). mode: ok, fail, wrong_identity."""
    state = SimpleNamespace(mode="ok", hits=0)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            row = json.loads(self.rfile.read(length))["dataframe_records"][0]
            state.hits += 1
            if state.mode == "fail":
                self.send_response(500)
                self.end_headers()
                return
            score = float(score_for(row))
            body = json.dumps({"predictions": [{
                "score": score, "threshold": threshold,
                "predicted_fraud": score >= threshold,
                "model_sha256": ("c" * 64) if state.mode == "wrong_identity" else sha256,
            }]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.shutdown = lambda: (server.shutdown(), server.server_close(), thread.join(5))
    return state, f"http://127.0.0.1:{server.server_address[1]}"


class GatewayClientPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = TestClient(app)
        cls.api.__enter__()
        cls.addClassCleanup(cls.api.__exit__, None, None, None)

    def setUp(self):
        pipeline = app.state.pipeline

        def champion_score(row):
            return pipeline.predict_proba(frame_for(Transaction(**row)))[0, 1]

        self.champion, champion_url = start_upstream(
            champion_score, app.state.threshold, app.state.model_sha256
        )
        self.challenger, challenger_url = start_upstream(
            lambda row: CHALLENGER_SCORE, CHALLENGER_THRESHOLD, CHALLENGER_SHA
        )
        self.addCleanup(self.champion.shutdown)
        self.addCleanup(self.challenger.shutdown)

        self.router = CanaryRouter(
            ModelClient(champion_url, app.state.threshold, timeout=2.0,
                        expected_sha256=app.state.model_sha256),
            ModelClient(challenger_url, CHALLENGER_THRESHOLD, timeout=2.0,
                        expected_sha256=CHALLENGER_SHA),
            weight=0, rollback_on_failure=True,
        )
        self.gateway = TestClient(create_app(self.router, KEY))
        self.addCleanup(self.gateway.close)

    def metric(self, name, **labels):
        return scraped(self.gateway.get("/metrics").text, name, **labels)

    def set_weight(self, weight):
        response = self.gateway.post(
            "/admin/canary", json={"weight": weight}, headers={"X-API-Key": KEY}
        )
        self.assertEqual(response.status_code, 200)

    def test_champion_route_returns_the_same_score_as_the_direct_api(self):
        direct = self.api.post("/predict", json=PAYLOAD).json()
        response = self.gateway.post(
            "/predict", json=PAYLOAD, headers={"X-Transaction-ID": "integration-1"}
        )
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertAlmostEqual(result["score"], direct["score"], places=9)
        self.assertEqual(result["predicted_fraud"], direct["predicted_fraud"])
        self.assertEqual(result["threshold"], direct["threshold"])
        self.assertEqual(result["transaction_id"], "integration-1")
        self.assertEqual((result["requested_route"], result["route"]), ("champion", "champion"))
        self.assertFalse(result["fallback"])
        self.assertEqual((self.champion.hits, self.challenger.hits), (1, 0))

    def test_request_metrics_count_the_final_status(self):
        self.gateway.post("/predict", json=PAYLOAD)
        self.assertEqual(self.metric("fraudshield_gateway_requests_total", status="200"), 1)
        self.assertEqual(self.metric(
            "fraudshield_gateway_scoring_results_total",
            requested_route="champion", route="champion", fallback="false",
        ), 1)
        self.assertEqual(self.metric("fraudshield_gateway_request_duration_seconds_count"), 1)

    def test_invalid_input_is_rejected_before_any_model_server_is_called(self):
        response = self.gateway.post("/predict", json={**PAYLOAD, "amount": -1})
        self.assertEqual(response.status_code, 422)
        self.assertEqual((self.champion.hits, self.challenger.hits), (0, 0))
        self.assertEqual(self.metric("fraudshield_gateway_requests_total", status="422"), 1)

    def test_healthy_challenger_serves_all_traffic_at_weight_100(self):
        self.set_weight(100)
        result = self.gateway.post("/predict", json=PAYLOAD).json()
        self.assertEqual((result["requested_route"], result["route"]),
                         ("challenger", "challenger"))
        self.assertEqual(result["score"], CHALLENGER_SCORE)
        self.assertEqual((self.champion.hits, self.challenger.hits), (0, 1))

    def test_failing_challenger_falls_back_once_and_is_removed_automatically(self):
        self.set_weight(100)
        self.challenger.mode = "fail"
        result = self.gateway.post("/predict", json=PAYLOAD).json()
        self.assertEqual(result["requested_route"], "challenger")
        self.assertEqual(result["route"], "champion")
        self.assertTrue(result["fallback"])
        self.assertTrue(result["rollback_triggered"])
        self.assertEqual((self.challenger.hits, self.champion.hits), (1, 1))

        routing = self.gateway.get("/routing").json()
        self.assertEqual(routing["canary_weight"], 0)
        self.assertEqual(routing["fallback_attempts"], 1)
        self.assertEqual(routing["automatic_rollbacks"], 1)
        self.assertEqual(self.metric("fraudshield_gateway_canary_weight_percent"), 0)
        self.assertEqual(self.metric("fraudshield_gateway_fallback_attempts_total"), 1)
        self.assertEqual(self.metric("fraudshield_gateway_automatic_removals_total"), 1)
        self.assertEqual(self.metric(
            "fraudshield_gateway_scoring_results_total",
            requested_route="challenger", route="champion", fallback="true",
        ), 1)

        follow_up = self.gateway.post("/predict", json=PAYLOAD).json()
        self.assertEqual(follow_up["requested_route"], "champion")
        self.assertFalse(follow_up["fallback"])
        self.assertEqual(self.challenger.hits, 1)

    def test_challenger_with_the_wrong_model_identity_is_treated_as_a_failure(self):
        self.set_weight(100)
        self.challenger.mode = "wrong_identity"
        result = self.gateway.post("/predict", json=PAYLOAD).json()
        self.assertEqual(result["route"], "champion")
        self.assertTrue(result["fallback"])

    def test_champion_failure_is_a_503_and_is_counted(self):
        self.champion.mode = "fail"
        response = self.gateway.post("/predict", json=PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json(), {"detail": "Scoring service is temporarily unavailable."}
        )
        self.assertEqual(self.metric("fraudshield_gateway_requests_total", status="503"), 1)

    def test_champion_failure_during_fallback_is_not_retried(self):
        self.set_weight(100)
        self.challenger.mode = "fail"
        self.champion.mode = "fail"
        response = self.gateway.post("/predict", json=PAYLOAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual((self.challenger.hits, self.champion.hits), (1, 1))

    def test_admin_change_needs_the_key_and_leaves_routing_unchanged_without_it(self):
        for headers in ({}, {"X-API-Key": "wrong"}):
            response = self.gateway.post("/admin/canary", json={"weight": 50}, headers=headers)
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.gateway.get("/routing").json()["canary_weight"], 0)


if __name__ == "__main__":
    unittest.main()
