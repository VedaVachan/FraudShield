"""Measure bounded, closed-loop HTTP concurrency on local FraudShield."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
from pathlib import Path
import platform
import subprocess
from threading import Barrier
import time

import httpx
import pandas as pd

from benchmark_logreg import summarize
from fraudshield.features import INPUT_COLUMNS


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger("fraudshield.benchmark")


def validate(body, identity, target, endpoint, threshold):
    score = body["score"]
    actual_threshold = body["threshold"]
    for value in (score, actual_threshold):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise ValueError("Invalid score or threshold.")
    if not math.isclose(actual_threshold, threshold, rel_tol=0, abs_tol=1e-7):
        raise ValueError("Unexpected threshold.")
    if type(body["predicted_fraud"]) is not bool:
        raise ValueError("Decision must be boolean.")
    if body["predicted_fraud"] != (score >= actual_threshold):
        raise ValueError("Decision differs from threshold.")

    if target == "api":
        if (
            body["model_name"] != identity["model_name"]
            or body["model_version"] != identity["model_version"]
        ):
            raise ValueError("API model identity changed.")
        if endpoint == "/explain" and len(body["contributions"]) != 9:
            raise ValueError("Unexpected explanation size.")
    elif (
        body["requested_route"] != "champion"
        or body["route"] != "champion"
        or body["fallback"] is not False
    ):
        raise ValueError("Gateway did not use champion directly.")


def scenario(base_url, endpoint, records, identity, target,
             threshold, concurrency, requests):
    barrier = Barrier(concurrency)

    def worker(worker_id):
        successes = []
        all_durations = []
        failures = {}
        with httpx.Client(
            base_url=base_url, timeout=10, trust_env=False
        ) as client:
            try:
                for index in range(5):
                    response = client.post(
                        endpoint, json=records[(worker_id + index) % len(records)]
                    )
                    response.raise_for_status()
                    validate(
                        response.json(), identity, target, endpoint, threshold
                    )
                barrier.wait(timeout=60)
            except Exception:
                barrier.abort()
                raise

            started = time.perf_counter()
            for index in range(worker_id, requests, concurrency):
                clock = time.perf_counter_ns()
                try:
                    response = client.post(
                        endpoint, json=records[index % len(records)]
                    )
                    response.raise_for_status()
                    body = response.json()
                    elapsed = (time.perf_counter_ns() - clock) / 1_000_000
                    validate(body, identity, target, endpoint, threshold)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    elapsed = (time.perf_counter_ns() - clock) / 1_000_000
                    name = type(exc).__name__
                    if isinstance(exc, httpx.HTTPStatusError):
                        name += ":" + str(exc.response.status_code)
                    failures[name] = failures.get(name, 0) + 1
                else:
                    successes.append(elapsed)
                all_durations.append(elapsed)
            finished = time.perf_counter()

        return started, finished, successes, all_durations, failures

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(worker, number) for number in range(concurrency)
        ]
        batches = [future.result() for future in futures]

    elapsed = max(row[1] for row in batches) - min(row[0] for row in batches)
    successes = [value for row in batches for value in row[2]]
    durations = [value for row in batches for value in row[3]]
    failures = {}
    for row in batches:
        for name, count in row[4].items():
            failures[name] = failures.get(name, 0) + count

    assert len(durations) == requests
    return {
        "concurrent_clients": concurrency,
        "warmup_requests": 5 * concurrency,
        "attempted_requests": requests,
        "successful_requests": len(successes),
        "failed_requests": requests - len(successes),
        "failure_types": failures,
        "measured_wall_seconds": elapsed,
        "successful_requests_per_second": len(successes) / elapsed,
        "success_latency_ms": summarize(successes) if successes else None,
        "all_attempt_latency_ms": summarize(durations),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("api", "gateway"), required=True)
    parser.add_argument("--endpoint", choices=("predict", "explain"), default="predict")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 4, 8])
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.target == "gateway" and args.endpoint != "predict":
        parser.error("The gateway supports prediction only.")
    if (
        not args.concurrency
        or any(value < 1 or value > 8 for value in args.concurrency)
        or len(set(args.concurrency)) != len(args.concurrency)
        or not max(args.concurrency) <= args.requests <= 10000
    ):
        parser.error("Use unique concurrency levels 1–8 and up to 10000 requests.")
    if args.output.exists():
        raise SystemExit("STOP: benchmark output already exists.")

    sample_path = ROOT / "data/processed/paysim_v1/validation.csv"
    records = pd.read_csv(
        sample_path, usecols=list(INPUT_COLUMNS), nrows=1000
    ).to_dict(orient="records")
    if len(records) != 1000:
        raise ValueError("Expected 1000 validation input rows.")

    registry = json.loads(
        (ROOT / "reports/mlflow_registry.json").read_text()
    )["models"]["xgboost"]
    recovery = json.loads(
        (ROOT / "runtime/retraining/recovery_inputs.json").read_text()
    )
    reference = ROOT / "reports/drift_reference.json"
    if recovery["reference_sha256"] != hashlib.sha256(reference.read_bytes()).hexdigest():
        raise ValueError("Recovery inputs differ from the current drift reference.")

    base_url = "http://127.0.0.1:" + ("8000" if args.target == "api" else "8020")
    endpoint = "/" + args.endpoint
    result = {
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "platform": platform.platform(),
        "base_url": base_url,
        "endpoint": endpoint,
        "workload": "closed-loop clients, one transaction per request",
        "sample": "first 1000 validation input rows; no labels or test data",
        "sample_sha256": hashlib.sha256(
            json.dumps(records, sort_keys=True, allow_nan=False).encode()
        ).hexdigest(),
        "connection_reuse": True,
        "includes": "JSON encoding, local HTTP, server work and response decoding",
        "excludes": "warmup, browser rendering and remote networking",
        "scope": "observed local performance; no capacity or SLA claim",
        "scenarios": [],
        "status": "failed",
    }

    with httpx.Client(timeout=15, trust_env=False) as client:
        health = client.get("http://127.0.0.1:8000/health")
        health.raise_for_status()
        identity = health.json()
        if (
            identity["status"] != "ready"
            or identity["model_sha256"] != registry["model_sha256"]
        ):
            raise ValueError("Unexpected API model identity.")
        result["api_identity_before"] = identity

        if args.target == "gateway":
            response = client.get(base_url + "/routing")
            response.raise_for_status()
            result["routing_before"] = response.json()
            if result["routing_before"]["canary_weight"] != 0:
                raise ValueError("Gateway must have challenger weight zero.")

        try:
            for concurrency in sorted(args.concurrency):
                print(
                    f"Measuring {args.target}{endpoint}: "
                    f"{concurrency} clients, {args.requests} requests...",
                    flush=True,
                )
                measured = scenario(
                    base_url, endpoint, records, identity, args.target,
                    registry["threshold"], concurrency, args.requests,
                )
                result["scenarios"].append(measured)
                print(json.dumps(measured, indent=2), flush=True)
                if measured["failed_requests"]:
                    raise RuntimeError("Failures observed; higher concurrency skipped.")

            final = client.get("http://127.0.0.1:8000/health")
            final.raise_for_status()
            result["api_identity_after"] = final.json()
            if result["api_identity_after"] != identity:
                raise ValueError("API identity changed.")
            if args.target == "gateway":
                response = client.get(base_url + "/routing")
                response.raise_for_status()
                result["routing_after"] = response.json()
                if result["routing_after"] != result["routing_before"]:
                    raise ValueError("Gateway routing state changed.")
            result["status"] = "completed_without_request_failures"
        except Exception as exc:
            result["error"] = str(exc)[:1000]
            LOGGER.error(
                "Benchmark scenario failed: %s: %s",
                type(exc).__name__, str(exc)[:200],
            )
        finally:
            if args.target == "api" and endpoint == "/predict":
                try:
                    for row in recovery["records"]:
                        response = client.post(base_url + "/predict", json=row)
                        response.raise_for_status()
                    result["recovery_predictions_sent"] = len(recovery["records"])
                except Exception as exc:
                    result["recovery_error"] = str(exc)[:1000]
                    result["status"] = "failed"
                    LOGGER.error(
                        "Recovery predictions failed: %s: %s",
                        type(exc).__name__, str(exc)[:200],
                    )
            result["finished_utc"] = datetime.now(timezone.utc).isoformat()
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as handle:
                json.dump(result, handle, indent=2, allow_nan=False)
                handle.write("\n")

    print("Report:", args.output)
    return 0 if result["status"] == "completed_without_request_failures" else 1


if __name__ == "__main__":
    raise SystemExit(main())
