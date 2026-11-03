"""Train the logistic regression baseline from DVC-managed Spark Parquet."""

import hashlib
import json

import train_logreg as job

import numpy as np
import pyarrow.parquet as pq

PREPARED = job.ROOT / "data/processed/paysim_spark_dvc_v1"
PARQUET = PREPARED / "parquet"
OUTPUT = job.ROOT / "artifacts/logreg_dvc_v1"
SPLITS = ("train", "validation")


def load_parquet_split(name, metadata):
    if name not in SPLITS:
        raise ValueError(f"Unexpected training input split: {name}")

    files = metadata["files"]
    actual_files = {
        path.relative_to(PARQUET).as_posix()
        for path in (PARQUET / f"split={name}").glob("*.parquet")
    }
    if not files or actual_files != set(files):
        raise ValueError(f"{name}: Parquet files differ from the Spark report.")

    count = metadata["rows"]
    features = np.empty((count, len(job.FEATURE_NAMES)), dtype=np.float64)
    labels = np.empty(count, dtype=np.int8)
    columns = [*job.INPUT_COLUMNS, *job.FEATURE_NAMES, "isFraud"]
    offset = 0
    sample = None

    for relative_path, expected_hash in sorted(files.items()):
        path = PARQUET / relative_path
        if job.sha256(path) != expected_hash:
            raise ValueError(f"{name}: checksum mismatch for {relative_path}")

        with pq.ParquetFile(path) as parquet_file:
            for batch in parquet_file.iter_batches(
                batch_size=job.CHUNK_SIZE,
                columns=columns,
                use_threads=False,
            ):
                if batch.num_rows == 0:
                    continue

                frame = batch.to_pandas()
                if sample is None:
                    sample = frame.loc[:, list(job.INPUT_COLUMNS)].head(20).copy()

                end = offset + len(frame)
                if end > count:
                    raise ValueError(f"{name}: too many rows.")

                values = frame.loc[:, list(job.FEATURE_NAMES)].to_numpy(
                    dtype=np.float64
                )
                target = frame["isFraud"].to_numpy()
                if not np.isfinite(values).all() or not np.isin(target, [0, 1]).all():
                    raise ValueError(f"{name}: invalid features or labels.")

                features[offset:end] = values
                labels[offset:end] = target
                offset = end

    if offset != count or int(labels.sum()) != metadata["fraud"]:
        raise ValueError(f"{name}: unexpected row or fraud count.")
    if sample is None or set(np.unique(labels)) != {0, 1}:
        raise ValueError(f"{name}: missing sample or class.")

    print(f"{name}: verified and loaded {offset:,} Parquet rows", flush=True)
    return features, labels, sample


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
        # The split fingerprint hashes the file-checksum list, not a CSV.
        fingerprint = hashlib.sha256(
            json.dumps(files, sort_keys=True).encode("utf-8")
        ).hexdigest()
        manifest["splits"][name] = {
            **report["splits"][name],
            "files": files,
            "sha256": fingerprint,
        }

    job.OUTPUT = OUTPUT
    job.REPORT = OUTPUT / "validation.json"
    with job.threadpool_limits(limits=2):
        job.main(manifest=manifest, split_loader=load_parquet_split)


if __name__ == "__main__":
    main()
