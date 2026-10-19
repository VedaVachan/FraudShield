"""Check Spark feature parity and invalid-input rejection."""

import unittest

import numpy as np
import pandas as pd
from pyspark.sql import SparkSession, functions as F

from fraudshield.features import (
    FEATURE_NAMES,
    INPUT_COLUMNS,
    TRANSACTION_TYPES,
    build_features,
)
from fraudshield.spark_features import build_spark_features


class SparkFeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spark = (
            SparkSession.builder
            .appName("FraudShield-Spark-Feature-Tests")
            .master("local[2]")
            .config("spark.driver.bindAddress", "127.0.0.1")
            .config("spark.driver.host", "127.0.0.1")
            .config("spark.ui.enabled", "false")
            .config("spark.sql.shuffle.partitions", "2")
            .config("spark.sql.ansi.enabled", "false")
            .getOrCreate()
        )
        cls.spark.sparkContext.setLogLevel("WARN")

    @classmethod
    def tearDownClass(cls):
        cls.spark.stop()

    def frame(self, rows):
        return self.spark.createDataFrame(
            rows,
            "type string, amount double, oldbalanceOrg double",
        )

    def test_parity_all_types_and_edge_values(self):
        rows = [
            (kind, amount, balance)
            for kind in TRANSACTION_TYPES
            for amount, balance in (
                (0.0, 0.0),
                (100.0, 0.0),
                (0.0, 100.0),
                (100.0, 50.0),
                (100.0, 100.0),
                (1e-12, 1e-12),
                (1e308, 1e308),
                (1e308, 1.0),
            )
        ]
        expected = build_features(
            pd.DataFrame(rows, columns=INPUT_COLUMNS)
        )
        result = build_spark_features(self.frame(rows))

        self.assertEqual(result.columns, list(FEATURE_NAMES))
        actual = np.array([tuple(row) for row in result.collect()])
        self.assertTrue(np.isfinite(actual).all())

        # Spark row order is not guaranteed. Compare sorted feature rows.
        actual = np.array(sorted(map(tuple, actual)))
        expected = np.array(sorted(map(tuple, expected)))
        np.testing.assert_allclose(
            actual, expected, rtol=1e-12, atol=1e-12
        )

    def test_rejects_invalid_financial_values(self):
        for column_index in (1, 2):
            for value in (-1.0, float("nan"), float("inf"),
                          float("-inf"), None):
                with self.subTest(column=column_index, value=value):
                    row = ["PAYMENT", 100.0, 500.0]
                    row[column_index] = value
                    with self.assertRaises(ValueError):
                        build_spark_features(self.frame([tuple(row)]))

    def test_rejects_invalid_types(self):
        for kind in ("UNKNOWN", None):
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError):
                    build_spark_features(
                        self.frame([(kind, 100.0, 500.0)])
                    )

    def test_rejects_extra_and_missing_columns(self):
        frame = self.frame([("PAYMENT", 100.0, 500.0)])
        for column in ("isFraud", "step", "newbalanceOrig"):
            with self.subTest(column=column):
                with self.assertRaises(ValueError):
                    build_spark_features(
                        frame.withColumn(column, F.lit(1))
                    )

        with self.assertRaises(ValueError):
            build_spark_features(frame.drop("amount"))


if __name__ == "__main__":
    unittest.main()
