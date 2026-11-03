"""Evaluate the frozen DVC XGBoost model on chronological holdout data."""

import json
from pathlib import Path

import train_xgboost as job
import pyarrow.parquet as pq
from sklearn.metrics import average_precision_score

ROOT = job.ROOT
FREEZE = ROOT / "reports/xgboost_model_selection.json"
PREPARED = ROOT / "data/processed/paysim_spark_dvc_v1"
OUTPUT = ROOT / "reports/xgboost_heldout_evaluation.json"
INPUTS = ["type", "amount", "oldbalanceOrg"]


def read_json(path):
    return json.loads(path.read_text())


def main():
    if OUTPUT.exists():
        raise SystemExit(f"STOP: evaluation report already exists: {OUTPUT}")

    selection = read_json(FREEZE)
    prepared = read_json(PREPARED / "spark_preparation.json")
    if (
        selection["selection_split"] != "validation"
        or selection["test_evaluated_at_selection"] is not False
        or prepared["status"] != "PASS"
        or prepared["source_sha256"] != selection["source_sha256"]
    ):
        raise ValueError("Unexpected model-selection or data provenance.")

    model_path = ROOT / selection["selected_model"]
    if job.sha256(model_path) != selection["model_sha256"]:
        raise ValueError("Frozen model checksum mismatch.")
    bundle = job.joblib.load(model_path)
    threshold = selection["threshold"]
    if bundle["threshold"] != threshold:
        raise ValueError("Frozen threshold differs from saved model.")

    parquet = PREPARED / "parquet"
    files = {
        path: digest
        for path, digest in prepared["parquet_sha256"].items()
        if path.startswith("split=test/")
    }
    actual = {
        path.relative_to(parquet).as_posix()
        for path in (parquet / "split=test").glob("*.parquet")
    }
    if not files or actual != set(files):
        raise ValueError("Test partition files differ from the Spark report.")

    for name, digest in files.items():
        if job.sha256(parquet / name) != digest:
            raise ValueError(f"Test file checksum mismatch: {name}")

    label_batches, score_batches = [], []
    with job.threadpool_limits(limits=2):
        for name in sorted(files):
            with pq.ParquetFile(parquet / name) as source:
                for batch in source.iter_batches(
                    batch_size=100_000,
                    columns=[*INPUTS, "isFraud"],
                    use_threads=False,
                ):
                    frame = batch.to_pandas()
                    labels = frame["isFraud"].to_numpy()
                    scores = bundle["pipeline"].predict_proba(frame[INPUTS])[:, 1]
                    if not job.np.isin(labels, [0, 1]).all():
                        raise ValueError("Invalid test labels.")
                    if not job.np.isfinite(scores).all():
                        raise ValueError("Nonfinite test scores.")
                    label_batches.append(labels)
                    score_batches.append(scores)

    labels = job.np.concatenate(label_batches)
    scores = job.np.concatenate(score_batches)
    expected = prepared["splits"]["test"]
    if len(labels) != expected["rows"] or int(labels.sum()) != expected["fraud"]:
        raise ValueError("Held-out row or fraud count differs from Spark report.")

    precision, recall, _ = job.precision_recall_curve(labels, scores)
    report = {
        "evaluation_split": "test",
        "selected_model": selection["selected_model"],
        "model_sha256": selection["model_sha256"],
        "selection_record_sha256": job.sha256(FREEZE),
        "source_sha256": selection["source_sha256"],
        "test_parquet_sha256": files,
        "test_rows": len(labels),
        "test_fraud_rows": int(labels.sum()),
        "test_fraud_rate": float(labels.mean()),
        "average_precision": float(average_precision_score(labels, scores)),
        "pr_auc_trapezoidal": float(job.auc(recall, precision)),
        "at_frozen_threshold": job.classification_metrics(labels, scores, threshold),
        "confusion_matrix_layout": "[[TN, FP], [FN, TP]]",
    }
    with OUTPUT.open("x") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report, indent=2))
    print(f"PASS: frozen model evaluated. Report: {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
