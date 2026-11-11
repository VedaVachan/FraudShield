"""Run a small, local Ray Tune grid on the verified Spark splits."""

import hashlib
import json
import os
from pathlib import Path
from tempfile import mkdtemp

for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[name] = "2"

import numpy as np  # noqa: E402 - thread limits set first
import ray  # noqa: E402 - thread limits set first
import xgboost as xgb  # noqa: E402 - thread limits set first
from ray import tune  # noqa: E402 - thread limits set first
from sklearn.metrics import average_precision_score  # noqa: E402 - thread limits set first
from threadpoolctl import threadpool_limits  # noqa: E402 - thread limits set first

from train_logreg_dvc import PREPARED, SPLITS, load_parquet_split  # noqa: E402 - thread limits set first

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "artifacts/xgboost_dvc_v1/validation.json"


def trial(config):
    preparation = json.loads((PREPARED / "spark_preparation.json").read_text())
    if preparation["status"] != "PASS":
        raise ValueError("Spark preparation did not pass.")

    baseline = json.loads(BASELINE.read_text())
    if preparation["feature_names"] != baseline["feature_names"]:
        raise ValueError("Feature order differs from the baseline.")
    if preparation["source_sha256"] != baseline["source_sha256"]:
        raise ValueError("Raw dataset differs from the baseline.")

    def load(name):
        if name not in SPLITS:
            raise ValueError(f"Unexpected split: {name}")
        files = {
            path: digest
            for path, digest in preparation["parquet_sha256"].items()
            if path.startswith(f"split={name}/")
        }
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if fingerprint != baseline["split_sha256"][name]:
            raise ValueError(f"{name}: split differs from the baseline.")
        return load_parquet_split(
            name, {**preparation["splits"][name], "files": files}
        )

    with threadpool_limits(limits=2):
        train_x, train_y, _ = load("train")
        train_x = train_x.astype(np.float32)
        val_x, val_y, _ = load("validation")
        val_x = val_x.astype(np.float32)

        params = {**baseline["training_parameters"], **config}
        model = xgb.XGBClassifier(**params)
        model.fit(train_x, train_y, eval_set=[(val_x, val_y)], verbose=False)
        scores = model.predict_proba(val_x)[:, 1]
        if not np.isfinite(scores).all():
            raise ValueError("Nonfinite validation scores.")

    tune.report({
        "average_precision": float(average_precision_score(val_y, scores)),
        "best_iteration": int(model.best_iteration),
    })


def main():
    baseline = json.loads(BASELINE.read_text())
    storage = Path(mkdtemp(prefix="ray-xgb-grid-", dir=ROOT / "runtime"))
    ray.init(
        address="local",
        num_cpus=2,
        object_store_memory=128 * 1024**2,
        include_dashboard=False,
    )

    try:
        results = tune.Tuner(
            tune.with_resources(trial, {"cpu": 2}),
            param_space={
                "max_depth": tune.grid_search([4, 6]),
                "min_child_weight": tune.grid_search([5, 10]),
            },
            tune_config=tune.TuneConfig(max_concurrent_trials=1),
            run_config=tune.RunConfig(
                name="xgboost_validation_grid",
                storage_path=str(storage),
                verbose=1,
            ),
        ).fit()

        if len(results) != 4 or results.errors:
            raise RuntimeError(f"Expected four successful trials: {results.errors}")

        rows = [
            {
                **result.config,
                "average_precision": result.metrics["average_precision"],
                "best_iteration_zero_based": result.metrics["best_iteration"],
            }
            for result in results
        ]
        rows.sort(key=lambda row: (
            -row["average_precision"], row["max_depth"], row["min_child_weight"]
        ))
        control = next(
            row for row in rows
            if row["max_depth"] == 4 and row["min_child_weight"] == 5
        )

        report = {
            "evaluation_split": "validation",
            "test_evaluated": False,
            "source_sha256": baseline["source_sha256"],
            "split_sha256": baseline["split_sha256"],
            "baseline_average_precision": baseline["average_precision"],
            "control_delta": control["average_precision"] - baseline["average_precision"],
            "best_config": rows[0],
            "trials": rows,
        }
        path = storage / "summary.json"
        with path.open("x") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        print(json.dumps(report, indent=2))
        print(f"PASS: four validation-only trials. Summary: {path}")
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
