"""Verify protocol failures use the model-service error contract."""

from http.client import BadStatusLine, IncompleteRead
import unittest
from unittest.mock import MagicMock

from backend.model_client import ModelClient, ModelServerError


class ModelClientTransportTests(unittest.TestCase):
    def make_client(self):
        return ModelClient(
            "http://127.0.0.1:5001",
            expected_threshold=0.9,
            timeout=2.0,
        )

    def test_bad_status_line_is_wrapped(self):
        client = self.make_client()
        failure = BadStatusLine("invalid HTTP status")
        client.opener = MagicMock()
        client.opener.open.side_effect = failure

        with self.assertRaises(ModelServerError) as caught:
            client.predict({"type": "TRANSFER", "amount": 1.0,
                            "oldbalanceOrg": 1.0})

        self.assertIs(caught.exception.__cause__, failure)

    def test_incomplete_body_is_wrapped_and_response_closed(self):
        client = self.make_client()
        failure = IncompleteRead(b'{"predictions":', 30)
        response = MagicMock()
        response.status = 200
        response.read.side_effect = failure
        context = MagicMock()
        context.__enter__.return_value = response
        client.opener = MagicMock()
        client.opener.open.return_value = context

        with self.assertRaises(ModelServerError) as caught:
            client.predict({"type": "TRANSFER", "amount": 1.0,
                            "oldbalanceOrg": 1.0})

        self.assertIs(caught.exception.__cause__, failure)
        context.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
