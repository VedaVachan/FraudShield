"""Compare local MLflow HTTP predictions with the registered v1 bundle."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import httpx
import joblib
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports/mlflow_http_verification.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if OUTPUT.exists():
        raise SystemExit("STOP: HTTP verification report already exists.")

    registry_path = ROOT / "reports/mlflow_registry.json"
    registry = json.loads(registry_path.read_text())["models"]["xgboost"]
    model_path = ROOT / "artifacts/xgboost_v1/model.joblib"
    if digest(model_path) != registry["model_sha256"]:
        raise ValueError("Local bundle differs from the registry receipt.")
    bundle = joblib.load(model_path)
    if bundle["threshold"] != registry["threshold"]:
        raise ValueError("Threshold differs from the registry receipt.")

    records = [
        {"type": "TRANSFER", "amount": 100000.0, "oldbalanceOrg": 100000.0},
        {"type": "PAYMENT", "amount": 100.0, "oldbalanceOrg": 5000.0},
        {"type": "CASH_OUT", "amount": 10000.0, "oldbalanceOrg": 500.0},
    ]
    with httpx.Client(
        base_url="http://127.0.0.1:5001", timeout=30, trust_env=False
    ) as client:
        client.get("/ping").raise_for_status()
        response = client.post(
            "/invocations", json={"dataframe_records": records}
        )
        response.raise_for_status()
        actual = pd.DataFrame(response.json()["predictions"])

    if len(actual) != len(records):
        raise ValueError("Unexpected number of predictions.")
    scores = bundle["pipeline"].predict_proba(pd.DataFrame(records))[:, 1]
    np.testing.assert_allclose(
        actual["score"], scores, rtol=1e-6, atol=1e-7
    )
    np.testing.assert_allclose(
        actual["threshold"], bundle["threshold"], rtol=0, atol=0
    )
    np.testing.assert_array_equal(
        actual["predicted_fraud"], scores >= bundle["threshold"]
    )

    report = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "verification_code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "verification_script_sha256": digest(Path(__file__)),
        "base_url": "http://127.0.0.1:5001",
        "expected_registry_uri": registry["uri"],
        "reference_model_sha256": registry["model_sha256"],
        "registry_receipt_sha256": digest(registry_path),
        "serving_lock_sha256": digest(ROOT / "requirements-serving.lock.txt"),
        "synthetic_cases": len(records),
        "ping": "PASS",
        "score_parity": "PASS",
        "threshold_parity": "PASS",
        "decision_parity": "PASS",
        "scope": (
            "HTTP outputs match the reference on three synthetic cases; "
            "sample parity alone does not prove server artifact identity."
        ),
        "fastapi_uses_mlflow_http": False,
    }
    with OUTPUT.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
