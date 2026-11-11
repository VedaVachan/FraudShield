"""MLflow wrapper preserving the fitted pipeline and decision threshold."""

import hashlib
from io import BytesIO
from pathlib import Path

import joblib
import mlflow.pyfunc
import numpy as np
import pandas as pd


class FraudShieldModel(mlflow.pyfunc.PythonModel):
    """MLflow model that scores with the verified pipeline and returns the decision."""
    def __init__(self, expected_sha256):
        """Store the SHA-256 the model bundle must have."""
        self.expected_sha256 = expected_sha256

    def load_context(self, context):
        """Verify the bundle checksum, then load the pipeline and threshold."""
        path = Path(context.artifacts["bundle"])
        payload = path.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        if actual != self.expected_sha256:
            raise ValueError("Model bundle checksum mismatch.")

        bundle = joblib.load(BytesIO(payload))
        self.pipeline = bundle["pipeline"]
        self.threshold = float(bundle["threshold"])
        if not np.isfinite(self.threshold) or not 0 <= self.threshold <= 1:
            raise ValueError("Invalid classification threshold.")
        self.model_sha256 = actual

    def predict(self, context, model_input, params=None):
        """Return score, threshold and decision for each input row."""
        scores = self.pipeline.predict_proba(model_input)[:, 1]
        if not np.isfinite(scores).all():
            raise ValueError("Nonfinite model scores.")

        return pd.DataFrame({
            "score": scores.astype(float),
            "threshold": np.full(len(scores), self.threshold),
            "predicted_fraud": scores >= self.threshold,
        }, index=model_input.index)


class IdentifiedFraudShieldModel(FraudShieldModel):
    """Include the verified bundle hash in every prediction row."""

    def predict(self, context, model_input, params=None):
        """Return the base prediction with the verified bundle hash added to each row."""
        result = super().predict(context, model_input, params)
        result["model_sha256"] = self.model_sha256
        return result
