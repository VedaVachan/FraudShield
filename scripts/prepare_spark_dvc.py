"""Run Spark preparation with separate DVC-managed outputs."""

import os
import sys

import prepare_spark as job


def main():
    output = job.ROOT / "data/processed/paysim_spark_dvc_v1"

    os.environ["SPARK_LOCAL_IP"] = "127.0.0.1"
    os.environ["PYSPARK_PYTHON"] = sys.executable
    os.environ["PYSPARK_SUBMIT_ARGS"] = "--driver-memory 1g pyspark-shell"

    job.OUTPUT = output / "parquet"
    job.TEMP = output / "parquet.tmp"
    job.REPORT = output / "spark_preparation.json"
    job.main()


if __name__ == "__main__":
    main()
