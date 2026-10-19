"""Validate PaySim and write chronological Spark feature datasets."""

import csv
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pandas as pd
from pyspark.sql import SparkSession, functions as F
from pyspark.sql.types import StringType, StructField, StructType

from fraudshield.features import (
    FEATURE_NAMES,
    INPUT_COLUMNS,
    TRANSACTION_TYPES,
    build_features,
)
from fraudshield.spark_features import feature_expressions

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/paysim.csv"
OUTPUT = ROOT / "data/processed/paysim_spark_v1"
TEMP = ROOT / "data/processed/paysim_spark_v1.tmp"
REPORT = ROOT / "reports/spark_preparation.json"

EXPECTED_HASH = (
    "16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b"
)
COLUMNS = [
    "step", "type", "amount", "nameOrig", "oldbalanceOrg",
    "newbalanceOrig", "nameDest", "oldbalanceDest",
    "newbalanceDest", "isFraud", "isFlaggedFraud",
]
NUMERIC = [
    "amount", "oldbalanceOrg", "newbalanceOrig",
    "oldbalanceDest", "newbalanceDest",
]
EXPECTED = {
    "train": (6082007, 5781, 1, 520),
    "validation": (191147, 1180, 521, 631),
    "test": (89466, 1252, 632, 743),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def invalid_number(value):
    return (
        value.isNull()
        | F.isnan(value)
        | (F.abs(value) == F.lit(float("inf")))
        | (value < 0)
    )


def split_summary(frame):
    rows = frame.groupBy("split").agg(
        F.count("*").alias("rows"),
        F.sum("isFraud").alias("fraud"),
        F.min("step").alias("step_min"),
        F.max("step").alias("step_max"),
    ).collect()

    result = {}
    for row in rows:
        name = row["split"]
        actual = tuple(
            int(row[key])
            for key in ("rows", "fraud", "step_min", "step_max")
        )
        if name not in EXPECTED or actual != EXPECTED[name]:
            raise ValueError(f"Unexpected split statistics: {name}: {actual}")
        result[name] = dict(zip(
            ("rows", "fraud", "step_min", "step_max"), actual
        ))

    if set(result) != set(EXPECTED):
        raise ValueError("Missing or unexpected split.")
    return result


def main():
    started = time.perf_counter()
    for path in (OUTPUT, TEMP, REPORT):
        if path.exists():
            raise SystemExit(f"STOP: path already exists: {path}")

    print("Verifying raw CSV checksum...", flush=True)
    if sha256(SOURCE) != EXPECTED_HASH:
        raise ValueError("Raw dataset checksum differs from validated source.")

    with SOURCE.open(newline="", encoding="utf-8") as handle:
        if next(csv.reader(handle), None) != COLUMNS:
            raise ValueError("Unexpected CSV header.")

    spark = (
        SparkSession.builder
        .appName("FraudShield-PaySim-Preparation")
        .master("local[2]")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.default.parallelism", "2")
        .config("spark.sql.ansi.enabled", "false")
        .config("spark.sql.csv.parser.columnPruning.enabled", "false")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")

    try:
        schema = StructType([
            StructField(name, StringType(), True) for name in COLUMNS
        ])
        raw = (
            spark.read.schema(schema)
            .option("header", "true")
            .option("enforceSchema", "false")
            .option("mode", "FAILFAST")
            .csv(str(SOURCE))
        )

        checks = {}
        for name in COLUMNS:
            value = F.col(name)
            checks[f"missing:{name}"] = (
                value.isNull() | (F.length(F.trim(value)) == 0)
            )

        checks["invalid_type"] = (
            F.col("type").isNull()
            | ~F.col("type").isin(*TRANSACTION_TYPES)
        )
        for name in NUMERIC:
            checks[f"invalid_numeric:{name}"] = invalid_number(
                F.col(name).cast("double")
            )

        step = F.col("step").cast("int")
        checks["invalid_step"] = (
            step.isNull()
            | ~F.col("step").rlike(r"^[0-9]+$")
            | ~step.between(1, 743)
        )
        for name in ("isFraud", "isFlaggedFraud"):
            checks[f"invalid_label:{name}"] = (
                F.col(name).isNull() | ~F.col(name).isin("0", "1")
            )

        print("Validating all raw columns...", flush=True)
        totals = raw.agg(
            F.count("*").alias("rows"),
            *[
                F.sum(
                    F.when(F.coalesce(condition, F.lit(True)), 1)
                    .otherwise(0)
                ).alias(name)
                for name, condition in checks.items()
            ],
        ).first().asDict()

        errors = {
            name: int(totals[name])
            for name in checks if totals[name]
        }
        if totals["rows"] != 6362620 or errors:
            raise ValueError(
                f"Validation failed: rows={totals['rows']}, errors={errors}"
            )

        inputs = raw.select(
            F.col("step").cast("int").alias("step"),
            "type",
            F.col("amount").cast("double").alias("amount"),
            F.col("oldbalanceOrg").cast("double").alias("oldbalanceOrg"),
            F.col("isFraud").cast("int").alias("isFraud"),
        )
        inputs = inputs.withColumn(
            "split",
            F.when(F.col("step") <= 520, "train")
            .when(F.col("step") <= 631, "validation")
            .otherwise("test"),
        )
        summary = split_summary(inputs)

        prepared = inputs.select("*", *feature_expressions())
        print("Writing chronological Parquet partitions...", flush=True)
        TEMP.parent.mkdir(parents=True, exist_ok=True)
        (
            prepared.repartition(4)
            .write.mode("errorifexists")
            .partitionBy("split")
            .parquet(str(TEMP))
        )

        print("Reading back Parquet and checking features...", flush=True)
        restored = spark.read.parquet(str(TEMP))
        if split_summary(restored) != summary:
            raise ValueError("Parquet readback statistics differ.")

        bad_features = restored.agg(*[
            F.sum(
                F.when(invalid_number(F.col(name)), 1).otherwise(0)
            ).alias(name)
            for name in FEATURE_NAMES
        ]).first().asDict()
        if any(bad_features.values()):
            raise ValueError(f"Invalid output features: {bad_features}")

        # Collect only a bounded training sample, not the full dataset.
        sample = (
            restored.filter(F.col("split") == "train")
            .select(*INPUT_COLUMNS, *FEATURE_NAMES)
            .limit(1000)
            .collect()
        )
        reference = build_features(pd.DataFrame(
            [{name: row[name] for name in INPUT_COLUMNS} for row in sample],
            columns=INPUT_COLUMNS,
        ))
        actual = np.array([
            [row[name] for name in FEATURE_NAMES] for row in sample
        ])
        np.testing.assert_allclose(
            actual, reference, rtol=1e-12, atol=1e-12
        )

        files = {
            str(path.relative_to(TEMP)): sha256(path)
            for path in sorted(TEMP.rglob("*.parquet"))
        }
        if not files:
            raise ValueError("No Parquet files produced.")

        report = {
            "status": "PASS",
            "source_sha256": EXPECTED_HASH,
            "spark_version": spark.version,
            "master": spark.sparkContext.master,
            "driver_memory": spark.sparkContext.getConf().get(
                "spark.driver.memory"
            ),
            "validation_errors": errors,
            "model_inputs": list(INPUT_COLUMNS),
            "feature_names": list(FEATURE_NAMES),
            "audit_columns": ["step", "split"],
            "target": "isFraud",
            "train_end_step": 520,
            "validation_end_step": 631,
            "splits": summary,
            "feature_parity_training_rows": len(sample),
            "feature_parity": "PASS",
            "parquet_readback": "PASS",
            "test_model_evaluated": False,
            "output": str(OUTPUT.relative_to(ROOT)),
            "parquet_sha256": files,
            "total_seconds": time.perf_counter() - started,
        }

        with (TEMP / "manifest.json").open("x") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")

        TEMP.rename(OUTPUT)
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        with REPORT.open("x") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")

        print(json.dumps(report, indent=2))
        print("PASS: Spark validation, feature parity, and Parquet readback.")
    finally:
        spark.stop()
        print("Spark stopped.")


if __name__ == "__main__":
    main()
