"""Integration: the real API app, its metrics, drift window and pipeline history together.

Only files are faked (a temporary history file); the model, the middleware, the
metrics registry and the routers are the real ones, called through the ASGI app.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

import backend.pipeline
from backend.app import app
from backend.metrics import REGISTRY


VALID = {"type": "TRANSFER", "amount": 100000.0, "oldbalanceOrg": 100000.0}
UNIQUE_AMOUNT = 123456.789


def counter(name, **labels):
    """Current value of a counter in the API metrics registry (0 if not yet created)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def scraped(text, name, **labels):
    """Value of one sample in a Prometheus text scrape, or None if it is absent."""
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and all(
                sample.labels.get(key) == value for key, value in labels.items()
            ):
                return sample.value
    return None


def history_record(run_id, status="success", failures=None, stage="automated_tests"):
    """A record shaped like the ones scripts/run_ci.py appends to the history file."""
    record = {
        "schema_version": 1, "run_id": run_id, "stage": stage, "status": status,
        "started_utc": "2026-11-12T00:00:00+00:00",
        "finished_utc": "2026-11-12T00:01:00+00:00",
        "duration_seconds": 60.0, "git_commit": "a" * 40, "git_dirty": False,
        "command": ["python", "-m", "unittest"], "exit_code": 0 if status == "success" else 1,
        "log_path": f"runtime/ci/{run_id}/{stage}.log",
    }
    if failures is not None:
        record["gate"] = {"status": "FAIL", "failures": failures}
    return record


class ApiMetricsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.addClassCleanup(cls.client.__exit__, None, None, None)

    def drift_samples(self):
        return scraped(self.client.get("/metrics").text, "fraudshield_drift_window_samples")

    def decisions(self, endpoint):
        return sum(
            counter("fraudshield_decisions_total", endpoint=endpoint, decision=decision)
            for decision in ("flagged", "not_flagged")
        )

    def test_predict_updates_request_decision_latency_and_drift_metrics(self):
        before = {
            "requests": counter("fraudshield_requests_total", endpoint="/predict", status="200"),
            "decisions": self.decisions("/predict"),
            "latency": counter("fraudshield_request_duration_seconds_count",
                               endpoint="/predict"),
            "drift": self.drift_samples(),
        }
        response = self.client.post("/predict", json=VALID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            counter("fraudshield_requests_total", endpoint="/predict", status="200"),
            before["requests"] + 1,
        )
        self.assertEqual(self.decisions("/predict"), before["decisions"] + 1)
        self.assertEqual(
            counter("fraudshield_request_duration_seconds_count", endpoint="/predict"),
            before["latency"] + 1,
        )
        self.assertEqual(self.drift_samples(), before["drift"] + 1)

    def test_decision_counter_matches_the_decision_in_the_response(self):
        body = self.client.post("/predict", json=VALID).json()
        label = "flagged" if body["predicted_fraud"] else "not_flagged"
        before = counter("fraudshield_decisions_total", endpoint="/predict", decision=label)
        again = self.client.post("/predict", json=VALID).json()
        self.assertEqual(again["predicted_fraud"], body["predicted_fraud"])
        self.assertEqual(
            counter("fraudshield_decisions_total", endpoint="/predict", decision=label),
            before + 1,
        )

    def test_explain_is_counted_but_does_not_enter_the_drift_window(self):
        requests = counter("fraudshield_requests_total", endpoint="/explain", status="200")
        decisions = self.decisions("/explain")
        drift = self.drift_samples()
        response = self.client.post("/explain", json=VALID)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            counter("fraudshield_requests_total", endpoint="/explain", status="200"),
            requests + 1,
        )
        self.assertEqual(self.decisions("/explain"), decisions + 1)
        self.assertEqual(self.drift_samples(), drift)

    def test_invalid_input_is_counted_as_422_and_changes_nothing_else(self):
        rejected = counter("fraudshield_requests_total", endpoint="/predict", status="422")
        decisions = self.decisions("/predict")
        drift = self.drift_samples()
        response = self.client.post("/predict", json={**VALID, "amount": -5})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "Invalid transaction input."})
        self.assertEqual(
            counter("fraudshield_requests_total", endpoint="/predict", status="422"),
            rejected + 1,
        )
        self.assertEqual(self.decisions("/predict"), decisions)
        self.assertEqual(self.drift_samples(), drift)

    def test_scrape_exposes_the_counters_and_never_transaction_values(self):
        self.client.post("/predict", json={**VALID, "amount": UNIQUE_AMOUNT})
        text = self.client.get("/metrics").text
        self.assertIsNotNone(scraped(
            text, "fraudshield_requests_total", endpoint="/predict", status="200"
        ))
        self.assertNotIn("123456", text)
        self.assertNotIn("TRANSFER", text)

    def test_reads_are_not_counted_as_scoring_requests(self):
        total = sum(
            sample.value for family in REGISTRY.collect()
            if family.name == "fraudshield_requests"
            for sample in family.samples if sample.name == "fraudshield_requests_total"
        )
        for path in ("/health", "/pipeline/runs", "/pipeline/summary", "/metrics"):
            self.client.get(path)
        after = sum(
            sample.value for family in REGISTRY.collect()
            if family.name == "fraudshield_requests"
            for sample in family.samples if sample.name == "fraudshield_requests_total"
        )
        self.assertEqual(after, total)

    def test_health_reports_the_identity_the_scores_come_from(self):
        health = self.client.get("/health").json()
        scored = self.client.post("/predict", json=VALID).json()
        self.assertEqual(health["status"], "ready")
        self.assertEqual(health["model_name"], scored["model_name"])
        self.assertEqual(health["model_version"], scored["model_version"])
        self.assertRegex(health["model_sha256"], r"^[0-9a-f]{64}$")


class ApiPipelineHistoryIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.addClassCleanup(cls.client.__exit__, None, None, None)

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = Path(folder.name) / "pipeline_runs.jsonl"
        self.summary = Path(folder.name) / "pipeline_summary.json"
        self.patchers = [
            patch.object(backend.pipeline, "HISTORY", self.history),
            patch.object(backend.pipeline, "SUMMARY", self.summary),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, records):
        self.history.write_text("".join(json.dumps(row) + "\n" for row in records))

    def test_recorded_runs_are_returned_newest_first_through_the_app(self):
        self.write([history_record("one"), history_record("two"), history_record("three")])
        body = self.client.get("/pipeline/runs", params={"limit": 2}).json()
        self.assertTrue(body["history_available"])
        self.assertEqual([run["run_id"] for run in body["runs"]], ["three", "two"])

    def test_failed_run_keeps_its_reasons_and_hides_commands_and_log_paths(self):
        self.write([history_record("bad", "failed", ["average precision below floor"],
                                   stage="xgboost_validation_gate")])
        run = self.client.get("/pipeline/runs").json()["runs"][0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["failure_reasons"], ["average precision below floor"])
        self.assertNotIn("command", run)
        self.assertNotIn("log_path", run)

    def test_damaged_lines_are_counted_not_fatal(self):
        self.history.write_text(
            json.dumps(history_record("ok")) + "\nnot json\n"
            + json.dumps({**history_record("x"), "status": "unknown"}) + "\n"
        )
        body = self.client.get("/pipeline/runs").json()
        self.assertEqual([run["run_id"] for run in body["runs"]], ["ok"])
        self.assertEqual(body["invalid_records"], 2)

    def test_missing_history_file_is_reported_as_unavailable_not_an_error(self):
        response = self.client.get("/pipeline/runs")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["history_available"])

    def test_bad_limit_uses_the_apps_generic_422(self):
        for limit in (0, 101, "x"):
            with self.subTest(limit=limit):
                response = self.client.get("/pipeline/runs", params={"limit": limit})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {"detail": "Invalid transaction input."})

    def test_summary_is_served_from_the_file_and_rejects_an_unsupported_one(self):
        self.summary.write_text(json.dumps({"schema_version": 1, "stages": []}))
        self.assertEqual(self.client.get("/pipeline/summary").json()["schema_version"], 1)
        self.summary.write_text(json.dumps({"schema_version": 2}))
        self.assertEqual(self.client.get("/pipeline/summary").status_code, 503)
        self.summary.unlink()
        self.assertEqual(self.client.get("/pipeline/summary").status_code, 503)

    def test_real_summary_file_matches_what_the_pipeline_page_reads(self):
        for patcher in self.patchers:
            patcher.stop()
        response = self.client.get("/pipeline/summary")
        self.assertEqual(response.status_code, 200)
        on_disk = json.loads(
            (Path(backend.pipeline.ROOT) / "reports/pipeline_summary.json").read_text()
        )
        self.assertEqual(response.json(), on_disk)


if __name__ == "__main__":
    unittest.main()
