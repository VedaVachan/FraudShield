"""Native XGBoost SHAP explanations for the deployed prediction trees."""

import numpy as np
import xgboost as xgb
from scipy.special import expit

from fraudshield.features import FEATURE_NAMES, build_features


class FraudExplainer:
    """Create once per loaded model, then reuse for explanation requests."""

    def __init__(self, pipeline):
        """Extract the best-iteration booster used to compute SHAP contributions."""
        self.pipeline = pipeline
        classifier = pipeline.named_steps["classifier"]
        self.tree_count = int(classifier.best_iteration) + 1

        # Booster.predict otherwise uses all trees, including those trained
        # after the best iteration during early stopping.
        self.booster = classifier.get_booster()[:self.tree_count]
        self.booster.set_param({"nthread": 2})

    def explain(self, frame):
        """Return SHAP contributions, base values, margins and scores for the rows."""
        if len(frame) == 0:
            raise ValueError("At least one transaction is required.")

        features = build_features(frame)
        matrix = xgb.DMatrix(features, nthread=2)

        contributions = self.booster.predict(
            matrix, pred_contribs=True, approx_contribs=False
        )
        margins = self.booster.predict(matrix, output_margin=True)
        scores = self.pipeline.predict_proba(frame)[:, 1]

        if contributions.shape != (len(frame), len(FEATURE_NAMES) + 1):
            raise ValueError("Unexpected contribution shape.")
        if not np.isfinite(contributions).all():
            raise ValueError("Nonfinite SHAP contributions.")

        # These checks ensure explanations match the actual prediction model.
        np.testing.assert_allclose(
            contributions.sum(axis=1),
            margins,
            rtol=1e-5,
            atol=1e-4,
        )
        np.testing.assert_allclose(
            expit(margins),
            scores,
            rtol=1e-5,
            atol=1e-6,
        )

        return {
            "feature_names": list(FEATURE_NAMES),
            "contributions": contributions[:, :-1],
            "base_values": contributions[:, -1],
            "raw_margins": margins,
            "scores": scores,
        }
