"""Benchmark warmed local HTTP requests, one transaction at a time."""

import json
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

from benchmark_logreg import summarize
from fraudshield.features import INPUT_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports/api_latency.json"
WARMUP = 100
REPEATS = 1000


def main():
    if REPORT.exists():
        raise SystemExit("STOP: API latency report already exists.")

    frame = pd.read_csv(
        ROOT / "data/processed/paysim_v1/validation.csv",
        usecols=list(INPUT_COLUMNS),
        nrows=REPEATS,
    )
    records = frame.to_dict(orient="records")
    if len(records) != REPEATS:
        raise ValueError("Unexpected benchmark sample size.")

    expected_hash = json.loads(
        (ROOT / "reports/xgboost_validation.json").read_text()
    )["model_sha256"]

    result = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "base_url": "http://127.0.0.1:8000",
        "concurrency": 1,
        "batch_size": 1,
        "connection_reuse": True,
        "warmup_requests_per_endpoint": WARMUP,
        "measured_requests_per_endpoint": REPEATS,
        "sample": "first 1000 validation rows",
        "platform": platform.platform(),
        "includes": "client JSON encoding, local HTTP, server work, response decoding",
        "excludes": "cold start, concurrent load, browser rendering, remote networking",
        "endpoints": {},
    }

    with httpx.Client(
        base_url=result["base_url"], timeout=30, trust_env=False
    ) as client:
        health = client.get("/health")
        health.raise_for_status()
        identity = health.json()
        if identity["status"] != "ready":
            raise ValueError("API is not ready.")
        if identity["model_sha256"] != expected_hash:
            raise ValueError("API loaded an unexpected model.")
        result["model"] = identity

        for endpoint in ("/predict", "/explain"):
            print(f"Benchmarking {endpoint}...", flush=True)
            durations = []

            for index in range(WARMUP + REPEATS):
                payload = records[index % len(records)]
                start = time.perf_counter_ns()
                response = client.post(endpoint, json=payload)
                response.raise_for_status()
                body = response.json()
                elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000

                if not 0 <= body["score"] <= 1:
                    raise ValueError("Invalid response score.")
                if body["predicted_fraud"] != (body["score"] >= body["threshold"]):
                    raise ValueError("Response decision does not match threshold.")
                if (
                    body["model_name"] != identity["model_name"]
                    or body["model_version"] != identity["model_version"]
                ):
                    raise ValueError("Model identity changed during benchmark.")
                if endpoint == "/explain" and len(body["contributions"]) != 9:
                    raise ValueError("Unexpected explanation size.")

                if index >= WARMUP:
                    durations.append(elapsed_ms)

            result["endpoints"][endpoint] = {
                **summarize(durations),
                "successful_measured_requests": len(durations),
            }

        final_health = client.get("/health")
        final_health.raise_for_status()
        if final_health.json() != identity:
            raise ValueError("API identity changed during benchmark.")

    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    print(json.dumps(result, indent=2))
    print("PASS: both HTTP benchmarks completed without request failures.")


if __name__ == "__main__":
    main()
