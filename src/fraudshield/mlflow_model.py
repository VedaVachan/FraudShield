"""MLflow wrapper preserving the fitted pipeline and decision threshold."""

import hashlib
from pathlib import Path

import joblib
import mlflow.pyfunc
import numpy as np
import pandas as pd


class FraudShieldModel(mlflow.pyfunc.PythonModel):
    def __init__(self, expected_sha256):
        self.expected_sha256 = expected_sha256

    def load_context(self, context):
        path = Path(context.artifacts["bundle"])
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != self.expected_sha256:
            raise ValueError("Model bundle checksum mismatch.")

        bundle = joblib.load(path)
        self.pipeline = bundle["pipeline"]
        self.threshold = float(bundle["threshold"])
        if not np.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("Invalid classification threshold.")

    def predict(self, context, model_input, params=None):
        scores = self.pipeline.predict_proba(model_input)[:, 1]
        if not np.isfinite(scores).all():
            raise ValueError("Nonfinite model scores.")

        return pd.DataFrame({
            "score": scores.astype(float),
            "threshold": np.full(len(scores), self.threshold),
            "predicted_fraud": scores >= self.threshold,
        }, index=model_input.index)
