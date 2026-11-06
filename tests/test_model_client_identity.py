"""Verify model identity enforcement independently of score and threshold."""
import json
import unittest
from unittest.mock import MagicMock

from backend.model_client import ModelClient, ModelServerError

DIGEST = "a" * 64


class ModelClientIdentityTests(unittest.TestCase):
    def client(self, digest=DIGEST):
        return ModelClient(
            "http://127.0.0.1:5001", expected_threshold=0.7,
            expected_sha256=digest,
        )

    def respond(self, client, identity):
        row = {"score": 0.8, "threshold": 0.7, "predicted_fraud": True}
        row.update(identity)
        response = MagicMock()
        response.status = 200
        response.read.return_value = json.dumps({"predictions": [row]}).encode()
        response.__enter__.return_value = response
        client.opener = MagicMock()
        client.opener.open.return_value = response
        return client.predict({})

    def test_matching_identity_is_returned(self):
        result = self.respond(self.client(), {"model_sha256": DIGEST})
        self.assertEqual(result["model_sha256"], DIGEST)

    def test_missing_identity_is_rejected(self):
        with self.assertRaises(ModelServerError):
            self.respond(self.client(), {})

    def test_wrong_identity_with_correct_threshold_is_rejected(self):
        with self.assertRaises(ModelServerError):
            self.respond(self.client(), {"model_sha256": "b" * 64})

    def test_malformed_response_identity_is_rejected(self):
        for value in (None, True, 123, [], {}, "invalid"):
            with self.subTest(value=value):
                with self.assertRaises(ModelServerError):
                    self.respond(self.client(), {"model_sha256": value})

    def test_invalid_expected_identity_is_rejected(self):
        for value in (True, 123, "", "a" * 63, "g" * 64):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.client(value)

    def test_legacy_mode_preserves_three_field_contract(self):
        result = self.respond(self.client(None), {})
        self.assertEqual(set(result), {"score", "threshold", "predicted_fraud"})


if __name__ == "__main__":
    unittest.main()
