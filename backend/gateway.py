"""Separate local gateway for testing champion/challenger HTTP routing."""

from secrets import compare_digest
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.app import Transaction
from backend.model_client import ModelServerError
from backend.gateway_metrics import (
    GatewayMetrics, GatewayMetricsMiddleware,
)


class CanaryWeight(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    weight: int = Field(ge=0, le=100)


def create_app(routing, admin_key):
    """Build a gateway around one process-local router.

    Model identity is not inferred from the configured endpoint address.
    Transaction IDs select a route; they are not sent to the model.
    """
    if not isinstance(admin_key, str) or len(admin_key) < 32:
        raise ValueError("Gateway admin key must contain at least 32 characters.")

    app = FastAPI(title="FraudShield Canary Gateway", version="0.1.0")
    app.state.routing = routing
    metrics = GatewayMetrics(routing)
    app.state.gateway_metrics = metrics
    app.add_middleware(GatewayMetricsMiddleware, metrics=metrics)

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics():
        return metrics.response()

    @app.get("/health")
    def health():
        return {
            "status": "configured",
            "scope": "gateway initialized; does not verify upstream health",
        }

    @app.get("/routing")
    def routing_status():
        return routing.snapshot()

    @app.post("/predict")
    def predict(
        transaction: Transaction,
        transaction_id: str | None = Header(
            default=None, alias="X-Transaction-ID",
            min_length=1, max_length=128,
        ),
    ):
        identifier = transaction_id if transaction_id is not None else str(uuid4())
        if not identifier.strip():
            raise HTTPException(
                status_code=422, detail="Invalid transaction identifier."
            )
        try:
            result = routing.predict(identifier, transaction.model_dump())
        except ModelServerError as exc:
            raise HTTPException(
                status_code=503,
                detail="Scoring service is temporarily unavailable.",
            ) from exc

        metrics.observe_result(result)
        return {
            **result["prediction"],
            "transaction_id": identifier,
            "requested_route": result["requested_route"],
            "route": result["route"],
            "fallback": result["fallback"],
            "rollback_triggered": result["rollback_triggered"],
        }

    @app.post("/admin/canary")
    def set_canary(
        change: CanaryWeight,
        supplied_key: str | None = Header(default=None, alias="X-API-Key"),
    ):
        if supplied_key is None or not compare_digest(
            supplied_key.encode("utf-8"), admin_key.encode("utf-8")
        ):
            raise HTTPException(status_code=401, detail="Invalid admin credentials.")
        try:
            routing.set_weight(change.weight)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="Requested canary configuration is unavailable.",
            ) from exc
        return routing.snapshot()

    return app
