"""Check model-client transport and response validation."""

import json
from unittest import TestCase, main
from unittest.mock import MagicMock
from urllib.error import URLError

from backend.model_client import ModelClient, ModelServerError


class ModelClientTests(TestCase):
    def setUp(self):
        self.client = ModelClient(
            "http://127.0.0.1:5001", expected_threshold=0.8
        )
        self.client.opener = MagicMock()
        self.transaction = {
            "type": "TRANSFER", "amount": 100.0, "oldbalanceOrg": 100.0
        }

    def respond(self, payload, status=200):
        response = MagicMock()
        response.status = status
        response.read.return_value = json.dumps(payload).encode()
        self.client.opener.open.return_value.__enter__.return_value = response
        return response

    def prediction(self, **changes):
        row = {
            "score": 0.9, "threshold": 0.8, "predicted_fraud": True
        }
        row.update(changes)
        return {"predictions": [row]}

    def test_valid_prediction_and_request(self):
        self.respond(self.prediction())
        result = self.client.predict(self.transaction)
        self.assertEqual(result["score"], 0.9)
        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            json.loads(request.data),
            {"dataframe_records": [self.transaction]},
        )
        self.assertEqual(
            self.client.opener.open.call_args.kwargs["timeout"], 3.0
        )

    def test_unavailable_service(self):
        self.client.opener.open.side_effect = URLError("connection refused")
        with self.assertRaises(ModelServerError):
            self.client.predict(self.transaction)

    def test_timeout(self):
        self.client.opener.open.side_effect = TimeoutError("timed out")
        with self.assertRaises(ModelServerError):
            self.client.predict(self.transaction)

    def test_rejects_invalid_outputs(self):
        cases = [
            {"score": float("nan")},
            {"score": 1.1},
            {"score": True},
            {"threshold": 0.7},
            {"predicted_fraud": False},
            {"predicted_fraud": 1},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.respond(self.prediction(**changes))
                with self.assertRaises(ModelServerError):
                    self.client.predict(self.transaction)

    def test_rejects_wrong_response_shape(self):
        for payload in ({}, {"predictions": []}, {"predictions": [{}, {}]}):
            with self.subTest(payload=payload):
                self.respond(payload)
                with self.assertRaises(ModelServerError):
                    self.client.predict(self.transaction)

    def test_rejects_invalid_json(self):
        response = self.respond({})
        response.read.return_value = b"not-json"
        with self.assertRaises(ModelServerError):
            self.client.predict(self.transaction)

    def test_rejects_oversized_response(self):
        response = self.respond({})
        response.read.return_value = b"x" * 65537
        with self.assertRaises(ModelServerError):
            self.client.predict(self.transaction)
        response.read.assert_called_once_with(65537)

    def test_rejects_non_200_response(self):
        self.respond(self.prediction(), status=503)
        with self.assertRaises(ModelServerError):
            self.client.predict(self.transaction)

    def test_rejects_invalid_configuration(self):
        for address in (
            "file:///tmp/model", "http://user:secret@localhost:5001",
            "http://localhost:5001/path",
        ):
            with self.subTest(address=address):
                with self.assertRaises(ValueError):
                    ModelClient(address, expected_threshold=0.8)


if __name__ == "__main__":
    main()
