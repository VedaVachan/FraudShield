"""Import the recorded Ray validation grid without rerunning trials."""

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
    source = ROOT / "reports/xgboost_ray_tune_validation.json"
    receipt_path = ROOT / "reports/mlflow_ray_import.json"
    if receipt_path.exists():
        raise SystemExit("STOP: Ray import receipt already exists.")

    report = json.loads(source.read_text())
    source_hash = digest(source)
    trials = report["trials"]
    configurations = {
        (row["max_depth"], row["min_child_weight"]) for row in trials
    }
    if (
        report["evaluation_split"] != "validation"
        or report["test_evaluated"] is not False
        or len(trials) != 4
        or configurations != {(4, 5), (6, 5), (4, 10), (6, 10)}
    ):
        raise ValueError("Unexpected validation grid.")

    for row in trials:
        ap = row["average_precision"]
        iteration = row["best_iteration_zero_based"]
        if not math.isfinite(ap) or not 0 <= ap <= 1:
            raise ValueError("Invalid average precision.")
        if type(iteration) is not int or iteration < 0:
            raise ValueError("Invalid best iteration.")

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
    receipts = []

    for row in trials:
        depth = row["max_depth"]
        weight = row["min_child_weight"]
        identity = hashlib.sha256(
            f"ray-historical:{source_hash}:{depth}:{weight}".encode()
        ).hexdigest()
        matches = client.search_runs(
            [experiment.experiment_id],
            filter_string=f"tags.import_identity = '{identity}'",
        )
        if matches:
            if len(matches) != 1 or matches[0].info.status != "FINISHED":
                raise SystemExit("STOP: partial or duplicate trial import.")
            run_id = matches[0].info.run_id
        else:
            with mlflow.start_run(
                experiment_id=experiment.experiment_id,
                run_name=f"ray-grid-depth{depth}-child{weight}-historical",
                tags={
                    "run_type": "historical_import",
                    "artifact_family": "ray_validation_grid",
                    "import_identity": identity,
                    "import_code_commit": commit,
                    "training_code_commit": "not_established",
                    "source_report_sha256": source_hash,
                    "evaluation_split": "validation",
                    "test_evaluated": "false",
                    "mlflow.note.content": (
                        "Imported trial summary; no training or tuning executed. "
                        "Timestamps describe import time. No trial model "
                        "artifact or trial-specific threshold is supplied."
                    ),
                },
            ) as run:
                run_id = run.info.run_id
                mlflow.log_params({
                    "max_depth": depth,
                    "min_child_weight": weight,
                    "source_sha256": report["source_sha256"],
                    "train_split_sha256": report["split_sha256"]["train"],
                    "validation_split_sha256": report["split_sha256"]["validation"],
                })
                mlflow.log_metrics({
                    "validation_average_precision": row["average_precision"],
                    "best_iteration_zero_based": row["best_iteration_zero_based"],
                })
                mlflow.log_artifact(str(source), "reports")

        stored = client.get_run(run_id)
        for key, expected in {
            "validation_average_precision": row["average_precision"],
            "best_iteration_zero_based": row["best_iteration_zero_based"],
        }.items():
            if not math.isclose(
                stored.data.metrics[key], expected, rel_tol=0, abs_tol=1e-12
            ):
                raise ValueError(f"Metric readback failed: {key}")
        with TemporaryDirectory() as destination:
            downloaded = client.download_artifacts(
                run_id, f"reports/{source.name}", dst_path=destination
            )
            if digest(Path(downloaded)) != source_hash:
                raise ValueError("Downloaded trial report checksum differs.")
        receipts.append({
            **row,
            "run_id": run_id,
            "import_code_commit": stored.data.tags.get("import_code_commit"),
            "metric_readback": "PASS",
            "report_checksum": "PASS",
        })

    receipt = {
        "run_type": "historical_import",
        "source_report_sha256": source_hash,
        "training_code_commit": "not_established",
        "trials": receipts,
    }
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
