"""Check deterministic routing, configuration and fallback behavior."""

import unittest
from unittest.mock import Mock

from backend.canary import CanaryRouter
from backend.model_client import ModelServerError


class CanaryRouterTests(unittest.TestCase):
    def setUp(self):
        self.champion = Mock()
        self.challenger = Mock()
        prediction = {
            "score": 0.9, "threshold": 0.8, "predicted_fraud": True
        }
        self.champion.predict.return_value = prediction
        self.challenger.predict.return_value = prediction
        self.transaction = {
            "type": "TRANSFER", "amount": 100.0, "oldbalanceOrg": 100.0
        }

    def test_zero_weight_uses_champion(self):
        router = CanaryRouter(self.champion, self.challenger, weight=0)
        for index in range(100):
            result = router.predict(str(index), self.transaction)
            self.assertEqual(result["route"], "champion")
            self.assertFalse(result["fallback"])
        self.challenger.predict.assert_not_called()

    def test_full_weight_uses_challenger(self):
        router = CanaryRouter(self.champion, self.challenger, weight=100)
        for index in range(100):
            result = router.predict(str(index), self.transaction)
            self.assertEqual(result["route"], "challenger")
        self.champion.predict.assert_not_called()

    def test_ten_percent_distribution_and_repeatability(self):
        first = CanaryRouter(self.champion, self.challenger, weight=10)
        second = CanaryRouter(self.champion, self.challenger, weight=10)
        routes = []
        for index in range(10000):
            transaction_id = f"synthetic-{index}"
            route = first.predict(transaction_id, self.transaction)["route"]
            repeated = second.predict(transaction_id, self.transaction)["route"]
            self.assertEqual(route, repeated)
            routes.append(route)
        fraction = routes.count("challenger") / len(routes)
        self.assertGreaterEqual(fraction, 0.09)
        self.assertLessEqual(fraction, 0.11)

    def test_weight_change_removes_challenger_traffic(self):
        router = CanaryRouter(self.champion, self.challenger, weight=100)
        self.assertEqual(
            router.predict("same-id", self.transaction)["route"], "challenger"
        )
        router.set_weight(0)
        self.assertEqual(
            router.predict("same-id", self.transaction)["route"], "champion"
        )

    def test_challenger_failure_falls_back(self):
        self.challenger.predict.side_effect = ModelServerError("unavailable")
        router = CanaryRouter(self.champion, self.challenger, weight=100)
        result = router.predict("example", self.transaction)
        self.assertEqual(result["requested_route"], "challenger")
        self.assertEqual(result["route"], "champion")
        self.assertTrue(result["fallback"])
        self.assertEqual(router.snapshot()["fallback_attempts"], 1)
        self.champion.predict.assert_called_once_with(self.transaction)

    def test_champion_failure_propagates(self):
        self.champion.predict.side_effect = ModelServerError("unavailable")
        router = CanaryRouter(self.champion, self.challenger, weight=0)
        with self.assertRaises(ModelServerError):
            router.predict("example", self.transaction)
        self.challenger.predict.assert_not_called()
        self.assertEqual(router.snapshot()["fallback_attempts"], 0)

    def test_both_fail_without_retry_loop(self):
        self.champion.predict.side_effect = ModelServerError("champion down")
        self.challenger.predict.side_effect = ModelServerError("challenger down")
        router = CanaryRouter(self.champion, self.challenger, weight=100)
        with self.assertRaises(ModelServerError):
            router.predict("example", self.transaction)
        self.champion.predict.assert_called_once()
        self.challenger.predict.assert_called_once()

    def test_invalid_weight_preserves_configuration(self):
        router = CanaryRouter(self.champion, self.challenger, weight=10)
        for weight in (-1, 101, True, 10.5, "10"):
            with self.subTest(weight=weight):
                with self.assertRaises(ValueError):
                    router.set_weight(weight)
                self.assertEqual(router.snapshot()["canary_weight"], 10)

    def test_missing_challenger_rejects_positive_weight(self):
        router = CanaryRouter(self.champion)
        with self.assertRaises(ValueError):
            router.set_weight(10)
        self.assertEqual(router.snapshot()["canary_weight"], 0)

    def test_invalid_transaction_id(self):
        router = CanaryRouter(self.champion)
        for value in ("", " ", None, 123, "x" * 129):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    router.predict(value, self.transaction)
        self.champion.predict.assert_not_called()


if __name__ == "__main__":
    unittest.main()
