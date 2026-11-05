"""Build a historical console summary from recorded reports."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main():
    sources = {}

    def load(relative):
        content = (ROOT / relative).read_bytes()
        sources[relative] = hashlib.sha256(content).hexdigest()
        return json.loads(content)

    spark = load("reports/spark_preparation.json")
    imports = load("reports/mlflow_import.json")
    registry = load("reports/mlflow_registry.json")
    tuning = load("reports/xgboost_ray_tune_validation.json")

    if spark["status"] != "PASS":
        raise ValueError("Spark report did not pass.")
    seconds = spark["total_seconds"]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Invalid Spark duration.")
    rows = sum(item["rows"] for item in spark["splits"].values())

    models = []
    for key, item in registry["models"].items():
        original = imports["runs"][key]
        if (
            item["source_run_id"] != original["run_id"]
            or item["model_sha256"] != original["model_sha256"]
        ):
            raise ValueError(f"Inconsistent MLflow receipts: {key}")
        models.append({
            "name": item["name"],
            "version": str(item["version"]),
            "threshold": item["threshold"],
            "model_sha256": item["model_sha256"],
            "historical_import_run_id": original["run_id"],
            "packaging_run_id": item["packaging_run_id"],
            "prediction_parity": item["prediction_parity"],
        })

    result = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "historical report snapshot; not live MLflow queries",
        "spark": {
            "scope": "original complete preparation job, not DVC rerun",
            "rows": rows,
            "total_seconds": seconds,
            "effective_rows_per_second": rows / seconds,
            "timing_scope": (
                "whole-job elapsed time including validation and readback; "
                "not individual-stage throughput"
            ),
            "output": spark["output"],
            "spark_version": spark["spark_version"],
        },
        "registered_original_models": models,
        "ray_tuning": {
            "evaluation_split": tuning["evaluation_split"],
            "test_evaluated": tuning["test_evaluated"],
            "baseline_average_precision": tuning["baseline_average_precision"],
            "best_config": tuning["best_config"],
            "trials": tuning["trials"],
        },
        "source_report_sha256": sources,
    }
    destination = ROOT / "reports/pipeline_summary.json"
    with destination.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"PASS: summary created from {len(sources)} reports.")
    print(f"Spark: {rows:,} rows; {seconds:.2f} s; "
          f"{rows / seconds:,.1f} effective rows/s.")


if __name__ == "__main__":
    main()
