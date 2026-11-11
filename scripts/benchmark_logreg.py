"""Measure warmed single-transaction latency without reading test data."""

import os

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "2"

import hashlib  # noqa: E402 - thread limits set first
import json  # noqa: E402 - thread limits set first
import platform  # noqa: E402 - thread limits set first
import time  # noqa: E402 - thread limits set first
from pathlib import Path  # noqa: E402 - thread limits set first

import joblib  # noqa: E402 - thread limits set first
import numpy as np  # noqa: E402 - thread limits set first
import pandas as pd  # noqa: E402 - thread limits set first
from threadpoolctl import threadpool_limits  # noqa: E402 - thread limits set first

from fraudshield.features import INPUT_COLUMNS  # noqa: E402 - thread limits set first

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "artifacts/logreg_v1/model.joblib"
REPORT = ROOT / "reports/logreg_latency.json"
VALIDATION = ROOT / "reports/logreg_validation.json"
WARMUP = 100
REPEATS = 1000


def summarize(values):
    values = np.asarray(values, dtype=np.float64)
    return {
        "median_ms": float(np.median(values)),
        "p95_ms": float(np.percentile(values, 95)),
        "p99_ms": float(np.percentile(values, 99)),
        "mean_ms": float(np.mean(values)),
        "max_ms": float(np.max(values)),
    }


def measure(function):
    for index in range(WARMUP):
        function(index)

    durations = []
    for index in range(REPEATS):
        start = time.perf_counter_ns()
        function(index)
        durations.append((time.perf_counter_ns() - start) / 1_000_000)

    return summarize(durations)


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

    # Small validation sample; no test data and no transaction logging.
    frame = pd.read_csv(
        ROOT / "data/processed/paysim_v1/validation.csv",
        usecols=list(INPUT_COLUMNS),
        nrows=1000,
    ).loc[:, list(INPUT_COLUMNS)]
    if frame.empty:
        raise ValueError("Benchmark input is empty.")

    records = frame.to_dict(orient="records")
    transformed = pipeline.named_steps["features"].transform(frame)
    scaled = pipeline.named_steps["scaler"].transform(transformed)
    classifier = pipeline.named_steps["classifier"]

    def classifier_only(index):
        position = index % len(records)
        return classifier.predict_proba(scaled[position:position + 1])[0, 1]

    def full_prediction(index):
        record = records[index % len(records)]
        one = pd.DataFrame([record], columns=list(INPUT_COLUMNS))
        score = float(pipeline.predict_proba(one)[0, 1])
        return score, int(score >= threshold)

    for index in range(20):
        score, decision = full_prediction(index)
        np.testing.assert_allclose(
            score, classifier_only(index), rtol=1e-10, atol=1e-12
        )
        assert decision == int(score >= threshold)

    result = {
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
    print("PASS: benchmark completed and report saved.")


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
