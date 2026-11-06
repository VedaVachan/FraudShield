"""Check worker state transitions without running model training."""

import fcntl
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from uuid import uuid4

from backend.retraining_queue import connect, enqueue_notification
from backend.retraining_worker import execute_request


class RetrainingWorkerTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / "runtime/retraining/queue.db"
        record = {
            "delivery_id": str(uuid4()),
            "received_utc": "2026-11-06T09:00:00+00:00",
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
        result = enqueue_notification(self.database, record, now=100)
        self.request_id = result[0]["request_id"]
        self.calls = []
        self.train_exit = 0
        self.gate_exit = 0
        self.bad_receipt = False
        self.bad_gate = False
        self.timeout = False

    def status(self):
        connection = connect(self.database)
        try:
            return connection.execute(
                "SELECT status FROM requests WHERE request_id = ?",
                (self.request_id,),
            ).fetchone()[0]
        finally:
            connection.close()

    def runner(self, command, **kwargs):
        self.calls.append(command)
        if command[1].endswith("train_xgboost_tracked.py"):
            if self.timeout:
                import subprocess
                raise subprocess.TimeoutExpired(command, 900)
            if self.train_exit:
                return SimpleNamespace(returncode=self.train_exit)

            run_id = "a" * 32
            directory = self.root / "artifacts/xgboost_tracked" / run_id
            directory.mkdir(parents=True)
            model = directory / "model.joblib"
            model.write_bytes(b"mock model bundle")
            report = directory / "validation.json"
            report.write_text('{"mock_validation": true}\n')
            self.model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
            self.report_hash = hashlib.sha256(report.read_bytes()).hexdigest()

            receipt = {
                "run_id": run_id,
                "retraining_request_id": (
                    "wrong-request" if self.bad_receipt else self.request_id
                ),
                "trigger": "controlled_drift",
                "test_evaluated": False,
                "registered_or_promoted": False,
                "metric_readback": "PASS",
                "artifact_checksum": "PASS",
                "model_path": model.relative_to(self.root).as_posix(),
                "validation_report": report.relative_to(self.root).as_posix(),
                "model_sha256": self.model_hash,
            }
            receipt_path = Path(command[command.index("--receipt") + 1])
            receipt_path.write_text(json.dumps(receipt))
            return SimpleNamespace(returncode=0)

        gate = {
            "status": "FAIL" if self.gate_exit else "PASS",
            "test_set_opened": False,
            "identity": {
                "model_sha256": "wrong" if self.bad_gate else self.model_hash,
                "report_sha256": self.report_hash,
            },
            "failures": ["Mock metric below floor."] if self.gate_exit else [],
        }
        json.dump(gate, kwargs["stdout"])
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=self.gate_exit)

    def execute(self):
        return execute_request(
            self.database, self.request_id,
            root=self.root, runner=self.runner,
        )

    def test_success_preserves_candidate_only_scope(self):
        result = self.execute()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.status(), "succeeded")
        self.assertIs(result["registered_or_promoted"], False)
        self.assertEqual(len(self.calls), 2)
        saved = self.root / "runtime/retraining/runs" / self.request_id / "result.json"
        self.assertEqual(json.loads(saved.read_text())["status"], "succeeded")
        self.assertFalse((self.root / "artifacts/xgboost_v1").exists())

    def test_completed_request_cannot_run_again(self):
        self.execute()
        result = self.execute()
        self.assertEqual(result["status"], "not_queued")
        self.assertEqual(len(self.calls), 2)

    def test_training_failure_skips_gate(self):
        self.train_exit = 1
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.status(), "failed")
        self.assertEqual(len(self.calls), 1)

    def test_training_timeout_is_recorded(self):
        self.timeout = True
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.status(), "failed")
        self.assertEqual(len(self.calls), 1)

    def test_gate_failure_rejects_candidate(self):
        self.gate_exit = 1
        result = self.execute()
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(self.status(), "rejected")
        self.assertIs(result["registered_or_promoted"], False)

    def test_wrong_request_receipt_skips_gate(self):
        self.bad_receipt = True
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.calls), 1)

    def test_inconsistent_gate_identity_fails(self):
        self.bad_gate = True
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.status(), "failed")

    def test_global_lock_prevents_execution(self):
        lock_path = self.database.parent / "worker.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.execute()
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self.status(), "queued")
        self.assertEqual(self.calls, [])

    def test_unknown_request_never_runs_commands(self):
        with self.assertRaises(ValueError):
            execute_request(
                self.database, str(uuid4()),
                root=self.root, runner=self.runner,
            )
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
