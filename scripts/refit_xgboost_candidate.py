"""Refit the Ray Tune winner and select its threshold on validation."""

import hashlib
import json
import math

import train_xgboost as job
from train_logreg_dvc import PREPARED, load_parquet_split
from sklearn.metrics import average_precision_score

ROOT = job.ROOT
OUT = ROOT / "artifacts/xgboost_tuned_v1"


def read_json(path):
    return json.loads(path.read_text())


def main():
    if OUT.exists():
        raise SystemExit(f"STOP: candidate output already exists: {OUT}")

    grid = read_json(ROOT / "reports/xgboost_ray_tune_validation.json")
    baseline = read_json(ROOT / "artifacts/xgboost_dvc_v1/validation.json")
    prepared = read_json(PREPARED / "spark_preparation.json")
    winner = grid["best_config"]

    if (
        grid["evaluation_split"] != "validation"
        or grid["test_evaluated"] is not False
        or grid["control_delta"] != 0
        or prepared["status"] != "PASS"
        or prepared["feature_names"] != list(job.FEATURE_NAMES)
        or prepared["source_sha256"] != grid["source_sha256"]
        or baseline["source_sha256"] != grid["source_sha256"]
        or baseline["split_sha256"] != grid["split_sha256"]
    ):
        raise ValueError("Input reports do not match the verified tuning run.")

    def load(name):
        files = {
            path: digest
            for path, digest in prepared["parquet_sha256"].items()
            if path.startswith(f"split={name}/")
        }
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True).encode("utf-8")
        ).hexdigest()
        if fingerprint != grid["split_sha256"][name]:
            raise ValueError(f"{name}: split differs from the tuning run.")
        return load_parquet_split(
            name, {**prepared["splits"][name], "files": files}
        )

    params = {
        **baseline["training_parameters"],
        "max_depth": winner["max_depth"],
        "min_child_weight": winner["min_child_weight"],
    }

    with job.threadpool_limits(limits=2):
        train_x, train_y, _ = load("train")
        train_x = train_x.astype(job.np.float32)
        val_x, val_y, sample = load("validation")
        val_x = val_x.astype(job.np.float32)

        model = job.xgb.XGBClassifier(**params)
        model.fit(train_x, train_y, eval_set=[(val_x, val_y)], verbose=25)
        del train_x, train_y

        scores = model.predict_proba(val_x)[:, 1]
        if not job.np.isfinite(scores).all():
            raise ValueError("Nonfinite validation scores.")

        ap = float(average_precision_score(val_y, scores))
        if (
            not math.isclose(ap, winner["average_precision"], rel_tol=0, abs_tol=1e-6)
            or int(model.best_iteration) != winner["best_iteration_zero_based"]
        ):
            raise ValueError(f"Refit differs from Tune: AP={ap}, iteration={model.best_iteration}")

        precision, recall, thresholds = job.precision_recall_curve(val_y, scores)
        denom = precision[:-1] + recall[:-1]
        f1 = job.np.divide(
            2 * precision[:-1] * recall[:-1], denom,
            out=job.np.zeros_like(thresholds), where=denom > 0,
        )
        threshold = float(thresholds[job.np.flatnonzero(f1 == f1.max())[-1]])

        pipeline = job.Pipeline([
            ("features", job.FunctionTransformer(job.build_features, validate=False)),
            ("classifier", model),
        ])
        job.np.testing.assert_allclose(
            pipeline.predict_proba(sample)[:, 1],
            scores[:len(sample)],
            rtol=1e-6, atol=1e-7,
        )

        report = {
            "model": "xgboost_tuned_candidate",
            "evaluation_split": "validation",
            "test_evaluated": False,
            "source_sha256": grid["source_sha256"],
            "split_sha256": grid["split_sha256"],
            "feature_names": list(job.FEATURE_NAMES),
            "training_parameters": params,
            "average_precision": ap,
            "best_iteration_zero_based": int(model.best_iteration),
            "threshold_selection": "maximum validation F1; higher threshold on ties",
            "at_selected_threshold": job.classification_metrics(val_y, scores, threshold),
        }

        OUT.mkdir(parents=True)
        path = OUT / "model.joblib"
        job.joblib.dump(
            {"pipeline": pipeline, "threshold": threshold, "metadata": report},
            path,
        )
        model.save_model(OUT / "model.ubj")
        restored = job.joblib.load(path)
        job.np.testing.assert_allclose(
            restored["pipeline"].predict_proba(sample),
            pipeline.predict_proba(sample),
            rtol=1e-6, atol=1e-7,
        )
        if restored["threshold"] != threshold:
            raise ValueError("Reloaded threshold differs.")

        report["model_sha256"] = job.sha256(path)
        report["serialization_check"] = "PASS"
        with (OUT / "validation.json").open("x") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")

        print(json.dumps(report, indent=2))
        print("PASS: Tune winner reproduced; candidate saved and reloaded.")
        print("Test set was not opened.")


if __name__ == "__main__":
    main()
