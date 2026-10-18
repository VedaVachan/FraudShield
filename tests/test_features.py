import unittest

import numpy as np
import pandas as pd

from fraudshield.features import build_features, FEATURE_NAMES


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({
            "type": ["TRANSFER", "PAYMENT"],
            "amount": [100.0, 0.0],
            "oldbalanceOrg": [50.0, 0.0],
        })

    def test_known_values_and_zero_balance(self):
        result = build_features(self.frame)
        self.assertEqual(result.shape, (2, len(FEATURE_NAMES)))
        np.testing.assert_allclose(
            result[0, :4],
            [np.log1p(100), np.log1p(50), 100 / 151, 1],
        )
        np.testing.assert_array_equal(result[1, :4], [0, 0, 0, 0])
        np.testing.assert_array_equal(result[:, 4:].sum(axis=1), [1, 1])
        self.assertEqual(result[0, FEATURE_NAMES.index("type_TRANSFER")], 1)
        self.assertEqual(result[1, FEATURE_NAMES.index("type_PAYMENT")], 1)

    def test_rejects_leakage_columns(self):
        for column in ("isFraud", "step", "newbalanceOrig"):
            with self.subTest(column=column):
                with self.assertRaises(ValueError):
                    build_features(self.frame.assign(**{column: 1}))

    def test_rejects_invalid_financial_values(self):
        for column in ("amount", "oldbalanceOrg"):
            for value in (-1, np.nan, np.inf, -np.inf, "invalid"):
                with self.subTest(column=column, value=value):
                    frame = self.frame.copy().astype({column: object})
                    frame.loc[0, column] = value
                    with self.assertRaises(ValueError):
                        build_features(frame)

    def test_rejects_unknown_type_and_missing_column(self):
        with self.assertRaises(ValueError):
            build_features(self.frame.assign(type="UNKNOWN"))
        with self.assertRaises(ValueError):
            build_features(self.frame.drop(columns="amount"))

    def test_column_order_and_input_preservation(self):
        original = self.frame.copy(deep=True)
        expected = build_features(self.frame)
        actual = build_features(self.frame[list(reversed(self.frame.columns))])
        np.testing.assert_array_equal(actual, expected)
        pd.testing.assert_frame_equal(self.frame, original)

    def test_large_finite_values(self):
        frame = self.frame.copy()
        frame.loc[0, ["amount", "oldbalanceOrg"]] = 1e308
        result = build_features(frame)
        self.assertTrue(np.isfinite(result).all())
        self.assertAlmostEqual(result[0, 2], 0.5)


if __name__ == "__main__":
    unittest.main()
