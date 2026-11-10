"""Verify gateway construction from registry, key file and environment."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.gateway_server import create_gateway


CHAMPION_SHA = "a" * 64
CHALLENGER_SHA = "b" * 64
TEST_KEY = "test-only-key-" + "k" * 32
CONFIGURATION_VARIABLES = (
    "GATEWAY_REGISTRY_FILE", "GATEWAY_ADMIN_KEY_FILE", "CHAMPION_URL",
    "CHALLENGER_URL", "CHALLENGER_THRESHOLD", "CHALLENGER_MODEL_SHA256",
    "MODEL_TIMEOUT_SECONDS",
)


class GatewayServerFactoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.registry = self.folder / "registry.json"
        self.registry.write_text(json.dumps({
            "models": {"xgboost": {
                "threshold": 0.75, "model_sha256": CHAMPION_SHA,
            }}
        }))
        self.key_file = self.folder / "admin.key"
        self.key_file.write_text(TEST_KEY + "\n")

    def build(self, **overrides):
        environment = {
            "GATEWAY_REGISTRY_FILE": str(self.registry),
            "GATEWAY_ADMIN_KEY_FILE": str(self.key_file),
            "CHALLENGER_MODEL_SHA256": CHALLENGER_SHA,
        }
        environment.update(overrides)
        with patch.dict(os.environ):
            for name in CONFIGURATION_VARIABLES:
                os.environ.pop(name, None)
            os.environ.update(environment)
            return create_gateway()

    def test_registry_values_configure_champion_and_zero_weight(self):
        app = self.build(
            CHAMPION_URL="http://127.0.0.1:6001",
            CHALLENGER_URL="http://127.0.0.1:6002",
            CHALLENGER_THRESHOLD="0.5",
            MODEL_TIMEOUT_SECONDS="1.5",
        )
        routing = app.state.routing
        self.assertEqual(routing.snapshot(), {
            "canary_weight": 0,
            "challenger_configured": True,
            "fallback_attempts": 0,
            "rollback_on_failure": True,
            "automatic_rollbacks": 0,
        })
        champion, challenger = routing._champion, routing._challenger
        self.assertEqual(champion.url, "http://127.0.0.1:6001/invocations")
        self.assertEqual(champion.threshold, 0.75)
        self.assertEqual(champion.expected_sha256, CHAMPION_SHA)
        self.assertEqual(champion.timeout, 1.5)
        self.assertEqual(challenger.url, "http://127.0.0.1:6002/invocations")
        self.assertEqual(challenger.threshold, 0.5)
        self.assertEqual(challenger.expected_sha256, CHALLENGER_SHA)
        self.assertEqual(challenger.timeout, 1.5)

    def test_defaults_when_endpoints_threshold_and_timeout_are_unset(self):
        routing = self.build().state.routing
        self.assertEqual(
            routing._champion.url, "http://127.0.0.1:5001/invocations"
        )
        self.assertEqual(
            routing._challenger.url, "http://127.0.0.1:5002/invocations"
        )
        self.assertEqual(routing._challenger.threshold, 0.8567820191383362)
        self.assertEqual(routing._champion.timeout, 2.0)

    def test_admin_key_file_is_stripped_and_enforced(self):
        client = TestClient(self.build())
        self.addCleanup(client.close)
        rejected = client.post(
            "/admin/canary", json={"weight": 10},
            headers={"X-API-Key": "wrong-" + TEST_KEY},
        )
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(client.get("/routing").json()["canary_weight"], 0)

        accepted = client.post(
            "/admin/canary", json={"weight": 10},
            headers={"X-API-Key": TEST_KEY},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["canary_weight"], 10)

    def test_challenger_sha_is_required(self):
        with patch.dict(os.environ):
            for name in CONFIGURATION_VARIABLES:
                os.environ.pop(name, None)
            os.environ["GATEWAY_REGISTRY_FILE"] = str(self.registry)
            os.environ["GATEWAY_ADMIN_KEY_FILE"] = str(self.key_file)
            with self.assertRaises(KeyError):
                create_gateway()

    def test_invalid_challenger_sha_is_rejected(self):
        for value in ("short", "B" * 64, CHALLENGER_SHA[:-1]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.build(CHALLENGER_MODEL_SHA256=value)

    def test_invalid_numeric_configuration_is_rejected(self):
        cases = (
            {"MODEL_TIMEOUT_SECONDS": "0"},
            {"MODEL_TIMEOUT_SECONDS": "-1"},
            {"MODEL_TIMEOUT_SECONDS": "not-a-number"},
            {"CHALLENGER_THRESHOLD": "1.5"},
            {"CHALLENGER_THRESHOLD": "nan"},
        )
        for overrides in cases:
            with self.subTest(**overrides):
                with self.assertRaises(ValueError):
                    self.build(**overrides)

    def test_invalid_endpoint_url_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build(CHAMPION_URL="http://user:secret@127.0.0.1:5001")

    def test_short_admin_key_is_rejected(self):
        self.key_file.write_text("too-short\n")
        with self.assertRaises(ValueError):
            self.build()

    def test_missing_files_are_reported(self):
        with self.assertRaises(FileNotFoundError):
            self.build(GATEWAY_ADMIN_KEY_FILE=str(self.folder / "absent.key"))
        with self.assertRaises(FileNotFoundError):
            self.build(GATEWAY_REGISTRY_FILE=str(self.folder / "absent.json"))

    def test_registry_without_xgboost_entry_is_rejected(self):
        self.registry.write_text(json.dumps({"models": {}}))
        with self.assertRaises(KeyError):
            self.build()


if __name__ == "__main__":
    unittest.main()
