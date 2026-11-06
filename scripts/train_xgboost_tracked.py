"""Train a separate XGBoost baseline inside an active MLflow run."""

import train_xgboost as job
from train_logreg_dvc import PREPARED, SPLITS, load_parquet_split

import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import mlflow
from mlflow.tracking import MlflowClient


ROOT = job.ROOT
RECEIPT = ROOT / "reports/mlflow_tracked_xgboost.json"
SOURCES = [
    "scripts/train_xgboost_tracked.py",
    "scripts/check_model_gate.py",
    "scripts/run_retraining_worker.py",
    "scripts/enqueue_drift_requests.py",
    "backend/retraining_worker.py",
    "backend/retraining_queue.py",
    "scripts/train_xgboost.py",
    "scripts/train_logreg.py",
    "scripts/train_logreg_dvc.py",
    "src/fraudshield/features.py",
    "requirements-ml.lock.txt",
    "requirements-spark.txt",
    "dvc.yaml",
    "dvc.lock",
]


def git(*args):
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True
    ).strip()


def source_hashes():
    return {name: job.sha256(ROOT / name) for name in SOURCES}


def main(*, receipt_path=RECEIPT,
         trigger="manual_reproducibility_check", request_id=None):
    receipt_path = Path(receipt_path)
    if trigger not in ("manual_reproducibility_check", "controlled_drift"):
        raise ValueError("Unsupported training trigger.")
    if trigger == "controlled_drift" and not request_id:
        raise ValueError("Controlled retraining requires a request ID.")
    if receipt_path.exists():
        raise SystemExit("STOP: tracked-training receipt already exists.")

    # Require the training sources to be committed and unchanged.
    git("ls-files", "--error-unmatch", *SOURCES)
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", *SOURCES],
        cwd=ROOT, check=True,
    )
    commit = git("rev-parse", "HEAD")
    hashes = source_hashes()

    prepared_path = PREPARED / "spark_preparation.json"
    prepared_hash = job.sha256(prepared_path)
    prepared = json.loads(prepared_path.read_text())
    if (
        prepared["status"] != "PASS"
        or prepared["feature_names"] != list(job.FEATURE_NAMES)
    ):
        raise ValueError("Preparation status or feature order differs.")

    manifest = {"source_sha256": prepared["source_sha256"], "splits": {}}
    for name in SPLITS:
        files = {
            path: value
            for path, value in prepared["parquet_sha256"].items()
            if path.startswith(f"split={name}/")
        }
        if not files:
            raise ValueError(f"Missing Parquet checksum list: {name}")
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True).encode()
        ).hexdigest()
        manifest["splits"][name] = {
            **prepared["splits"][name],
            "files": files,
            "sha256": fingerprint,
        }

    database = ROOT / "runtime/mlflow/tracking.db"
    if not database.is_file():
        raise SystemExit("STOP: expected MLflow database is missing.")
    uri = f"sqlite:///{database}"
    mlflow.set_tracking_uri(uri)
    client = MlflowClient(tracking_uri=uri)
    experiment = client.get_experiment_by_name("FraudShield-Models")
    if experiment is None:
        raise SystemExit("STOP: expected experiment is missing.")

    with mlflow.start_run(
        experiment_id=experiment.experiment_id,
        run_name="xgboost-baseline-tracked-training",
        tags={
            "run_type": "training",
            "git_commit": commit,
            "training_code_commit": commit,
            "training_sources_clean": "true",
            "git_dirty": str(bool(git("status", "--porcelain"))).lower(),
            "evaluation_split": "validation",
            "test_evaluated": "false",
            "trigger": trigger,
            "retraining_request_id": request_id or "none",
        },
    ) as run:
        run_id = run.info.run_id
        job.OUTPUT = ROOT / "artifacts/xgboost_tracked" / run_id
        job.REPORT = job.OUTPUT / "validation.json"

        mlflow.log_dict({
            "git_commit": commit,
            "source_sha256": manifest["source_sha256"],
            "prepared_report_sha256": prepared_hash,
            "source_file_sha256": hashes,
            "splits": manifest["splits"],
        }, "provenance/training_inputs.json")
        mlflow.log_dict({
            package: version(package)
            for package in (
                "mlflow", "xgboost-cpu", "scikit-learn",
                "numpy", "pandas", "pyarrow", "joblib",
            )
        }, "provenance/runtime_versions.json")
        for name in SOURCES:
            mlflow.log_artifact(
                str(ROOT / name),
                (Path("provenance/code") / Path(name).parent).as_posix(),
            )

        with job.threadpool_limits(limits=2):
            job.main(manifest=manifest, split_loader=load_parquet_split)

        if (
            source_hashes() != hashes
            or job.sha256(prepared_path) != prepared_hash
        ):
            raise ValueError("Training sources or preparation report changed.")

        report = json.loads(job.REPORT.read_text())
        model_path = job.OUTPUT / "model.joblib"
        model_hash = job.sha256(model_path)
        if model_hash != report["model_sha256"]:
            raise ValueError("Saved model and report checksum differ.")

        selected = report["at_selected_threshold"]
        metrics = {
            "validation_average_precision": report["average_precision"],
            "validation_precision": selected["precision"],
            "validation_recall": selected["recall"],
            "validation_f1": selected["f1"],
            "fit_seconds": report["fit_seconds"],
            "best_iteration_zero_based": report["best_iteration_zero_based"],
        }
        mlflow.log_params({
            **{f"training_{key}": str(value)
               for key, value in report["training_parameters"].items()},
            "selected_threshold": selected["threshold"],
            "train_rows": report["train_rows"],
            "validation_rows": report["validation_rows"],
        })
        mlflow.log_metrics(metrics)
        mlflow.set_tag("model_sha256", model_hash)
        mlflow.log_artifact(str(job.REPORT), "reports")
        mlflow.log_artifact(str(model_path), "model_bundle")
        mlflow.log_artifact(str(job.OUTPUT / "model.ubj"), "model_bundle")

        stored = client.get_run(run_id)
        for key, expected in metrics.items():
            if not job.np.isclose(
                stored.data.metrics[key], expected, rtol=0, atol=1e-12
            ):
                raise ValueError(f"Metric readback failed: {key}")
        with TemporaryDirectory() as destination:
            downloaded = client.download_artifacts(
                run_id, "model_bundle/model.joblib", dst_path=destination
            )
            if job.sha256(Path(downloaded)) != model_hash:
                raise ValueError("Downloaded model checksum differs.")

    if client.get_run(run_id).info.status != "FINISHED":
        raise ValueError("MLflow run did not finish successfully.")

    receipt = {
        "run_id": run_id,
        "run_type": "training",
        "trigger": trigger,
        "retraining_request_id": request_id,
        "training_code_commit": commit,
        "model_path": model_path.relative_to(ROOT).as_posix(),
        "model_sha256": model_hash,
        "validation_report": job.REPORT.relative_to(ROOT).as_posix(),
        "validation_average_precision": report["average_precision"],
        "metric_readback": "PASS",
        "artifact_checksum": "PASS",
        "test_evaluated": False,
        "registered_or_promoted": False,
    }
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    with receipt_path.open("x") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=RECEIPT)
    parser.add_argument("--trigger", choices=(
        "manual_reproducibility_check", "controlled_drift",
    ), default="manual_reproducibility_check")
    parser.add_argument("--request-id")
    args = parser.parse_args()
    main(receipt_path=args.receipt, trigger=args.trigger,
         request_id=args.request_id)
