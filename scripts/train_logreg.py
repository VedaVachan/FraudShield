"""Train logistic regression; choose a threshold on validation only."""

import os

for variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"
):
    os.environ[variable] = "2"

import hashlib
import json
import time
import warnings
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.exceptions import ConvergenceWarning
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    auc,
    average_precision_score,
    confusion_matrix,
    precision_recall_curve,
    precision_recall_fscore_support,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, StandardScaler
from threadpoolctl import threadpool_limits

from fraudshield.features import (
    INPUT_COLUMNS, FEATURE_NAMES, build_features,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/processed/paysim_v1"
OUTPUT = ROOT / "artifacts/logreg_v1"
REPORT = ROOT / "reports/logreg_validation.json"
CHUNK_SIZE = 100_000


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_split(name, metadata):
    path = DATA / f"{name}.csv"
    if sha256(path) != metadata["sha256"]:
        raise ValueError(f"{name}: checksum mismatch.")

    count = metadata["rows"]
    features = np.empty((count, len(FEATURE_NAMES)), dtype=np.float64)
    labels = np.empty(count, dtype=np.int8)
    offset = 0
    sample = None

    for chunk in pd.read_csv(
        path,
        usecols=[*INPUT_COLUMNS, "isFraud"],
        dtype={
            "type": "category",
            "amount": "float64",
            "oldbalanceOrg": "float64",
            "isFraud": "int8",
        },
        chunksize=CHUNK_SIZE,
    ):
        inputs = chunk.loc[:, list(INPUT_COLUMNS)]
        if sample is None:
            sample = inputs.head(20).copy()

        end = offset + len(chunk)
        if end > count:
            raise ValueError(f"{name}: too many rows.")

        features[offset:end] = build_features(inputs)
        labels[offset:end] = chunk["isFraud"].to_numpy()
        offset = end

        if offset % 1_000_000 == 0:
            print(f"{name}: loaded {offset:,} rows", flush=True)

    if offset != count or int(labels.sum()) != metadata["fraud"]:
        raise ValueError(f"{name}: unexpected row or fraud count.")
    if set(np.unique(labels)) != {0, 1}:
        raise ValueError(f"{name}: invalid classes.")

    return features, labels, sample


def classification_metrics(labels, scores, threshold):
    predictions = (scores >= threshold).astype(np.int8)
    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, predictions, average="binary", zero_division=0
    )
    return {
        "threshold": float(threshold),
        "precision": float(precision),
        "recall": float(recall),
        "f1": float(f1),
        "confusion_matrix": confusion_matrix(
            labels, predictions, labels=[0, 1]
        ).tolist(),
        "predicted_fraud_rate": float(predictions.mean()),
    }


def main(*, manifest=None, split_loader=load_split):
    if OUTPUT.exists() or REPORT.exists():
        raise SystemExit("STOP: model output or validation report already exists.")

    if manifest is None:
        manifest = json.loads((DATA / "manifest.json").read_text())
    started = time.perf_counter()

    print("Loading training data...", flush=True)
    train_x, train_y, _ = split_loader("train", manifest["splits"]["train"])

    scaler = StandardScaler()
    for start in range(0, len(train_x), CHUNK_SIZE):
        scaler.partial_fit(train_x[start:start + CHUNK_SIZE])
    for start in range(0, len(train_x), CHUNK_SIZE):
        scaler.transform(train_x[start:start + CHUNK_SIZE], copy=False)

    model = LogisticRegression(
        solver="lbfgs",
        class_weight="balanced",
        C=1.0,
        max_iter=500,
        tol=1e-4,
        random_state=42,
    )

    print("Fitting logistic regression; this stage may be quiet...", flush=True)
    fit_started = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        model.fit(train_x, train_y)
    fit_seconds = time.perf_counter() - fit_started

    train_rows = len(train_y)
    del train_x, train_y

    print("Evaluating validation data...", flush=True)
    val_x, val_y, sample = split_loader(
        "validation", manifest["splits"]["validation"]
    )
    scaler.transform(val_x, copy=False)
    scores = model.predict_proba(val_x)[:, 1]

    if not np.isfinite(scores).all():
        raise ValueError("Nonfinite validation scores.")

    precision, recall, thresholds = precision_recall_curve(val_y, scores)
    f1 = np.divide(
        2 * precision[:-1] * recall[:-1],
        precision[:-1] + recall[:-1],
        out=np.zeros_like(thresholds),
        where=(precision[:-1] + recall[:-1]) > 0,
    )
    # On an exact F1 tie, choose the higher threshold.
    best_index = np.flatnonzero(f1 == f1.max())[-1]
    threshold = float(thresholds[best_index])

    pipeline = Pipeline([
        ("features", FunctionTransformer(build_features, validate=False)),
        ("scaler", scaler),
        ("classifier", model),
    ])
    np.testing.assert_allclose(
        pipeline.predict_proba(sample)[:, 1],
        scores[:len(sample)],
        rtol=1e-10,
        atol=1e-12,
    )

    result = {
        "model": "logistic_regression",
        "evaluation_split": "validation",
        "threshold_selection": "maximum validation F1; higher threshold on ties",
        "test_evaluated": False,
        "train_rows": train_rows,
        "validation_rows": len(val_y),
        "validation_fraud_rate": float(val_y.mean()),
        "average_precision": float(average_precision_score(val_y, scores)),
        "pr_auc_trapezoidal": float(auc(recall, precision)),
        "at_0_5": classification_metrics(val_y, scores, 0.5),
        "at_selected_threshold": classification_metrics(val_y, scores, threshold),
        "confusion_matrix_layout": "[[TN, FP], [FN, TP]]",
        "fit_seconds": fit_seconds,
        "iterations": model.n_iter_.tolist(),
        "feature_names": list(FEATURE_NAMES),
        "training_parameters": model.get_params(),
        "sklearn_version": sklearn.__version__,
        "source_sha256": manifest["source_sha256"],
        "split_sha256": {
            name: manifest["splits"][name]["sha256"]
            for name in ("train", "validation")
        },
    }

    OUTPUT.mkdir(parents=True)
    bundle = {
        "pipeline": pipeline,
        "threshold": threshold,
        "metadata": result,
    }
    model_path = OUTPUT / "model.joblib"
    joblib.dump(bundle, model_path)

    restored = joblib.load(model_path)
    np.testing.assert_allclose(
        restored["pipeline"].predict_proba(sample),
        pipeline.predict_proba(sample),
        rtol=1e-10,
        atol=1e-12,
    )
    if restored["threshold"] != threshold:
        raise ValueError("Saved threshold differs after reload.")

    result["model_sha256"] = sha256(model_path)
    result["serialization_check"] = "PASS"
    result["total_seconds"] = time.perf_counter() - started

    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    print(json.dumps(result, indent=2))
    print("PASS: training, validation, pipeline parity, and model reload.")
    print("Test set was not opened.")


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
