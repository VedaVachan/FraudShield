import unittest

from fastapi.testclient import TestClient
from scipy.special import expit

from backend.app import app, Transaction, frame_for


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()
        cls.payload = {
            "type": "TRANSFER",
            "amount": 100000.0,
            "oldbalanceOrg": 100000.0,
        }

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["model_name"], "FraudShield-XGBoost")

    def test_prediction_matches_original_pipeline(self):
        response = self.client.post("/predict", json=self.payload)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        expected = app.state.pipeline.predict_proba(
            frame_for(Transaction(**self.payload))
        )[0, 1]
        self.assertAlmostEqual(result["score"], float(expected), places=7)
        self.assertEqual(result["threshold"], app.state.threshold)
        self.assertEqual(
            result["predicted_fraud"],
            result["score"] >= result["threshold"],
        )

    def test_explanation_matches_prediction(self):
        prediction = self.client.post("/predict", json=self.payload).json()
        response = self.client.post("/explain", json=self.payload)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertAlmostEqual(result["score"], prediction["score"], places=7)
        self.assertEqual(result["predicted_fraud"], prediction["predicted_fraud"])
        self.assertEqual(len(result["contributions"]), 9)
        reconstructed = result["base_value"] + sum(
            item["shap_log_odds"] for item in result["contributions"]
        )
        self.assertAlmostEqual(reconstructed, result["raw_margin"], delta=1e-4)
        self.assertAlmostEqual(
            float(expit(result["raw_margin"])), result["score"], delta=1e-6
        )

    def test_rejects_invalid_inputs(self):
        invalid = [
            {**self.payload, "amount": -1},
            {**self.payload, "oldbalanceOrg": -1},
            {**self.payload, "amount": "100"},
            {**self.payload, "amount": True},
            {**self.payload, "type": "UNKNOWN"},
            {**self.payload, "isFraud": 1},
            {"type": "TRANSFER", "amount": 100},
        ]
        for endpoint in ("/predict", "/explain"):
            for payload in invalid:
                with self.subTest(endpoint=endpoint, payload=payload):
                    response = self.client.post(endpoint, json=payload)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(
                        response.json(), {"detail": "Invalid transaction input."}
                    )

    def test_rejects_nonfinite_number(self):
        response = self.client.post(
            "/predict",
            content='{"type":"TRANSFER","amount":1e999,"oldbalanceOrg":0}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 422)

    def test_zero_amount_and_balance(self):
        response = self.client.post(
            "/predict",
            json={"type": "PAYMENT", "amount": 0, "oldbalanceOrg": 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["score"], 0)
        self.assertLessEqual(response.json()["score"], 1)


if __name__ == "__main__":
    unittest.main()
