"""Package existing approved bundles with prediction identity; never deploy them.

Install this file under scripts/, commit it, then run with PYTHONPATH=src:.
The package revision identity-v1 is independent of model registry versions.
"""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "runtime/mlflow-serving/identity-v1"
RECEIPT = ROOT / "reports/mlflow_identity_packages.json"
APPROVED = {
    "champion": {
        "bundle": "artifacts/xgboost_v1/model.joblib",
        "sha256": "1d2df49373788c5b1e554ec9ab9cfc346cc0feb1e13406bc0cd93b1594518bcb",
        "threshold": 0.9195699691772461,
    },
    "challenger": {
        "bundle": "artifacts/xgboost_tuned_v1/model.joblib",
        "sha256": "53e1e56d5109d850ee3ae5e35cc2abd3af2cc70d75ea70b5683203265814a3f0",
        "threshold": 0.8567820191383362,
    },
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*arguments):
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, text=True
    ).strip()


def source_paths():
    package = ROOT / "src/fraudshield"
    require(package.is_dir(), "Run the installed script in FraudShield.")
    paths = [p.relative_to(ROOT).as_posix() for p in package.rglob("*.py")]
    paths += [
        "scripts/package_identity_models.py",
        "requirements-serving.lock.txt",
        "reports/mlflow_registry.json",
    ]
    return sorted(paths)


def snapshot(paths):
    return {name: sha256(ROOT / name) for name in paths}


def preflight():
    require(not OUTPUT.exists(), f"Output already exists; inspect it: {OUTPUT}")
    require(not RECEIPT.exists(), f"Receipt already exists: {RECEIPT}")
    paths = source_paths()
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", *paths],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
    )
    require(not git("diff", "HEAD", "--", *paths),
            "Commit packaging sources and configuration before running.")
    for role, item in APPROVED.items():
        require(sha256(ROOT / item["bundle"]) == item["sha256"],
                f"Approved source checksum mismatch: {role}")
    registry = json.loads((ROOT / "reports/mlflow_registry.json").read_text())
    champion = registry["models"]["xgboost"]
    require(champion["model_sha256"] == APPROVED["champion"]["sha256"],
            "Champion registry receipt differs from the approved bundle.")
    require(champion["threshold"] == APPROVED["champion"]["threshold"],
            "Champion registry threshold differs.")
    return paths, snapshot(paths), git("rev-parse", "HEAD")


def package_one(role, item, folder, frame, commit, sources):
    import joblib
    import mlflow.pyfunc
    from mlflow.models import infer_signature
    import numpy as np
    import pandas as pd

    from fraudshield.mlflow_model import IdentifiedFraudShieldModel

    source = ROOT / item["bundle"]
    payload = source.read_bytes()
    require(hashlib.sha256(payload).hexdigest() == item["sha256"],
            f"Source changed before packaging: {role}")
    bundle = joblib.load(BytesIO(payload))
    threshold = float(bundle["threshold"])
    require(threshold == item["threshold"], f"Unexpected threshold: {role}")
    scores = bundle["pipeline"].predict_proba(frame)[:, 1]
    require(np.isfinite(scores).all() and ((scores >= 0) & (scores <= 1)).all(),
            f"Invalid reference scores: {role}")
    expected = pd.DataFrame({
        "score": scores.astype(float),
        "threshold": np.full(len(frame), threshold),
        "predicted_fraud": scores >= threshold,
        "model_sha256": [item["sha256"]] * len(frame),
    })
    destination = folder / role
    mlflow.pyfunc.save_model(
        path=str(destination),
        python_model=IdentifiedFraudShieldModel(item["sha256"]),
        artifacts={"bundle": str(source)},
        code_paths=[str(ROOT / "src/fraudshield")],
        pip_requirements=str(ROOT / "requirements-serving.lock.txt"),
        signature=infer_signature(frame, expected),
        input_example=frame,
        metadata={"packaging_git_commit": commit, "bundle_sha256": item["sha256"]},
    )
    require(sha256(destination / "artifacts/model.joblib") == item["sha256"],
            f"Copied bundle checksum mismatch: {role}")
    require(sha256(destination / "code/fraudshield/mlflow_model.py") ==
            sources["src/fraudshield/mlflow_model.py"],
            f"Packaged wrapper differs: {role}")
    restored = mlflow.pyfunc.load_model(str(destination))
    actual = restored.predict(frame)
    require(list(actual.columns) == list(expected.columns),
            f"Unexpected output contract: {role}")
    np.testing.assert_allclose(actual["score"], expected["score"], rtol=0, atol=1e-7)
    np.testing.assert_array_equal(actual["threshold"], expected["threshold"])
    np.testing.assert_array_equal(actual["predicted_fraud"], expected["predicted_fraud"])
    require(actual["model_sha256"].tolist() == expected["model_sha256"].tolist(),
            f"Prediction identity mismatch: {role}")
    files = {}
    for path in sorted(destination.rglob("*")):
        require(not path.is_symlink(), f"Unexpected package symlink: {path}")
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(destination).as_posix()] = sha256(path)
    print(f"PASS: {role} package restored; scores, decisions and identity verified.",
          flush=True)
    return {
        "package": (OUTPUT / role).relative_to(ROOT).as_posix(),
        "source_bundle": item["bundle"],
        "model_sha256": item["sha256"],
        "threshold": threshold,
        "verification_rows": len(frame),
        "prediction_parity": "PASS",
        "identity": "PASS",
        "package_file_sha256": files,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check prerequisites only")
    args = parser.parse_args()
    paths, sources, commit = preflight()
    if args.check:
        print("PASS: committed sources and approved model bundles verified; no packages created.")
        return

    lock_path = ROOT / "runtime/serving_audit/identity-packaging.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        paths, sources, commit = preflight()
        import pandas as pd
        from threadpoolctl import threadpool_limits

        # Invented inputs cover all transaction types and zero amounts/balances.
        frame = pd.DataFrame({
            "type": ["TRANSFER", "CASH_OUT", "PAYMENT", "CASH_IN", "DEBIT"],
            "amount": [100000.0, 500.0, 50.0, 0.0, 10.0],
            "oldbalanceOrg": [100000.0, 500.0, 1000.0, 0.0, 100.0],
        })
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".identity-build-", dir=OUTPUT.parent) as temporary:
            folder = Path(temporary) / "identity-v1"
            folder.mkdir()
            with threadpool_limits(limits=2):
                models = {
                    role: package_one(role, item, folder, frame, commit, sources)
                    for role, item in APPROVED.items()
                }
            require(snapshot(paths) == sources and git("rev-parse", "HEAD") == commit,
                    "Packaging sources or Git revision changed during execution.")
            for role, item in APPROVED.items():
                require(sha256(ROOT / item["bundle"]) == item["sha256"],
                        f"Source bundle changed during packaging: {role}")
            record = {
                "schema_version": 1,
                "created_utc": datetime.now(timezone.utc).isoformat(),
                "scope": "local identity-aware packages; in-process restored prediction checks",
                "packaging_git_commit": commit,
                "source_sha256": sources,
                "models": models,
                "test_set_opened": False,
                "retrained": False,
                "registered_or_promoted": False,
                "deployment_changed": False,
                "limitations": [
                    "Five synthetic cases check parity; they do not evaluate model accuracy.",
                    "HTTP and serving-container verification must be performed separately.",
                    "A reported SHA256 provides local consistency checking, not remote attestation.",
                    "Package files are checksummed, not guaranteed byte-identical across rebuilds.",
                ],
            }
            text = json.dumps(record, indent=2, allow_nan=False) + "\n"
            (folder / "verification.json").write_text(text)
            # Publishing the prepared directory does not change any Compose mounts.
            require(not OUTPUT.exists(), "Output appeared during execution; refusing replacement.")
            folder.rename(OUTPUT)
            with RECEIPT.open("x") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
    print("PASS: identity packages prepared; serving and registry unchanged.")
    print(f"Receipt: {RECEIPT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
