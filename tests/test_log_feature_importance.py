"""Test feature-importance logging with a tiny temporary model and MLflow store."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb
from mlflow.tracking import MlflowClient
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer

from fraudshield.features import FEATURE_NAMES, TRANSACTION_TYPES, build_features


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "log_feature_importance_under_test", ROOT / "scripts/log_feature_importance.py"
)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def write_tiny_model(path):
    """Fit a small classifier on synthetic rows and save it like the real bundle."""
    rng = np.random.default_rng(7)
    count = 400
    frame = pd.DataFrame({
        "type": rng.choice(TRANSACTION_TYPES, count),
        "amount": rng.uniform(1, 100000, count),
        "oldbalanceOrg": rng.uniform(0, 100000, count),
    })
    labels = (frame["amount"] > frame["amount"].median()).astype(int).to_numpy()
    classifier = xgb.XGBClassifier(
        n_estimators=6, max_depth=2, learning_rate=0.3, tree_method="hist",
        n_jobs=1, random_state=0,
    )
    classifier.fit(build_features(frame), labels)
    pipeline = Pipeline([
        ("features", FunctionTransformer(build_features, validate=False)),
        ("classifier", classifier),
    ])
    joblib.dump({"pipeline": pipeline, "threshold": 0.5}, path)


class FeatureImportanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One MLflow database is shared: creating it runs many migrations.
        cls.shared = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.shared.cleanup)
        cls.shared_uri = f"sqlite:///{Path(cls.shared.name) / 'mlflow.db'}"

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.model = self.folder / "model.joblib"
        write_tiny_model(self.model)
        self.model_hash = script.sha256_file(self.model)
        # Refusal tests use a path that must stay absent.
        self.database = self.folder / "unused.db"
        self.uri = self.shared_uri
        self.experiment = f"Importance-{self._testMethodName}"
        self.artifacts = self.folder / "artifacts"

    def log(self, **overrides):
        arguments = dict(
            model_path=self.model, expected_sha256=self.model_hash,
            tracking_uri=self.uri, experiment=self.experiment,
            artifact_location=self.artifacts.as_uri(), root=ROOT,
        )
        arguments.update(overrides)
        return script.log_feature_importance(**arguments)

    def test_importance_covers_the_nine_features_and_sums_to_one(self):
        record = script.gain_importance(
            script.load_classifier(self.model), self.model_hash
        )
        features = record["features"]
        self.assertEqual([item["feature"] for item in features], list(FEATURE_NAMES))
        self.assertEqual(len(features), 9)
        self.assertAlmostEqual(sum(item["share"] for item in features), 1.0, places=9)
        self.assertTrue(all(item["gain"] >= 0 for item in features))
        self.assertEqual(sorted(item["rank"] for item in features), list(range(1, 10)))
        top = min(features, key=lambda item: item["rank"])["feature"]
        self.assertIn(
            top, {"log_amount", "amount_balance_fraction", "amount_exceeds_balance"}
        )
        self.assertEqual(record["importance_type"], "gain")
        self.assertEqual(record["model_sha256"], self.model_hash)

    def test_wrong_hash_is_refused_before_anything_is_logged(self):
        with self.assertRaises(script.ModelIdentityError):
            self.log(
                expected_sha256="0" * 64, tracking_uri=f"sqlite:///{self.database}"
            )
        self.assertFalse(self.database.exists())
        self.assertFalse(self.artifacts.exists())

    def test_command_line_refuses_wrong_hash_with_exit_code_2(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = script.main([
                "--model", str(self.model), "--expected-sha256", "f" * 64,
                "--tracking-uri", f"sqlite:///{self.database}",
            ])
        self.assertEqual(code, 2)
        self.assertIn("STOP: Model checksum differs", stderr.getvalue())
        self.assertFalse(self.database.exists())

    def test_command_line_requires_a_hash_for_any_other_model(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                script.main([
                    "--model", str(self.model),
                    "--tracking-uri", f"sqlite:///{self.database}",
                ])
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.database.exists())

    def test_logs_one_new_analysis_run_without_touching_model_or_registry(self):
        receipt = self.log()
        client = MlflowClient(tracking_uri=self.uri)
        run = client.get_run(receipt["run_id"])
        self.assertEqual(run.info.status, "FINISHED")
        self.assertEqual(run.data.tags["run_type"], "analysis")
        self.assertEqual(run.data.tags["model_sha256"], self.model_hash)
        self.assertRegex(run.data.tags["git_commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(run.data.tags["retraining"], "none")
        self.assertEqual(run.data.tags["test_data_used"], "false")
        shares = {
            key: value for key, value in run.data.metrics.items()
            if key.startswith("gain_share_")
        }
        self.assertEqual(
            sorted(shares), sorted(f"gain_share_{name}" for name in FEATURE_NAMES)
        )
        self.assertAlmostEqual(sum(shares.values()), 1.0, places=9)

        names = {item.path for item in client.list_artifacts(receipt["run_id"])}
        self.assertIn("feature_importance.json", names)
        self.assertEqual(receipt["registered_or_promoted"], False)
        self.assertEqual(len(client.search_runs(
            [client.get_experiment_by_name(self.experiment).experiment_id]
        )), 1)
        self.assertEqual(list(client.search_registered_models()), [])
        self.assertEqual(script.sha256_file(self.model), self.model_hash)

        downloaded = client.download_artifacts(
            receipt["run_id"], "feature_importance.json", dst_path=str(self.folder)
        )
        self.assertEqual(
            json.loads(Path(downloaded).read_text())["model_sha256"], self.model_hash
        )

    def test_chart_is_logged_when_matplotlib_is_available(self):
        receipt = self.log()
        self.assertTrue(receipt["chart_logged"])
        names = {
            item.path for item in
            MlflowClient(tracking_uri=self.uri).list_artifacts(receipt["run_id"])
        }
        self.assertIn("feature_importance.png", names)

    def test_json_only_when_matplotlib_is_missing(self):
        with patch.dict(sys.modules, {"matplotlib": None, "matplotlib.pyplot": None}):
            receipt = self.log()
        self.assertFalse(receipt["chart_logged"])
        client = MlflowClient(tracking_uri=self.uri)
        names = {item.path for item in client.list_artifacts(receipt["run_id"])}
        self.assertEqual(names, {"feature_importance.json"})
        self.assertEqual(
            client.get_run(receipt["run_id"]).data.tags["feature_importance_chart"],
            "unavailable",
        )

    def test_receipt_is_written_once_and_never_overwritten(self):
        receipt_path = self.folder / "receipt.json"
        first = self.log(receipt_path=receipt_path)
        self.assertEqual(json.loads(receipt_path.read_text())["run_id"], first["run_id"])
        with self.assertRaises(FileExistsError):
            self.log(receipt_path=receipt_path)
        client = MlflowClient(tracking_uri=self.uri)
        experiment = client.get_experiment_by_name(self.experiment)
        self.assertEqual(len(client.search_runs([experiment.experiment_id])), 1)


if __name__ == "__main__":
    unittest.main()
