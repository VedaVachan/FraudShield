"""Verify local MLflow metadata and artifact storage."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ["MLFLOW_ENABLE_ASYNC_LOGGING"] = "false"

import mlflow  # noqa: E402 - MLflow environment set first
from mlflow.tracking import MlflowClient  # noqa: E402 - MLflow environment set first

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "runtime/mlflow"
DATABASE = STORAGE / "tracking.db"
ARTIFACTS = STORAGE / "artifacts/setup-check"


def main():
    STORAGE.mkdir(parents=True, exist_ok=True)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    tracking_uri = f"sqlite:///{DATABASE}"
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_registry_uri(tracking_uri)
    client = MlflowClient(tracking_uri=tracking_uri)

    name = "FraudShield-Setup-Checks"
    experiment = client.get_experiment_by_name(name)
    if experiment is None:
        experiment_id = client.create_experiment(
            name, artifact_location=ARTIFACTS.as_uri()
        )
    else:
        if experiment.artifact_location != ARTIFACTS.as_uri():
            raise ValueError("Existing experiment has a different artifact location.")
        experiment_id = experiment.experiment_id

    payload = {
        "purpose": "local tracking setup check",
        "contains_training_results": False,
    }

    with mlflow.start_run(
        experiment_id=experiment_id,
        run_name="local-storage-check",
        tags={"run_type": "setup_check"},
    ) as run:
        mlflow.log_param("storage", "local_sqlite")
        mlflow.log_metric("setup_probe", 1.0)
        mlflow.log_dict(payload, "checks/storage.json")
        run_id = run.info.run_id

    stored = client.get_run(run_id)
    assert stored.info.status == "FINISHED"
    assert stored.data.params["storage"] == "local_sqlite"
    assert stored.data.metrics["setup_probe"] == 1.0

    with TemporaryDirectory() as destination:
        path = client.download_artifacts(
            run_id, "checks/storage.json", dst_path=destination
        )
        assert json.loads(Path(path).read_text()) == payload

    print("Tracking URI:", tracking_uri)
    print("Experiment:", name)
    print("Run ID:", run_id)
    print("Artifact URI:", stored.info.artifact_uri)
    print("PASS: local metadata and artifact write/read checks.")


if __name__ == "__main__":
    main()
