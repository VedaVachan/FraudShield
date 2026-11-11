"""Verify project logging configuration and the log events it carries."""

import fcntl
import io
import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from backend import logging_config
from backend.alert_receiver import create_app as create_receiver
from backend.app import app as direct_app
from backend.canary import CanaryRouter
from backend.gateway import create_app as create_gateway_app
from backend.gateway_server import create_gateway
from backend.logging_config import (
    LOG_LEVEL_VARIABLE, PROJECT_LOGGERS, configure_logging, resolve_level,
)
from backend.model_client import ModelServerError
from backend.retraining_queue import enqueue_notification
from backend.retraining_worker import execute_request


TRANSACTION = {"type": "TRANSFER", "amount": 123456.789, "oldbalanceOrg": 5555.5}


def reset_project_logging():
    """Remove handlers added by configure_logging and restore defaults."""
    for name in PROJECT_LOGGERS:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if getattr(handler, logging_config._HANDLER_MARK, False):
                logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True


class LoggingConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(reset_project_logging)
        reset_project_logging()

    def marked_handlers(self, name):
        return [
            handler for handler in logging.getLogger(name).handlers
            if getattr(handler, logging_config._HANDLER_MARK, False)
        ]

    def test_repeated_configuration_adds_one_handler_per_logger(self):
        configure_logging()
        configure_logging()
        configure_logging("WARNING")
        for name in PROJECT_LOGGERS:
            self.assertEqual(len(self.marked_handlers(name)), 1, name)
        self.assertEqual(
            logging.getLogger("backend").level, logging.WARNING
        )

    def test_level_comes_from_environment_with_safe_default(self):
        with patch.dict(os.environ):
            os.environ.pop(LOG_LEVEL_VARIABLE, None)
            self.assertEqual(resolve_level(), logging.INFO)
            os.environ[LOG_LEVEL_VARIABLE] = "debug"
            self.assertEqual(resolve_level(), logging.DEBUG)
            os.environ[LOG_LEVEL_VARIABLE] = "not-a-level"
            self.assertEqual(resolve_level(), logging.INFO)
            os.environ[LOG_LEVEL_VARIABLE] = ""
            self.assertEqual(resolve_level(), logging.INFO)
        self.assertEqual(resolve_level("error"), logging.ERROR)
        self.assertEqual(resolve_level(42), logging.INFO)

    def test_output_has_timestamp_level_and_logger_name_on_stderr(self):
        stream = io.StringIO()
        with patch("sys.stderr", stream):
            configure_logging("INFO")
        logging.getLogger("backend.probe").info("hello %s", "world")
        logging.getLogger("backend.probe").debug("hidden")
        output = stream.getvalue()
        self.assertRegex(
            output,
            r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} INFO backend\.probe: "
            r"hello world\n$",
        )

    def test_root_logger_is_left_unchanged(self):
        before = list(logging.getLogger().handlers)
        configure_logging()
        self.assertEqual(logging.getLogger().handlers, before)


class CanaryLoggingTests(unittest.TestCase):
    def router(self, rollback):
        champion = Mock()
        champion.predict.return_value = {
            "score": 0.1, "threshold": 0.9, "predicted_fraud": False,
        }
        challenger = Mock()
        challenger.predict.side_effect = ModelServerError("upstream down")
        return CanaryRouter(
            champion, challenger, weight=100, rollback_on_failure=rollback
        )

    def test_automatic_removal_is_logged_without_transaction_values(self):
        router = self.router(True)
        with self.assertLogs("backend.canary", level="WARNING") as captured:
            result = router.predict("log-example", TRANSACTION)
        self.assertTrue(result["rollback_triggered"])
        text = "\n".join(captured.output)
        self.assertIn("removed automatically", text)
        self.assertIn("trying the champion once", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("TRANSFER", text)

    def test_fallback_without_removal_does_not_claim_removal(self):
        router = self.router(False)
        with self.assertLogs("backend.canary", level="WARNING") as captured:
            result = router.predict("log-example", TRANSACTION)
        self.assertFalse(result["rollback_triggered"])
        text = "\n".join(captured.output)
        self.assertIn("trying the champion once", text)
        self.assertNotIn("removed automatically", text)


class GatewayLoggingTests(unittest.TestCase):
    def setUp(self):
        self.key = "test-only-key-" + "q" * 32
        self.routing = Mock()
        self.routing.snapshot.return_value = {"canary_weight": 10}
        self.client = TestClient(create_gateway_app(self.routing, self.key))
        self.addCleanup(self.client.close)

    def test_invalid_admin_key_is_logged_without_either_key(self):
        with self.assertLogs("backend.gateway", level="WARNING") as captured:
            response = self.client.post(
                "/admin/canary", json={"weight": 10},
                headers={"X-API-Key": "wrong-key-" + "z" * 30},
            )
        self.assertEqual(response.status_code, 401)
        text = "\n".join(captured.output)
        self.assertIn("invalid admin credentials", text)
        self.assertNotIn(self.key, text)
        self.assertNotIn("wrong-key", text)

    def test_accepted_weight_change_is_logged(self):
        with self.assertLogs("backend.gateway", level="INFO") as captured:
            response = self.client.post(
                "/admin/canary", json={"weight": 10},
                headers={"X-API-Key": self.key},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Canary weight set to 10 percent", "\n".join(captured.output))

    def test_scoring_failure_is_logged_without_request_body(self):
        self.routing.predict.side_effect = ModelServerError("down")
        with self.assertLogs("backend.gateway", level="WARNING") as captured:
            response = self.client.post("/predict", json=TRANSACTION)
        self.assertEqual(response.status_code, 503)
        text = "\n".join(captured.output)
        self.assertIn("Scoring request failed", text)
        self.assertNotIn("123456", text)


class GatewayServerLoggingTests(unittest.TestCase):
    def test_startup_logs_hash_prefixes_but_not_the_admin_key(self):
        with tempfile.TemporaryDirectory() as folder:
            registry = Path(folder) / "registry.json"
            registry.write_text(json.dumps({"models": {"xgboost": {
                "threshold": 0.75, "model_sha256": "a" * 64,
            }}}))
            key_file = Path(folder) / "admin.key"
            secret = "start-up-key-" + "s" * 32
            key_file.write_text(secret + "\n")
            environment = {
                "GATEWAY_REGISTRY_FILE": str(registry),
                "GATEWAY_ADMIN_KEY_FILE": str(key_file),
                "CHALLENGER_MODEL_SHA256": "b" * 64,
            }
            with patch.dict(os.environ, environment):
                with self.assertLogs(
                    "backend.gateway_server", level="INFO"
                ) as captured:
                    create_gateway()
        text = "\n".join(captured.output)
        self.assertIn("champion_sha256_prefix=" + "a" * 12, text)
        self.assertIn("challenger_sha256_prefix=" + "b" * 12, text)
        self.assertNotIn("a" * 13, text)
        self.assertNotIn(secret, text)


class ReceiverAndApiLoggingTests(unittest.TestCase):
    def test_receiver_logs_accepted_and_rejected_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            client = TestClient(create_receiver(Path(folder) / "history.jsonl"))
            self.addCleanup(client.close)
            valid = {
                "status": "firing",
                "alerts": [{
                    "status": "firing", "labels": {"alertname": "ProbeAlert"},
                }],
            }
            with self.assertLogs("backend.alert_receiver", level="INFO") as ok:
                self.assertEqual(client.post("/alerts", json=valid).status_code, 200)
            self.assertIn(
                "Recorded firing notification with 1 alert(s)", "\n".join(ok.output)
            )
            with self.assertLogs(
                "backend.alert_receiver", level="WARNING"
            ) as rejected:
                response = client.post("/alerts", json={"status": "bogus"})
            self.assertEqual(response.status_code, 422)
            self.assertIn("invalid notification", "\n".join(rejected.output))
            self.assertNotIn("bogus", "\n".join(rejected.output))

    def test_api_logs_rejected_input_with_path_only(self):
        client = TestClient(direct_app)
        self.addCleanup(client.close)
        bad = {"type": "SECRET-TYPE", "amount": 98765.4321, "oldbalanceOrg": 1.0}
        with self.assertLogs("backend.app", level="INFO") as captured:
            response = client.post("/predict", json=bad)
        self.assertEqual(response.status_code, 422)
        text = "\n".join(captured.output)
        self.assertIn("path=/predict", text)
        self.assertNotIn("SECRET-TYPE", text)
        self.assertNotIn("98765", text)


class WorkerLoggingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
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
        self.request_id = enqueue_notification(
            self.database, record, now=100
        )[0]["request_id"]

    @staticmethod
    def failing_runner(command, **kwargs):
        return SimpleNamespace(returncode=1)

    def test_failed_request_logs_start_failure_and_outcome(self):
        with self.assertLogs("backend.retraining_worker", level="INFO") as captured:
            result = execute_request(
                self.database, self.request_id,
                root=self.root, runner=self.failing_runner,
            )
        self.assertEqual(result["status"], "failed")
        text = "\n".join(captured.output)
        self.assertIn(f"Retraining request {self.request_id} started", text)
        self.assertRegex(
            text, rf"ERROR.*Retraining request {self.request_id} failed: "
            r"RuntimeError: Training command failed"
        )
        self.assertIn(
            f"Retraining request {self.request_id} finished: status=failed", text
        )
        error_lines = [
            line for line in captured.output if "failed: RuntimeError" in line
        ]
        self.assertTrue(all(len(line) < 400 for line in error_lines))

    def test_busy_worker_lock_is_logged_as_a_warning(self):
        lock_path = self.database.parent / "worker.lock"
        with lock_path.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertLogs(
                "backend.retraining_worker", level="WARNING"
            ) as captured:
                result = execute_request(
                    self.database, self.request_id,
                    root=self.root, runner=self.failing_runner,
                )
        self.assertEqual(result, {"status": "busy"})
        self.assertIn("Worker lock is held elsewhere", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
