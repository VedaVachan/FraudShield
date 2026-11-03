"""Aggregate metrics for a single-worker FraudShield API."""

from time import perf_counter

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.responses import Response

REGISTRY = CollectorRegistry()

REQUESTS = Counter(
    "fraudshield_requests_total",
    "POST scoring requests, including invalid inputs and server errors.",
    ["endpoint", "status"],
    registry=REGISTRY,
)

LATENCY = Histogram(
    "fraudshield_request_duration_seconds",
    "Server-side POST scoring duration; excludes client/network time.",
    ["endpoint"],
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5),
    registry=REGISTRY,
)

DECISIONS = Counter(
    "fraudshield_decisions_total",
    "Successful scoring responses by model decision; not ground truth.",
    ["endpoint", "decision"],
    registry=REGISTRY,
)

ENDPOINTS = ("/predict", "/explain")
DECISION_KEY = "_fraudshield_metric_decision"
DRIFT_INPUT_KEY = "_fraudshield_drift_input"

for endpoint in ENDPOINTS:
    LATENCY.labels(endpoint)
    for decision in ("flagged", "not_flagged"):
        DECISIONS.labels(endpoint, decision)


def mark_prediction(request, predicted_fraud):
    """Keep only the decision in request-local state, never transaction data."""
    request.scope.setdefault("state", {})[DECISION_KEY] = (
        "flagged" if predicted_fraud else "not_flagged"
    )


def mark_drift_input(request, kind, amount, balance):
    request.scope.setdefault("state", {})[DRIFT_INPUT_KEY] = (kind, amount, balance)


class ScoringMetricsMiddleware:
    """Measure scoring requests through completion of the ASGI response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        endpoint = scope.get("path")
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or endpoint not in ENDPOINTS
        ):
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        status = 500
        completed = False
        failed = False

        async def measured_send(message):
            nonlocal status, completed
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif (
                message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                completed = True

        try:
            await self.app(scope, receive, measured_send)
        except Exception:
            failed = True
            if not completed:
                status = 500
            raise
        finally:
            REQUESTS.labels(endpoint, str(status)).inc()
            LATENCY.labels(endpoint).observe(perf_counter() - started)

            state = scope.get("state", {})
            drift_input = state.pop(DRIFT_INPUT_KEY, None)
            decision = state.get(DECISION_KEY)
            if (
                completed
                and not failed
                and status == 200
                and decision in ("flagged", "not_flagged")
            ):
                DECISIONS.labels(endpoint, decision).inc()
                if endpoint == "/predict" and drift_input is not None:
                    scope['app'].state.drift_monitor.observe(*drift_input)


async def metrics_response(request):
    # A fresh registry keeps each scrape's drift snapshot consistent.
    drift_registry = CollectorRegistry()
    monitor = getattr(request.app.state, "drift_monitor", None)
    size, scores = monitor.snapshot() if monitor is not None else (0, {})
    Gauge(
        "fraudshield_drift_window_samples",
        "Successful predictions in the current drift window.",
        registry=drift_registry,
    ).set(size)
    if scores:
        drift = Gauge(
            "fraudshield_input_drift_score",
            "Distance between recent input bucket proportions and training.",
            ["feature"], registry=drift_registry,
        )
        for feature, value in scores.items():
            drift.labels(feature).set(value)
    return Response(
        content=generate_latest(REGISTRY) + generate_latest(drift_registry),
        headers={"Content-Type": CONTENT_TYPE_LATEST},
    )


def configure_metrics(app):
    app.add_middleware(ScoringMetricsMiddleware)
    app.add_route("/metrics", metrics_response, methods=["GET"])
