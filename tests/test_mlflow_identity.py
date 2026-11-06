"""Test verified-byte loading and model identity."""
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd

from fraudshield.mlflow_model import (
    FraudShieldModel, IdentifiedFraudShieldModel,
)


class FixedPipeline:
    def predict_proba(self, frame):
        return np.tile([0.25, 0.75], (len(frame), 1))


class ModelIdentityTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "model.joblib"
        joblib.dump(
            {"pipeline": FixedPipeline(), "threshold": 0.7}, self.path
        )
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.context = SimpleNamespace(artifacts={"bundle": str(self.path)})
        self.frame = pd.DataFrame({"example": [1, 2]})

    def test_checksum_mismatch_prevents_loading(self):
        model = IdentifiedFraudShieldModel("0" * 64)
        with patch("fraudshield.mlflow_model.joblib.load") as loader:
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                model.load_context(self.context)
            loader.assert_not_called()

    def test_loads_verified_bytes_after_file_replacement(self):
        model = IdentifiedFraudShieldModel(self.digest)
        original_load = joblib.load

        def replace_then_load(stream):
            self.path.write_bytes(b"replaced after checksum")
            return original_load(stream)

        with patch("fraudshield.mlflow_model.joblib.load",
                   side_effect=replace_then_load):
            model.load_context(self.context)
        result = model.predict(None, self.frame)
        self.assertEqual(result["score"].tolist(), [0.75, 0.75])
        self.assertEqual(result["model_sha256"].tolist(), [self.digest] * 2)

    def test_identity_preserves_legacy_predictions(self):
        legacy = FraudShieldModel(self.digest)
        identified = IdentifiedFraudShieldModel(self.digest)
        legacy.load_context(self.context)
        identified.load_context(self.context)
        expected = legacy.predict(None, self.frame)
        actual = identified.predict(None, self.frame)
        self.assertEqual(
            list(expected.columns), ["score", "threshold", "predicted_fraud"]
        )
        pd.testing.assert_frame_equal(
            actual.drop(columns=["model_sha256"]), expected
        )
        self.assertEqual(actual["model_sha256"].tolist(), [self.digest] * 2)


if __name__ == "__main__":
    unittest.main()
