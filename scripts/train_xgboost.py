"""Train CPU XGBoost and evaluate validation data only."""

import os

for variable in (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"
):
    os.environ[variable] = "2"

import json
import time
from pathlib import Path

import joblib
import numpy as np
import xgboost as xgb
from sklearn.metrics import (
    auc, average_precision_score, precision_recall_curve,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer
from threadpoolctl import threadpool_limits

from fraudshield.features import FEATURE_NAMES, build_features
from train_logreg import load_split, classification_metrics, sha256

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data/processed/paysim_v1"
OUTPUT = ROOT / "artifacts/xgboost_v1"
REPORT = ROOT / "reports/xgboost_validation.json"


def main():
    if OUTPUT.exists() or REPORT.exists():
        raise SystemExit("STOP: model output or report already exists.")

    started = time.perf_counter()
    manifest = json.loads((DATA / "manifest.json").read_text())

    print("Loading training data...", flush=True)
    train_x, train_y, _ = load_split("train", manifest["splits"]["train"])
    train_x = train_x.astype(np.float32)

    print("Loading validation data...", flush=True)
    val_x, val_y, sample = load_split(
        "validation", manifest["splits"]["validation"]
    )
    val_x = val_x.astype(np.float32)

    positives = int(train_y.sum())
    negatives = len(train_y) - positives

    # A fixed initial heuristic, calculated from training labels only.
    positive_weight = float(np.sqrt(negatives / positives))

    parameters = {
        "objective": "binary:logistic",
        "eval_metric": "aucpr",
        "tree_method": "hist",
        "device": "cpu",
        "n_jobs": 2,
        "n_estimators": 300,
        "max_depth": 4,
        "learning_rate": 0.05,
        "min_child_weight": 5,
        "subsample": 0.8,
        "colsample_bytree": 1.0,
        "max_bin": 256,
        "reg_lambda": 1.0,
        "scale_pos_weight": positive_weight,
        "early_stopping_rounds": 25,
        "random_state": 42,
    }
    model = xgb.XGBClassifier(**parameters)

    print("Training XGBoost on CPU...", flush=True)
    fit_started = time.perf_counter()
    model.fit(
        train_x,
        train_y,
        eval_set=[(val_x, val_y)],
        verbose=25,
    )
    fit_seconds = time.perf_counter() - fit_started
    train_rows = len(train_y)
    del train_x, train_y

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
    best_index = np.flatnonzero(f1 == f1.max())[-1]
    threshold = float(thresholds[best_index])

    pipeline = Pipeline([
        ("features", FunctionTransformer(build_features, validate=False)),
        ("classifier", model),
    ])
    np.testing.assert_allclose(
        pipeline.predict_proba(sample)[:, 1],
        scores[:len(sample)],
        rtol=1e-6,
        atol=1e-7,
    )

    result = {
        "model": "xgboost",
        "evaluation_split": "validation",
        "test_evaluated": False,
        "threshold_selection": "maximum validation F1; higher threshold on ties",
        "train_rows": train_rows,
        "validation_rows": len(val_y),
        "validation_fraud_rate": float(val_y.mean()),
        "average_precision": float(average_precision_score(val_y, scores)),
        "pr_auc_trapezoidal": float(auc(recall, precision)),
        "at_0_5": classification_metrics(val_y, scores, 0.5),
        "at_selected_threshold": classification_metrics(val_y, scores, threshold),
        "confusion_matrix_layout": "[[TN, FP], [FN, TP]]",
        "fit_seconds": fit_seconds,
        "best_iteration_zero_based": int(model.best_iteration),
        "best_validation_aucpr_xgboost": float(model.best_score),
        "rounds_trained": len(model.evals_result()["validation_0"]["aucpr"]),
        "feature_names": list(FEATURE_NAMES),
        "training_parameters": parameters,
        "xgboost_version": xgb.__version__,
        "source_sha256": manifest["source_sha256"],
        "split_sha256": {
            name: manifest["splits"][name]["sha256"]
            for name in ("train", "validation")
        },
    }

    OUTPUT.mkdir(parents=True)
    model_path = OUTPUT / "model.joblib"
    joblib.dump(
        {"pipeline": pipeline, "threshold": threshold, "metadata": result},
        model_path,
    )
    model.save_model(OUTPUT / "model.ubj")

    restored = joblib.load(model_path)
    np.testing.assert_allclose(
        restored["pipeline"].predict_proba(sample),
        pipeline.predict_proba(sample),
        rtol=1e-6,
        atol=1e-7,
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
    print("PASS: XGBoost training, validation, pipeline parity, and reload.")
    print("Test set was not opened.")


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
