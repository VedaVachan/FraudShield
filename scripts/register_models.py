"""Register existing models and verify scores and threshold decisions."""

import os

for variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"
):
    os.environ[variable] = "2"

import hashlib
import json
from pathlib import Path

import joblib
import mlflow
import mlflow.pyfunc
import numpy as np
import pandas as pd
from mlflow.models import infer_signature
from mlflow.tracking import MlflowClient
from threadpoolctl import threadpool_limits

from fraudshield.features import INPUT_COLUMNS
from fraudshield.mlflow_model import FraudShieldModel

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "reports/mlflow_registry.json"
NAMES = {
    "logreg": "FraudShield-LogisticRegression",
    "xgboost": "FraudShield-XGBoost",
}


def main():
    if RECEIPT.exists():
        raise SystemExit("STOP: registry receipt already exists.")

    imports = json.loads((ROOT / "reports/mlflow_import.json").read_text())
    uri = f"sqlite:///{ROOT / 'runtime/mlflow/tracking.db'}"
    if imports["tracking_uri"] != uri:
        raise ValueError("Import receipt uses a different tracking database.")

    mlflow.set_tracking_uri(uri)
    mlflow.set_registry_uri(uri)
    client = MlflowClient(tracking_uri=uri, registry_uri=uri)
    experiment = client.get_experiment_by_name("FraudShield-Models")
    if experiment is None:
        raise ValueError("Expected experiment is missing.")

    # Refuse to create duplicate registrations after a partial run.
    for name in NAMES.values():
        if client.search_registered_models(filter_string=f"name = '{name}'"):
            raise SystemExit(
                f"STOP: {name} already exists. Inspect before rerunning."
            )

    # Invented examples for the packaged input schema; no transaction logs.
    example = pd.DataFrame({
        "type": ["PAYMENT", "TRANSFER", "CASH_OUT", "CASH_IN", "DEBIT"],
        "amount": [50.0, 100000.0, 500.0, 250.0, 10.0],
        "oldbalanceOrg": [1000.0, 100000.0, 500.0, 0.0, 100.0],
    })

    validation = pd.read_csv(
        ROOT / "data/processed/paysim_v1/validation.csv",
        usecols=[*INPUT_COLUMNS, "isFraud"],
    )
    sample = pd.concat([
        validation[validation["isFraud"] == 0].head(50),
        validation[validation["isFraud"] == 1].head(50),
    ]).loc[:, list(INPUT_COLUMNS)].reset_index(drop=True)
    if len(sample) != 100:
        raise ValueError("Expected 100 verification examples.")
    del validation

    prepared = {}
    for key in NAMES:
        path = ROOT / f"artifacts/{key}_v1/model.joblib"
        expected_hash = imports["runs"][key]["model_sha256"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"{key}: bundle checksum mismatch.")
        prepared[key] = (path, expected_hash, joblib.load(path))

    receipt = {"tracking_uri": uri, "models": {}}
    for key, registered_name in NAMES.items():
        path, expected_hash, bundle = prepared[key]
        threshold = float(bundle["threshold"])
        example_scores = bundle["pipeline"].predict_proba(example)[:, 1]
        example_output = pd.DataFrame({
            "score": example_scores.astype(float),
            "threshold": np.full(len(example), threshold),
            "predicted_fraud": example_scores >= threshold,
        })

        with mlflow.start_run(
            experiment_id=experiment.experiment_id,
            run_name=f"{key}-v1-model-packaging",
            tags={
                "run_type": "model_packaging",
                "source_run_id": imports["runs"][key]["run_id"],
                "model_sha256": expected_hash,
                "test_evaluated": "false",
                "mlflow.note.content": (
                    "Packages an existing trained model. No retraining. "
                    "Registration does not imply production promotion."
                ),
            },
        ) as run:
            mlflow.log_param("selected_threshold", threshold)
            info = mlflow.pyfunc.log_model(
                name="fraudshield",
                python_model=FraudShieldModel(expected_hash),
                artifacts={"bundle": str(path)},
                code_paths=[str(ROOT / "src/fraudshield")],
                pip_requirements=str(ROOT / "requirements-ml.lock.txt"),
                signature=infer_signature(example, example_output),
                input_example=example,
            )

            version = mlflow.register_model(
                model_uri=info.model_uri,
                name=registered_name,
                await_registration_for=60,
            )
            registered_uri = f"models:/{registered_name}/{version.version}"
            restored = mlflow.pyfunc.load_model(registered_uri)
            actual = restored.predict(sample)
            expected_scores = bundle["pipeline"].predict_proba(sample)[:, 1]

            np.testing.assert_allclose(
                actual["score"].to_numpy(),
                expected_scores,
                rtol=1e-6,
                atol=1e-7,
            )
            np.testing.assert_array_equal(
                actual["predicted_fraud"].to_numpy(),
                expected_scores >= threshold,
            )
            np.testing.assert_array_equal(
                actual["threshold"].to_numpy(),
                np.full(len(sample), threshold),
            )

            client.set_model_version_tag(
                registered_name, version.version,
                "prediction_parity", "PASS",
            )
            client.set_model_version_tag(
                registered_name, version.version,
                "model_sha256", expected_hash,
            )
            mlflow.log_param("registry_version", version.version)
            mlflow.set_tag("prediction_parity", "PASS")

            receipt["models"][key] = {
                "name": registered_name,
                "version": version.version,
                "uri": registered_uri,
                "packaging_run_id": run.info.run_id,
                "source_run_id": imports["runs"][key]["run_id"],
                "model_sha256": expected_hash,
                "threshold": threshold,
                "verification_rows": len(sample),
                "prediction_parity": "PASS",
            }
            print(f"PASS: {registered_uri}", flush=True)

    with RECEIPT.open("x", encoding="utf-8") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
