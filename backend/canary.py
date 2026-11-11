"""Deterministic canary routing with request-level champion fallback."""

import hashlib
import logging
from threading import Lock

from backend.model_client import ModelServerError

LOGGER = logging.getLogger(__name__)


class CanaryRouter:
    """Route requests without holding a configuration lock during HTTP calls."""

    def __init__(
        self, champion, challenger=None, weight=0,
        rollback_on_failure=False,
    ):
        """Store the clients, validate the weight and set the failure policy."""
        if type(rollback_on_failure) is not bool:
            raise ValueError('rollback_on_failure must be a boolean.')
        self._champion = champion
        self._challenger = challenger
        self._lock = Lock()
        self._weight = 0
        self._fallback_attempts = 0
        self._rollback_on_failure = rollback_on_failure
        self._automatic_rollbacks = 0
        self._generation = 0
        self.set_weight(weight)

    def set_weight(self, weight):
        """Set the challenger traffic percentage (integer 0 to 100)."""
        if type(weight) is not int or not 0 <= weight <= 100:
            raise ValueError("Canary weight must be an integer from 0 to 100.")
        with self._lock:
            if weight > 0 and self._challenger is None:
                raise ValueError("A challenger is required for positive weight.")
            self._weight = weight
            self._generation += 1

    def snapshot(self):
        """Return the current weight, counters and removal policy."""
        with self._lock:
            return {
                "canary_weight": self._weight,
                "challenger_configured": self._challenger is not None,
                "fallback_attempts": self._fallback_attempts,
                "rollback_on_failure": self._rollback_on_failure,
                "automatic_rollbacks": self._automatic_rollbacks,
            }

    def _choose(self, transaction_id):
        """Select champion or challenger deterministically from the transaction ID."""
        if (
            not isinstance(transaction_id, str)
            or not transaction_id.strip()
            or len(transaction_id) > 128
        ):
            raise ValueError("Transaction ID must contain 1 to 128 characters.")
        bucket = int.from_bytes(
            hashlib.sha256(transaction_id.encode()).digest()[:8], "big"
        ) % 10000
        with self._lock:
            if self._challenger is not None and bucket < self._weight * 100:
                return "challenger", self._challenger, self._generation
            return "champion", self._champion, self._generation

    def predict(self, transaction_id, transaction):
        """Score via the selected route, falling back once to the champion on failure."""
        requested_route, client, generation = self._choose(transaction_id)
        rollback_triggered = False
        try:
            prediction = client.predict(transaction)
            actual_route = requested_route
            fallback = False
        except ModelServerError:
            if requested_route != "challenger":
                raise
            with self._lock:
                self._fallback_attempts += 1
                # Ignore failures from an older routing configuration.
                if (
                    self._rollback_on_failure
                    and generation == self._generation
                    and self._weight > 0
                ):
                    self._weight = 0
                    self._generation += 1
                    self._automatic_rollbacks += 1
                    rollback_triggered = True
            LOGGER.warning(
                "Challenger request failed; trying the champion once."
            )
            if rollback_triggered:
                LOGGER.warning(
                    "Challenger traffic removed automatically: weight set to 0 "
                    "after an upstream failure."
                )
            # If champion also fails, propagate its error. Never retry in a loop.
            prediction = self._champion.predict(transaction)
            actual_route = "champion"
            fallback = True

        return {
            "prediction": prediction,
            "requested_route": requested_route,
            "route": actual_route,
            "fallback": fallback,
            "rollback_triggered": rollback_triggered,
        }
