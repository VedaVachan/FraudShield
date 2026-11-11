"""Save a synthetic demo explanation and benchmark the explanation path."""

import os

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "2"

import hashlib  # noqa: E402 - thread limits set first
import json  # noqa: E402 - thread limits set first
import platform  # noqa: E402 - thread limits set first
from pathlib import Path  # noqa: E402 - thread limits set first

import joblib  # noqa: E402 - thread limits set first
import numpy as np  # noqa: E402 - thread limits set first
import pandas as pd  # noqa: E402 - thread limits set first
from threadpoolctl import threadpool_limits  # noqa: E402 - thread limits set first

from fraudshield.explanations import FraudExplainer  # noqa: E402 - thread limits set first
from fraudshield.features import INPUT_COLUMNS  # noqa: E402 - thread limits set first
from benchmark_logreg import measure, WARMUP, REPEATS  # noqa: E402 - thread limits set first

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/shap_benchmark.json"


def main():
    if REPORT.exists():
        raise SystemExit("STOP: SHAP benchmark report already exists.")

    model_path = ROOT / "artifacts/xgboost_v1/model.joblib"
    validation_report = json.loads(
        (ROOT / "reports/xgboost_validation.json").read_text()
    )
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if digest != validation_report["model_sha256"]:
        raise ValueError("Model checksum mismatch.")

    bundle = joblib.load(model_path)
    explainer = FraudExplainer(bundle["pipeline"])

    # Invented input, not an actual dataset record; no ground-truth label.
    demo = {
        "type": "TRANSFER",
        "amount": 100000.0,
        "oldbalanceOrg": 100000.0,
    }
    explanation = explainer.explain(
        pd.DataFrame([demo], columns=list(INPUT_COLUMNS))
    )
    score = float(explanation["scores"][0])
    contributions = [
        {"feature": name, "shap_log_odds": float(value)}
        for name, value in zip(
            explanation["feature_names"],
            explanation["contributions"][0],
        )
    ]
    contributions.sort(key=lambda item: abs(item["shap_log_odds"]), reverse=True)

    # Same timing sample as the prediction benchmarks.
    frame = pd.read_csv(
        ROOT / "data/processed/paysim_v1/validation.csv",
        usecols=list(INPUT_COLUMNS),
        nrows=1000,
    ).loc[:, list(INPUT_COLUMNS)]
    if len(frame) != 1000:
        raise ValueError("Expected 1000 benchmark inputs.")
    records = frame.to_dict(orient="records")

    def explain_one(index):
        one = pd.DataFrame(
            [records[index % len(records)]],
            columns=list(INPUT_COLUMNS),
        )
        return explainer.explain(one)

    latency = measure(explain_one)

    result = {
        "model_sha256": digest,
        "trees_explained": explainer.tree_count,
        "method": "XGBoost native exact SHAP contributions",
        "contribution_units": "raw model log-odds",
        "demo": {
            "description": "invented transaction; ground truth unknown",
            "input": demo,
            "score": score,
            "threshold": bundle["threshold"],
            "predicted_fraud": bool(score >= bundle["threshold"]),
            "base_value": float(explanation["base_values"][0]),
            "raw_margin": float(explanation["raw_margins"][0]),
            "contributions": contributions,
        },
        "benchmark": {
            "batch_size": 1,
            "concurrency": 1,
            "numerical_thread_limit": 2,
            "warmup_calls": WARMUP,
            "measured_calls": REPEATS,
            "sample": "first 1000 validation rows",
            "full_explanation_latency": latency,
            "includes": [
                "DataFrame creation",
                "feature construction",
                "DMatrix construction",
                "SHAP and raw-margin computation",
                "pipeline scoring",
                "additivity and score-agreement checks",
            ],
            "excludes": [
                "model loading",
                "explainer initialization",
                "HTTP handling",
                "JSON serialization",
                "concurrent traffic",
            ],
        },
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "checks": "PASS",
    }

    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    print(json.dumps(result, indent=2))
    print("PASS: demo explanation and warmed latency benchmark saved.")


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
