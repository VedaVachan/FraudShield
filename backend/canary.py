"""Deterministic canary routing with request-level champion fallback."""

import hashlib
from threading import Lock

from backend.model_client import ModelServerError


class CanaryRouter:
    """Route requests without holding a configuration lock during HTTP calls."""

    def __init__(self, champion, challenger=None, weight=0):
        self._champion = champion
        self._challenger = challenger
        self._lock = Lock()
        self._weight = 0
        self._fallback_attempts = 0
        self.set_weight(weight)

    def set_weight(self, weight):
        if type(weight) is not int or not 0 <= weight <= 100:
            raise ValueError("Canary weight must be an integer from 0 to 100.")
        with self._lock:
            if weight > 0 and self._challenger is None:
                raise ValueError("A challenger is required for positive weight.")
            self._weight = weight

    def snapshot(self):
        with self._lock:
            return {
                "canary_weight": self._weight,
                "challenger_configured": self._challenger is not None,
                "fallback_attempts": self._fallback_attempts,
            }

    def _choose(self, transaction_id):
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
                return "challenger", self._challenger
            return "champion", self._champion

    def predict(self, transaction_id, transaction):
        requested_route, client = self._choose(transaction_id)
        try:
            prediction = client.predict(transaction)
            actual_route = requested_route
            fallback = False
        except ModelServerError:
            if requested_route != "challenger":
                raise
            with self._lock:
                self._fallback_attempts += 1
            # If champion also fails, propagate its error. Never retry in a loop.
            prediction = self._champion.predict(transaction)
            actual_route = "champion"
            fallback = True

        return {
            "prediction": prediction,
            "requested_route": requested_route,
            "route": actual_route,
            "fallback": fallback,
        }
