"""Import existing measured results into local MLflow without retraining."""

import hashlib
import json
import math
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["MLFLOW_ENABLE_ASYNC_LOGGING"] = "false"

import mlflow
from mlflow.tracking import MlflowClient

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "runtime/mlflow"
ARTIFACTS = STORAGE / "artifacts/models"
RECEIPT = ROOT / "reports/mlflow_import.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare(name, folder):
    validation_path = ROOT / f"reports/{name}_validation.json"
    latency_path = ROOT / f"reports/{name}_latency.json"
    model_path = ROOT / f"artifacts/{folder}/model.joblib"

    validation = json.loads(validation_path.read_text())
    latency = json.loads(latency_path.read_text())
    model_hash = digest(model_path)

    if model_hash != validation["model_sha256"]:
        raise ValueError(f"{name}: model checksum mismatch.")
    if model_hash != latency["model_sha256"]:
        raise ValueError(f"{name}: latency report references another model.")
    if validation["test_evaluated"] is not False:
        raise ValueError(f"{name}: unexpected test evaluation status.")

    selected = validation["at_selected_threshold"]
    metrics = {
        "validation_average_precision": validation["average_precision"],
        "validation_pr_auc_trapezoidal": validation["pr_auc_trapezoidal"],
        "validation_precision": selected["precision"],
        "validation_recall": selected["recall"],
        "validation_f1": selected["f1"],
        "validation_fraud_rate": validation["validation_fraud_rate"],
        "validation_predicted_fraud_rate": selected["predicted_fraud_rate"],
        "fit_seconds": validation["fit_seconds"],
    }
    for path_name in ("classifier_only", "full_python_prediction"):
        for statistic in ("median_ms", "p95_ms", "p99_ms"):
            metrics[f"{path_name}_{statistic}"] = latency[path_name][statistic]

    tn, fp, fn, tp = [
        value for row in selected["confusion_matrix"] for value in row
    ]
    metrics.update({"validation_tn": tn, "validation_fp": fp,
                    "validation_fn": fn, "validation_tp": tp})

    identity = hashlib.sha256(
        (model_hash + digest(validation_path) + digest(latency_path)).encode()
    ).hexdigest()
    return {
        "name": name,
        "validation": validation,
        "validation_path": validation_path,
        "latency_path": latency_path,
        "model_path": model_path,
        "model_hash": model_hash,
        "identity": identity,
        "metrics": metrics,
    }


def main():
    if RECEIPT.exists():
        raise SystemExit("STOP: import receipt already exists.")

    # Validate both inputs before creating any model runs.
    items = [
        prepare("logreg", "logreg_v1"),
        prepare("xgboost", "xgboost_v1"),
    ]

    uri = f"sqlite:///{STORAGE / 'tracking.db'}"
    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    client = MlflowClient(tracking_uri=uri)

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    experiment_name = "FraudShield-Models"
    experiment = client.get_experiment_by_name(experiment_name)
    if experiment is None:
        experiment_id = client.create_experiment(
            experiment_name, artifact_location=ARTIFACTS.as_uri()
        )
    else:
        if experiment.artifact_location != ARTIFACTS.as_uri():
            raise ValueError("Existing experiment artifact location differs.")
        experiment_id = experiment.experiment_id

    import_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    receipt = {"experiment": experiment_name, "tracking_uri": uri, "runs": {}}

    for item in items:
        existing = client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=f"tags.import_identity = '{item['identity']}'",
        )
        if existing:
            if len(existing) != 1 or existing[0].info.status != "FINISHED":
                raise RuntimeError("Partial or duplicate import found; inspect before retrying.")
            run_id = existing[0].info.run_id
            print(f"Reusing completed import: {item['name']}", flush=True)
        else:
            validation = item["validation"]
            with mlflow.start_run(
                experiment_id=experiment_id,
                run_name=f"{item['name']}-v1-historical-import",
                tags={
                    "run_type": "historical_import",
                    "import_identity": item["identity"],
                    "model_sha256": item["model_hash"],
                    "import_code_commit": import_commit,
                    "test_evaluated": "false",
                    "mlflow.note.content": (
                        "Imported from existing verified reports. No retraining. "
                        "Run timestamps represent import time, not training time. "
                        "Metrics are validation development results."
                    ),
                },
            ) as run:
                run_id = run.info.run_id
                mlflow.log_params({
                    f"training_{key}": str(value)
                    for key, value in validation["training_parameters"].items()
                })
                mlflow.log_params({
                    "selected_threshold": validation["at_selected_threshold"]["threshold"],
                    "train_rows": validation["train_rows"],
                    "validation_rows": validation["validation_rows"],
                    "source_sha256": validation["source_sha256"],
                })
                mlflow.log_metrics(item["metrics"])
                mlflow.log_artifact(str(item["validation_path"]), "reports")
                mlflow.log_artifact(str(item["latency_path"]), "reports")
                mlflow.log_artifact(str(item["model_path"]), "model_bundle")
                mlflow.log_dict(
                    {"feature_names": validation["feature_names"],
                     "split_sha256": validation["split_sha256"]},
                    "provenance/features_and_splits.json",
                )

        stored = client.get_run(run_id)
        assert stored.info.status == "FINISHED"
        for key, expected in item["metrics"].items():
            if not math.isclose(
                stored.data.metrics[key], expected, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValueError(f"Stored metric differs: {key}")

        with TemporaryDirectory() as destination:
            downloaded = client.download_artifacts(
                run_id, "model_bundle/model.joblib", dst_path=destination
            )
            if digest(Path(downloaded)) != item["model_hash"]:
                raise ValueError("Stored model artifact checksum differs.")

        receipt["runs"][item["name"]] = {
            "run_id": run_id,
            "model_sha256": item["model_hash"],
            "metric_readback": "PASS",
            "artifact_checksum": "PASS",
        }
        print(f"PASS: {item['name']} metrics and model artifact verified.", flush=True)

    with RECEIPT.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()

