"""Local FraudShield prediction and explanation API."""

import os

for variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"
):
    os.environ[variable] = "2"

import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import joblib
import numpy as np
import pandas as pd
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from threadpoolctl import threadpool_limits

from fraudshield.explanations import FraudExplainer
from fraudshield.features import INPUT_COLUMNS

ROOT = Path(__file__).resolve().parents[1]


class Transaction(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, allow_inf_nan=False
    )
    type: Literal["CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"]
    amount: float = Field(ge=0)
    oldbalanceOrg: float = Field(ge=0)


class Prediction(BaseModel):
    score: float
    threshold: float
    predicted_fraud: bool
    model_name: str
    model_version: str


class Contribution(BaseModel):
    feature: str
    shap_log_odds: float


class Explanation(Prediction):
    base_value: float
    raw_margin: float
    contribution_units: str
    contributions: list[Contribution]


@asynccontextmanager
async def lifespan(app):
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
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            app.state.pipeline = None
            app.state.explainer = None


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


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Do not echo transaction values or arbitrary submitted field names.
    return JSONResponse(
        status_code=422,
        content={"detail": "Invalid transaction input."},
    )


def frame_for(transaction):
    return pd.DataFrame(
        [transaction.model_dump()], columns=list(INPUT_COLUMNS)
    )


def prediction_payload(request, score):
    score = float(score)
    if not np.isfinite(score) or not 0 <= score <= 1:
        raise RuntimeError("Invalid model score.")
    state = request.app.state
    return {
        "score": score,
        "threshold": state.threshold,
        "predicted_fraud": bool(score >= state.threshold),
        "model_name": state.model_name,
        "model_version": state.model_version,
    }


@app.get("/health")
def health(request: Request):
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
    score = request.app.state.pipeline.predict_proba(
        frame_for(transaction)
    )[0, 1]
    return prediction_payload(request, score)


@app.post("/explain", response_model=Explanation)
def explain(transaction: Transaction, request: Request):
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
