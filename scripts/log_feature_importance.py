"""Log gain-based feature importance of the deployed model as a new MLflow run.

The model is only read: no training, no test data, no registry or model change.
The run is refused unless the model file's SHA-256 matches the expected value.
"""

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import joblib
from mlflow.tracking import MlflowClient

from fraudshield.features import FEATURE_NAMES


ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_MODEL = ROOT / "artifacts/xgboost_v1/model.joblib"
EXPECTED_SHA256 = "1d2df49373788c5b1e554ec9ab9cfc346cc0feb1e13406bc0cd93b1594518bcb"
TRACKING_DATABASE = ROOT / "runtime/mlflow/tracking.db"
EXPERIMENT = "FraudShield-Models"
RUN_NAME = "deployed-model-feature-importance"


class ModelIdentityError(ValueError):
    """The model file is not the one this analysis is approved for."""


def sha256_file(path):
    """Return the SHA-256 of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_model(path, expected_sha256):
    """Return the model's SHA-256 or raise if it differs from the expected value."""
    actual = sha256_file(path)
    if actual != expected_sha256:
        raise ModelIdentityError(
            "Model checksum differs from the expected value; nothing was logged."
        )
    return actual


def load_classifier(path):
    """Load the trusted model bundle and return its XGBoost classifier."""
    bundle = joblib.load(path)
    return bundle["pipeline"].named_steps["classifier"]


def gain_importance(classifier, model_sha256):
    """Return gain-based importance per feature, using the deployed trees only."""
    if int(classifier.n_features_in_) != len(FEATURE_NAMES):
        raise ValueError("The model does not have the expected nine features.")
    try:
        tree_count = int(classifier.best_iteration) + 1
    except AttributeError:
        tree_count = int(classifier.get_booster().num_boosted_rounds())
    booster = classifier.get_booster()[:tree_count]
    scores = booster.get_score(importance_type="gain")

    positional = {f"f{index}" for index in range(len(FEATURE_NAMES))}
    if not set(scores) <= positional | set(FEATURE_NAMES):
        raise ValueError("The model reports unexpected feature names.")
    gains = []
    for index, name in enumerate(FEATURE_NAMES):
        gains.append(float(scores.get(name, scores.get(f"f{index}", 0.0))))
    total = sum(gains)
    shares = [gain / total if total > 0 else 0.0 for gain in gains]
    order = sorted(range(len(gains)), key=lambda i: (-gains[i], i))
    rank = {index: position + 1 for position, index in enumerate(order)}
    return {
        "schema_version": 1,
        "importance_type": "gain",
        "model_sha256": model_sha256,
        "tree_count": tree_count,
        "scope": "computed from the saved trees only; no data was scored",
        "features": [
            {"feature": name, "gain": gains[i], "share": shares[i], "rank": rank[i]}
            for i, name in enumerate(FEATURE_NAMES)
        ],
    }


def render_chart(record, path):
    """Write a bar chart of gain shares; return False if matplotlib is unavailable."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    rows = sorted(record["features"], key=lambda item: item["share"])
    figure, axes = plt.subplots(figsize=(7, 4.5))
    axes.barh([item["feature"] for item in rows], [item["share"] for item in rows])
    axes.set_xlabel("Share of total gain")
    axes.set_title("Deployed model: gain-based feature importance")
    figure.tight_layout()
    figure.savefig(path, dpi=120)
    plt.close(figure)
    return True


def git_state(root):
    """Return the current commit hash and whether the working tree has changes."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip())
    return commit, dirty


def log_feature_importance(model_path, expected_sha256, tracking_uri, experiment,
                           receipt_path=None, artifact_location=None, root=ROOT):
    """Verify the model, then log one new analysis run; return a receipt dict."""
    if receipt_path is not None and Path(receipt_path).exists():
        raise FileExistsError(f"Receipt already exists: {receipt_path}")
    model_sha256 = verify_model(model_path, expected_sha256)
    record = gain_importance(load_classifier(model_path), model_sha256)
    commit, dirty = git_state(root)

    client = MlflowClient(tracking_uri=tracking_uri)
    found = client.get_experiment_by_name(experiment)
    experiment_id = (
        found.experiment_id if found is not None
        else client.create_experiment(experiment, artifact_location=artifact_location)
    )
    tags = {
        "run_type": "analysis",
        "git_commit": commit,
        "git_dirty": str(dirty).lower(),
        "model_sha256": model_sha256,
        "importance_type": "gain",
        "retraining": "none",
        "test_data_used": "false",
    }
    run = client.create_run(experiment_id, tags=tags, run_name=RUN_NAME)
    run_id = run.info.run_id
    try:
        with TemporaryDirectory() as folder:
            json_path = Path(folder) / "feature_importance.json"
            json_path.write_text(json.dumps(record, indent=2, allow_nan=False) + "\n")
            charted = render_chart(record, Path(folder) / "feature_importance.png")
            client.set_tag(run_id, "feature_importance_chart",
                           "png" if charted else "unavailable")
            for item in record["features"]:
                client.log_metric(run_id, f"gain_share_{item['feature']}", item["share"])
            client.log_artifact(run_id, str(json_path))
            if charted:
                client.log_artifact(run_id, str(Path(folder) / "feature_importance.png"))
    except BaseException:
        client.set_terminated(run_id, "FAILED")
        raise
    client.set_terminated(run_id, "FINISHED")

    receipt = {
        "run_id": run_id,
        "run_type": "analysis",
        "tracking_uri": tracking_uri,
        "experiment": experiment,
        "model_sha256": model_sha256,
        "git_commit": commit,
        "chart_logged": charted,
        "registered_or_promoted": False,
    }
    if receipt_path is not None:
        with Path(receipt_path).open("x", encoding="utf-8") as handle:
            json.dump(receipt, handle, indent=2)
            handle.write("\n")
    return receipt


def main(argv=None):
    """Parse arguments, refuse unsafe combinations and log the analysis run."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEPLOYED_MODEL)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--tracking-uri", default=f"sqlite:///{TRACKING_DATABASE}")
    parser.add_argument("--experiment", default=EXPERIMENT)
    parser.add_argument("--artifact-location",
                        help="only used if the experiment has to be created")
    parser.add_argument("--receipt", type=Path, help="write a receipt (never overwritten)")
    args = parser.parse_args(argv)

    if args.expected_sha256 is None:
        if args.model != DEPLOYED_MODEL:
            parser.error("--expected-sha256 is required with a different --model.")
        args.expected_sha256 = EXPECTED_SHA256
    if (args.tracking_uri == f"sqlite:///{TRACKING_DATABASE}"
            and not TRACKING_DATABASE.is_file()):
        print("STOP: expected MLflow database is missing.", file=sys.stderr)
        return 2
    try:
        receipt = log_feature_importance(
            args.model, args.expected_sha256, args.tracking_uri, args.experiment,
            receipt_path=args.receipt, artifact_location=args.artifact_location,
        )
    except ModelIdentityError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
