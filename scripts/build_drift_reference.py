"""Build aggregate drift reference distributions from Spark training data."""

import json

import train_logreg as job
import pyarrow.parquet as pq
from fraudshield.features import INPUT_COLUMNS, TRANSACTION_TYPES
from train_logreg_dvc import PREPARED

ROOT = job.ROOT
OUTPUT = ROOT / "reports/drift_reference.json"

# Bucket boundaries are inclusive on the lower side of each interval.
# Zero has its own bucket; the final bucket has no upper limit.
CUTS = [0.01, 100, 1_000, 10_000, 100_000, 1_000_000]
NUMERIC = ("amount", "oldbalanceOrg")


def main():
    if OUTPUT.exists():
        raise SystemExit(f"STOP: reference already exists: {OUTPUT}")

    prepared = json.loads((PREPARED / "spark_preparation.json").read_text())
    if prepared["status"] != "PASS":
        raise ValueError("Spark preparation did not pass.")

    parquet = PREPARED / "parquet"
    files = {
        name: digest
        for name, digest in prepared["parquet_sha256"].items()
        if name.startswith("split=train/")
    }
    actual = {
        path.relative_to(parquet).as_posix()
        for path in (parquet / "split=train").glob("*.parquet")
    }
    if not files or actual != set(files):
        raise ValueError("Training files differ from the Spark report.")

    types = {name: 0 for name in TRANSACTION_TYPES}
    counts = {
        name: job.np.zeros(len(CUTS) + 1, dtype=job.np.int64)
        for name in NUMERIC
    }
    rows = 0

    for name, digest in sorted(files.items()):
        path = parquet / name
        if job.sha256(path) != digest:
            raise ValueError(f"Training file checksum mismatch: {name}")

        with pq.ParquetFile(path) as source:
            for batch in source.iter_batches(
                batch_size=100_000, columns=list(INPUT_COLUMNS), use_threads=False
            ):
                frame = batch.to_pandas()
                if not frame["type"].isin(TRANSACTION_TYPES).all():
                    raise ValueError("Unknown or missing transaction type.")
                for kind, count in frame["type"].value_counts().items():
                    types[kind] += int(count)

                for column in NUMERIC:
                    values = frame[column].to_numpy(dtype=job.np.float64)
                    if not job.np.isfinite(values).all() or (values < 0).any():
                        raise ValueError(f"Invalid training values for {column}.")
                    buckets = job.np.searchsorted(CUTS, values, side="right")
                    counts[column] += job.np.bincount(
                        buckets, minlength=len(CUTS) + 1
                    )

                rows += len(frame)

    if (
        rows != prepared["splits"]["train"]["rows"]
        or sum(types.values()) != rows
        or any(int(values.sum()) != rows for values in counts.values())
    ):
        raise ValueError("Reference counts do not match the training split.")

    report = {
        "schema_version": 1,
        "reference_split": "train",
        "source_sha256": prepared["source_sha256"],
        "train_rows": rows,
        "transaction_type_counts": types,
        "numeric": {
            column: {
                "cut_points": CUTS,
                "counts": counts[column].tolist(),
            }
            for column in NUMERIC
        },
    }
    with OUTPUT.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    print(json.dumps(report, indent=2))
    print("PASS: training-only drift reference; all bucket totals match.")


if __name__ == "__main__":
    main()
