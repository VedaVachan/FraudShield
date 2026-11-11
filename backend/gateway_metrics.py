"""Prometheus metrics for the single-worker canary gateway."""

from time import perf_counter

from prometheus_client import (
    CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge,
    Histogram, generate_latest,
)
from starlette.responses import Response


class GatewayMetrics:
    """Prometheus counters and histogram for gateway scoring requests."""
    def __init__(self, routing):
        """Create the metric registry for the given router."""
        self.routing = routing
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "fraudshield_gateway_requests_total",
            "POST prediction requests, including invalid inputs and failures.",
            ["status"], registry=self.registry,
        )
        self.duration = Histogram(
            "fraudshield_gateway_request_duration_seconds",
            "Gateway prediction duration, including upstream HTTP and fallback.",
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
            registry=self.registry,
        )
        self.results = Counter(
            "fraudshield_gateway_scoring_results_total",
            "Successful scoring results by requested and actual route.",
            ["requested_route", "route", "fallback"],
            registry=self.registry,
        )

    def observe_result(self, result):
        """Count one successful scoring result by requested and actual route."""
        self.results.labels(
            result["requested_route"],
            result["route"],
            str(result["fallback"]).lower(),
        ).inc()

    def response(self):
        """Render registry metrics plus the router state as a Prometheus response."""
        state = self.routing.snapshot()
        snapshot = CollectorRegistry()
        Gauge(
            "fraudshield_gateway_canary_weight_percent",
            "Configured challenger traffic percentage.",
            registry=snapshot,
        ).set(state["canary_weight"])

        for name, field, description in (
            (
                "fraudshield_gateway_fallback_attempts_total",
                "fallback_attempts",
                "Challenger failures that initiated champion fallback.",
            ),
            (
                "fraudshield_gateway_automatic_removals_total",
                "automatic_rollbacks",
                "Automatic changes that removed challenger traffic.",
            ),
        ):
            Counter(name, description, registry=snapshot).inc(state[field])

        return Response(
            content=generate_latest(self.registry) + generate_latest(snapshot),
            headers={"Content-Type": CONTENT_TYPE_LATEST},
        )


class GatewayMetricsMiddleware:
    """ASGI middleware that measures POST /predict requests."""
    def __init__(self, app, metrics):
        """Wrap the application and store the metrics to update."""
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        """Time POST /predict requests and count them by final HTTP status."""
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != "/predict"
        ):
            await self.app(scope, receive, send)
            return

        started = perf_counter()
        status = 500
        completed = False

        async def measured_send(message):
            """Record the response status and completion while forwarding messages."""
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
            if not completed:
                status = 500
            raise
        finally:
            self.metrics.requests.labels(str(status)).inc()
            self.metrics.duration.observe(perf_counter() - started)
