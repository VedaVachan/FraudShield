"""Benchmark warmed single-transaction XGBoost predictions."""

import os

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "2"

import hashlib
import json
import platform
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from fraudshield.features import INPUT_COLUMNS
from benchmark_logreg import measure, WARMUP, REPEATS

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "artifacts/xgboost_v1/model.joblib"
REPORT = ROOT / "reports/xgboost_latency.json"
VALIDATION = ROOT / "reports/xgboost_validation.json"


def main():
    if REPORT.exists():
        raise SystemExit("STOP: latency report already exists.")

    expected = json.loads(VALIDATION.read_text())["model_sha256"]
    digest = hashlib.sha256(MODEL.read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError("Model checksum differs from training report.")

    bundle = joblib.load(MODEL)
    pipeline = bundle["pipeline"]
    threshold = bundle["threshold"]
    classifier = pipeline.named_steps["classifier"]

    frame = pd.read_csv(
        ROOT / "data/processed/paysim_v1/validation.csv",
        usecols=list(INPUT_COLUMNS),
        nrows=1000,
    ).loc[:, list(INPUT_COLUMNS)]
    if frame.empty:
        raise ValueError("Benchmark input is empty.")

    records = frame.to_dict(orient="records")
    features = pipeline.named_steps["features"].transform(frame)

    def classifier_only(index):
        position = index % len(records)
        return classifier.predict_proba(features[position:position + 1])[0, 1]

    def full_prediction(index):
        record = records[index % len(records)]
        one = pd.DataFrame([record], columns=list(INPUT_COLUMNS))
        score = float(pipeline.predict_proba(one)[0, 1])
        return score, int(score >= threshold)

    for index in range(20):
        score, decision = full_prediction(index)
        np.testing.assert_allclose(
            score, classifier_only(index), rtol=1e-6, atol=1e-7
        )
        assert decision == int(score >= threshold)

    result = {
        "model": "xgboost",
        "model_sha256": digest,
        "batch_size": 1,
        "concurrency": 1,
        "numerical_thread_limit": 2,
        "warmup_calls_per_path": WARMUP,
        "measured_calls_per_path": REPEATS,
        "input_sample": "first 1000 validation rows",
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "classifier_only": measure(classifier_only),
        "full_python_prediction": measure(full_prediction),
        "prediction_parity": "PASS",
        "excludes": [
            "model loading",
            "cold start",
            "HTTP and JSON handling",
            "concurrent traffic",
            "SHAP explanations",
        ],
    }

    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    print(json.dumps(result, indent=2))
    print("PASS: XGBoost benchmark completed and report saved.")


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
