"""Local FraudShield prediction and explanation API."""

import os

for variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"
):
    os.environ[variable] = "2"

import hashlib  # noqa: E402 - thread limits set first
import json  # noqa: E402 - thread limits set first
import logging  # noqa: E402 - thread limits set first
from contextlib import asynccontextmanager  # noqa: E402 - thread limits set first
from pathlib import Path  # noqa: E402 - thread limits set first
from typing import Literal  # noqa: E402 - thread limits set first

import joblib  # noqa: E402 - thread limits set first
import numpy as np  # noqa: E402 - thread limits set first
import pandas as pd  # noqa: E402 - thread limits set first
from fastapi import FastAPI, Request  # noqa: E402 - thread limits set first
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402 - thread limits set first
from fastapi.exceptions import RequestValidationError  # noqa: E402 - thread limits set first
from fastapi.responses import JSONResponse  # noqa: E402 - thread limits set first
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402 - thread limits set first
from threadpoolctl import threadpool_limits  # noqa: E402 - thread limits set first

from fraudshield.explanations import FraudExplainer  # noqa: E402 - thread limits set first
from fraudshield.features import INPUT_COLUMNS  # noqa: E402 - thread limits set first
from backend.drift import DriftMonitor  # noqa: E402 - thread limits set first
from backend.logging_config import configure_logging  # noqa: E402 - thread limits set first
from backend.pipeline import router as pipeline_router  # noqa: E402 - thread limits set first
from backend.metrics import configure_metrics, mark_drift_input, mark_prediction  # noqa: E402 - thread limits set first

ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


class Transaction(BaseModel):
    """Request body for scoring: type, amount and sender balance."""
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False
    )
    type: Literal["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"]
    amount: float = Field(ge=0)
    oldbalanceOrg: float = Field(ge=0)


class Prediction(BaseModel):
    """Response fields common to scoring and explanation."""
    score: float
    threshold: float
    predicted_fraud: bool
    model_name: str
    model_version: str


class Contribution(BaseModel):
    """One feature's SHAP contribution in raw model log-odds."""
    feature: str
    shap_log_odds: float


class Explanation(Prediction):
    """Prediction plus base value, raw margin and per-feature contributions."""
    base_value: float
    raw_margin: float
    contribution_units: str
    contributions: list[Contribution]


@asynccontextmanager
async def lifespan(app):
    """Verify and load the model at startup; release it at shutdown."""
    configure_logging()
    receipt = json.loads(
        (ROOT / "reports/mlflow_registry.json").read_text()
    )["models"]["xgboost"]
    path = ROOT / "artifacts/xgboost_v1/model.joblib"
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_hash != receipt["model_sha256"]:
        raise RuntimeError("Model checksum differs from registry receipt.")

    bundle = joblib.load(path)
    threshold = float(bundle["threshold"])
    if (
        not np.isfinite(threshold)
        or not 0 <= threshold <= 1
        or threshold != receipt["threshold"]
    ):
        raise RuntimeError("Model threshold is invalid or differs from receipt.")

    with threadpool_limits(limits=2):
        app.state.pipeline = bundle["pipeline"]
        app.state.explainer = FraudExplainer(bundle["pipeline"])
        app.state.threshold = threshold
        app.state.model_name = receipt["name"]
        app.state.model_version = str(receipt["version"])
        app.state.model_sha256 = actual_hash
        app.state.drift_monitor = DriftMonitor(ROOT / "reports/drift_reference.json")
        app.state.ready = True
        LOGGER.info(
            "Model loaded and API ready: name=%s version=%s sha256_prefix=%s "
            "threshold=%.6f",
            app.state.model_name, app.state.model_version,
            actual_hash[:12], threshold,
        )
        try:
            yield
        finally:
            app.state.ready = False
            app.state.pipeline = None
            app.state.explainer = None
            LOGGER.info("API stopped; model released.")


app = FastAPI(
    title="FraudShield",
    version="0.1.0",
    lifespan=lifespan,
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8080", "http://localhost:8080"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

configure_metrics(app)
app.include_router(pipeline_router)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Do not echo transaction values or arbitrary submitted field names.
    """Return a generic 422 without echoing submitted values."""
    LOGGER.info("Rejected invalid request: path=%s", request.url.path)
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid transaction input."},
    )


def frame_for(transaction):
    """Build a one-row DataFrame in the model's input column order."""
    return pd.DataFrame(
        [transaction.model_dump()], columns=list(INPUT_COLUMNS)
    )


def prediction_payload(request, score):
    """Validate the score, record the decision metric and build the response."""
    score = float(score)
    if not np.isfinite(score) or not 0 <= score <= 1:
        LOGGER.error("Model returned an invalid score; the request failed.")
        raise RuntimeError("Invalid model score.")
    state = request.app.state
    mark_prediction(request, score >= state.threshold)
    return {
        "score": score,
        "threshold": state.threshold,
        "predicted_fraud": bool(score >= state.threshold),
        "model_name": state.model_name,
        "model_version": state.model_version,
    }


@app.get("/health")
def health(request: Request):
    """Report readiness and the loaded model identity."""
    state = request.app.state
    ready = getattr(state, "ready", False)
    if not ready:
        return JSONResponse(status_code=503, content={"status": "not_ready"})
    return {
        "status": "ready",
        "model_name": state.model_name,
        "model_version": state.model_version,
        "model_sha256": state.model_sha256,
    }


@app.post("/predict", response_model=Prediction)
def predict(transaction: Transaction, request: Request):
    """Score one transaction and record its input for drift monitoring."""
    score = request.app.state.pipeline.predict_proba(
        frame_for(transaction)
    )[0, 1]
    payload = prediction_payload(request, score)
    mark_drift_input(request, transaction.type, transaction.amount, transaction.oldbalanceOrg)
    return payload


@app.post("/explain", response_model=Explanation)
def explain(transaction: Transaction, request: Request):
    """Score one transaction and return its SHAP feature contributions."""
    result = request.app.state.explainer.explain(frame_for(transaction))
    contributions = [
        {"feature": name, "shap_log_odds": float(value)}
        for name, value in zip(
            result["feature_names"], result["contributions"][0]
        )
    ]
    contributions.sort(key=lambda item: abs(item["shap_log_odds"]), reverse=True)
    return {
        **prediction_payload(request, result["scores"][0]),
        "base_value": float(result["base_values"][0]),
        "raw_margin": float(result["raw_margins"][0]),
        "contribution_units": "raw model log-odds",
        "contributions": contributions,
    }
