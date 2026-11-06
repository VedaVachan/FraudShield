"""Synchronous MLflow client for use from synchronous API handlers."""

import json
import logging
import math
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


LOGGER = logging.getLogger(__name__)


def log_failure(url, started, exc):
    reason = getattr(exc, "reason", None)
    if isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError):
        category = "timeout"
    elif isinstance(exc, HTTPError):
        category = "upstream_http_error"
    elif isinstance(exc, URLError):
        category = "connection_error"
    elif isinstance(exc, OSError):
        category = "transport_error"
    else:
        category = "invalid_response"
    LOGGER.warning(
        "Model upstream failure host=%s category=%s exception=%s elapsed_seconds=%.3f",
        urlsplit(url).netloc, category, type(exc).__name__,
        perf_counter() - started,
    )


class ModelServerError(RuntimeError):
    """Model service is unavailable or returned an invalid prediction."""


class ModelClient:
    """Call a model endpoint and enforce its expected decision threshold.

    The threshold check is a consistency check, not proof of model identity.
    Proxy environment variables are intentionally ignored for local serving.
    """

    def __init__(self, base_url, expected_threshold, timeout=3.0):
        parts = urlsplit(base_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError("Expected an HTTP service origin without credentials.")
        if (
            isinstance(expected_threshold, bool)
            or not isinstance(expected_threshold, (int, float))
            or not math.isfinite(expected_threshold)
            or not 0 <= expected_threshold <= 1
        ):
            raise ValueError("Expected threshold must be finite and in [0, 1].")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be finite and positive.")

        self.url = base_url.rstrip("/") + "/invocations"
        self.threshold = float(expected_threshold)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}))

    def predict(self, transaction):
        request = Request(
            self.url,
            data=json.dumps(
                {"dataframe_records": [transaction]}, allow_nan=False
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = perf_counter()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ModelServerError("Model service returned a non-200 status.")
                body = response.read(65537)
            if len(body) > 65536:
                raise ModelServerError("Model response exceeded the size limit.")
            result = json.loads(body)
            rows = result["predictions"]
            if not isinstance(rows, list) or len(rows) != 1:
                raise ValueError("Expected one prediction.")
            row = rows[0]
            score = row["score"]
            threshold = row["threshold"]
            decision = row["predicted_fraud"]
            for value in (score, threshold):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not 0 <= value <= 1
                ):
                    raise ValueError("Invalid numerical output.")
            if threshold != self.threshold:
                raise ValueError("Unexpected model threshold.")
            if type(decision) is not bool or decision != (score >= threshold):
                raise ValueError("Inconsistent model decision.")
            return {
                "score": float(score),
                "threshold": float(threshold),
                "predicted_fraud": decision,
            }
        except ModelServerError as exc:
            log_failure(self.url, started, exc)
            raise
        except (URLError, OSError, ValueError, KeyError, TypeError) as exc:
            log_failure(self.url, started, exc)
            raise ModelServerError(
                "Model service is unavailable or returned an invalid response."
            ) from exc
