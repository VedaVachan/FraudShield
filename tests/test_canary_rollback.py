"""Check opt-in automatic traffic removal and configuration generations."""

import unittest
from unittest.mock import Mock

from backend.canary import CanaryRouter
from backend.model_client import ModelServerError


class CanaryRollbackTests(unittest.TestCase):
    def setUp(self):
        self.champion = Mock()
        self.challenger = Mock()
        self.champion.predict.return_value = {
            "score": 0.9, "threshold": 0.8, "predicted_fraud": True
        }
        self.transaction = {
            "type": "TRANSFER", "amount": 100.0, "oldbalanceOrg": 100.0
        }

    def router(self, enabled=True):
        return CanaryRouter(
            self.champion, self.challenger, weight=100,
            rollback_on_failure=enabled,
        )

    def test_failure_disables_challenger_before_next_request(self):
        self.challenger.predict.side_effect = ModelServerError("down")
        router = self.router()
        first = router.predict("first", self.transaction)
        self.assertTrue(first["fallback"])
        self.assertTrue(first["rollback_triggered"])
        self.assertEqual(router.snapshot()["canary_weight"], 0)

        second = router.predict("second", self.transaction)
        self.assertEqual(second["requested_route"], "champion")
        self.assertFalse(second["fallback"])
        self.assertFalse(second["rollback_triggered"])
        self.challenger.predict.assert_called_once()
        self.assertEqual(router.snapshot()["automatic_rollbacks"], 1)

    def test_disables_even_when_champion_fallback_fails(self):
        self.challenger.predict.side_effect = ModelServerError("down")
        self.champion.predict.side_effect = ModelServerError("also down")
        router = self.router()
        with self.assertRaises(ModelServerError):
            router.predict("example", self.transaction)
        self.assertEqual(router.snapshot()["canary_weight"], 0)
        self.assertEqual(router.snapshot()["automatic_rollbacks"], 1)

    def test_disabled_policy_preserves_weight(self):
        self.challenger.predict.side_effect = ModelServerError("down")
        router = self.router(enabled=False)
        result = router.predict("example", self.transaction)
        self.assertTrue(result["fallback"])
        self.assertFalse(result["rollback_triggered"])
        self.assertEqual(router.snapshot()["canary_weight"], 100)

    def test_success_does_not_trigger_removal(self):
        self.challenger.predict.return_value = (
            self.champion.predict.return_value
        )
        router = self.router()
        result = router.predict("example", self.transaction)
        self.assertFalse(result["rollback_triggered"])
        self.assertEqual(router.snapshot()["canary_weight"], 100)

    def test_old_failure_does_not_disable_new_configuration(self):
        router = self.router()

        def reconfigure_then_fail(transaction):
            router.set_weight(10)
            raise ModelServerError("old request failed")

        self.challenger.predict.side_effect = reconfigure_then_fail
        result = router.predict("example", self.transaction)
        self.assertTrue(result["fallback"])
        self.assertFalse(result["rollback_triggered"])
        self.assertEqual(router.snapshot()["canary_weight"], 10)
        self.assertEqual(router.snapshot()["automatic_rollbacks"], 0)

    def test_champion_failure_does_not_count_as_canary_rollback(self):
        router = self.router()
        router.set_weight(0)
        self.champion.predict.side_effect = ModelServerError("down")
        with self.assertRaises(ModelServerError):
            router.predict("example", self.transaction)
        self.assertEqual(router.snapshot()["automatic_rollbacks"], 0)
        self.challenger.predict.assert_not_called()

    def test_policy_requires_boolean(self):
        with self.assertRaises(ValueError):
            CanaryRouter(self.champion, rollback_on_failure="true")


if __name__ == "__main__":
    unittest.main()
