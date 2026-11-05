"""Import a verified DVC model result as historical MLflow evidence."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import mlflow
from mlflow.tracking import MlflowClient


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", choices=("logreg", "xgboost"))
    args = parser.parse_args()
    name = args.name
    receipt_path = ROOT / f"reports/mlflow_dvc_{name}_import.json"
    if receipt_path.exists():
        raise SystemExit(f"STOP: receipt already exists: {receipt_path}")

    folder = ROOT / f"artifacts/{name}_dvc_v1"
    model = folder / "model.joblib"
    report_path = folder / "validation.json"
    report = json.loads(report_path.read_text())
    prepared_path = ROOT / (
        "data/processed/paysim_spark_dvc_v1/spark_preparation.json"
    )
    prepared = json.loads(prepared_path.read_text())
    model_hash = digest(model)

    if (
        report["evaluation_split"] != "validation"
        or report["test_evaluated"] is not False
        or report["serialization_check"] != "PASS"
        or report["model_sha256"] != model_hash
        or prepared["status"] != "PASS"
        or report["source_sha256"] != prepared["source_sha256"]
        or report["feature_names"] != prepared["feature_names"]
    ):
        raise ValueError("Model report or preparation identity is inconsistent.")

    for split in ("train", "validation"):
        files = {
            path: value for path, value in prepared["parquet_sha256"].items()
            if path.startswith(f"split={split}/")
        }
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True).encode()
        ).hexdigest()
        if not files or fingerprint != report["split_sha256"][split]:
            raise ValueError(f"{split}: split fingerprint differs.")

    selected = report["at_selected_threshold"]
    metrics = {"validation_average_precision": report["average_precision"]}
    for key in ("precision", "recall", "f1"):
        metrics[f"validation_{key}"] = selected[key]
    for key, value in metrics.items():
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"Invalid metric: {key}")

    database = ROOT / "runtime/mlflow/tracking.db"
    if not database.is_file():
        raise SystemExit("STOP: expected MLflow database is missing.")
    uri = f"sqlite:///{database}"
    mlflow.set_tracking_uri(uri)
    client = MlflowClient(tracking_uri=uri)
    experiment = client.get_experiment_by_name("FraudShield-Models")
    if experiment is None:
        raise SystemExit("STOP: expected experiment is missing.")

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    report_hash = digest(report_path)
    identity = hashlib.sha256(
        f"dvc-historical:{name}:{model_hash}:{report_hash}".encode()
    ).hexdigest()
    matches = client.search_runs(
        [experiment.experiment_id],
        filter_string=f"tags.import_identity = '{identity}'",
    )
    if matches:
        if len(matches) != 1 or matches[0].info.status != "FINISHED":
            raise SystemExit("STOP: partial or duplicate import; inspect first.")
        run_id = matches[0].info.run_id
    else:
        with mlflow.start_run(
            experiment_id=experiment.experiment_id,
            run_name=f"{name}-dvc-v1-historical-import",
            tags={
                "run_type": "historical_import",
                "artifact_family": "dvc_baseline",
                "import_identity": identity,
                "import_code_commit": commit,
                "training_code_commit": "not_established",
                "model_sha256": model_hash,
                "source_sha256": report["source_sha256"],
                "mlflow.note.content": (
                    "Historical report import; no retraining. Run timestamps "
                    "describe import time. Attached dvc.lock is the import-time "
                    "snapshot, not proof of the original training code state."
                ),
            },
        ) as run:
            run_id = run.info.run_id
            mlflow.log_metrics(metrics)
            mlflow.log_params({
                **{f"training_{k}": str(v)
                   for k, v in report["training_parameters"].items()},
                "selected_threshold": selected["threshold"],
            })
            mlflow.log_artifact(str(report_path), "reports")
            mlflow.log_artifact(str(model), "model_bundle")
            mlflow.log_artifact(str(ROOT / "dvc.lock"), "provenance")
            mlflow.log_dict({
                "source_sha256": report["source_sha256"],
                "split_sha256": report["split_sha256"],
                "validation_report_sha256": report_hash,
            }, "provenance/data_identity.json")

    stored = client.get_run(run_id)
    for key, expected in metrics.items():
        if not math.isclose(stored.data.metrics[key], expected, abs_tol=1e-12):
            raise ValueError(f"Metric readback failed: {key}")
    with TemporaryDirectory() as destination:
        downloaded = client.download_artifacts(
            run_id, "model_bundle/model.joblib", dst_path=destination
        )
        if digest(Path(downloaded)) != model_hash:
            raise ValueError("Downloaded model checksum differs.")

    receipt = {
        "run_id": run_id,
        "run_type": "historical_import",
        "artifact_family": "dvc_baseline",
        "model_sha256": model_hash,
        "validation_report_sha256": report_hash,
        "metric_readback": "PASS",
        "artifact_checksum": "PASS",
        "training_code_commit": "not_established",
        "import_code_commit": stored.data.tags.get("import_code_commit"),
    }
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
