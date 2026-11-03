"""Train CPU XGBoost from DVC-managed Spark Parquet."""

import hashlib
import json

import train_xgboost as job
from train_logreg_dvc import PREPARED, SPLITS, load_parquet_split


def main():
    report = json.loads((PREPARED / "spark_preparation.json").read_text())
    if report["status"] != "PASS":
        raise ValueError("Spark preparation did not pass.")
    if report["feature_names"] != list(job.FEATURE_NAMES):
        raise ValueError("Spark feature order differs from the trainer.")

    manifest = {"source_sha256": report["source_sha256"], "splits": {}}
    for name in SPLITS:
        files = {
            path: digest
            for path, digest in report["parquet_sha256"].items()
            if path.startswith(f"split={name}/")
        }
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest["splits"][name] = {
            **report["splits"][name],
            "files": files,
            "sha256": fingerprint,
        }

    job.OUTPUT = job.ROOT / "artifacts/xgboost_dvc_v1"
    job.REPORT = job.OUTPUT / "validation.json"
    with job.threadpool_limits(limits=2):
        job.main(manifest=manifest, split_loader=load_parquet_split)


if __name__ == "__main__":
    main()
