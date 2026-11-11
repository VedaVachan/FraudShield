"""Test registry alias promotion and rollback on a throwaway tracking database."""

import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mlflow.tracking import MlflowClient


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "promote_model_under_test", ROOT / "scripts/promote_model.py"
)
promote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promote)

GOOD = {
    "validation_average_precision": 0.95,
    "validation_precision": 0.93,
    "validation_recall": 1.0,
}


class PromoteModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One database for the class: creating it runs many MLflow migrations.
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.uri = f"sqlite:///{Path(cls.folder.name) / 'registry.db'}"
        cls.client = MlflowClient(tracking_uri=cls.uri)
        cls.experiment = cls.client.create_experiment(
            "promotion-tests",
            artifact_location=(Path(cls.folder.name) / "artifacts").as_uri(),
        )

    def setUp(self):
        self.name = f"Model-{self._testMethodName}"
        self.client.create_registered_model(self.name)
        self.sources = 0

    def version(self, metrics=None, test_evaluated="false", via_source=False):
        """Register a version whose run (or its source run) holds the evidence."""
        metrics = dict(GOOD if metrics is None else metrics)
        tags = {} if test_evaluated is None else {"test_evaluated": test_evaluated}
        holder = self.client.create_run(self.experiment, tags=tags)
        for key, value in metrics.items():
            self.client.log_metric(holder.info.run_id, key, value)
        self.client.set_terminated(holder.info.run_id, "FINISHED")
        run_id = holder.info.run_id
        if via_source:
            packaging = self.client.create_run(
                self.experiment, tags={"source_run_id": holder.info.run_id}
            )
            self.client.set_terminated(packaging.info.run_id, "FINISHED")
            run_id = packaging.info.run_id
        self.sources += 1
        created = self.client.create_model_version(
            self.name, source=f"file:///nowhere/{self.name}/{self.sources}",
            run_id=run_id,
        )
        return int(created.version)

    def aliases(self):
        return {
            key: str(value) for key, value in
            dict(self.client.get_registered_model(self.name).aliases).items()
        }

    def tags(self):
        return dict(self.client.get_registered_model(self.name).tags)

    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = promote.main([
                "--name", self.name, "--tracking-uri", self.uri, *arguments,
            ])
        text = out.getvalue()
        return code, (json.loads(text) if text.strip() else None), err.getvalue()

    def set_champion(self, version):
        self.client.set_registered_model_alias(self.name, "champion", str(version))

    def test_dry_run_is_the_default_and_changes_nothing(self):
        first, second = self.version(), self.version({**GOOD, "validation_average_precision": 0.97})
        self.set_champion(first)
        code, plan, err = self.cli("--candidate-version", str(second))
        self.assertEqual(code, 0)
        self.assertTrue(plan["gate_passed"])
        self.assertTrue(plan["dry_run"])
        self.assertFalse(plan["changed"])
        self.assertIn("DRY RUN", err)
        self.assertEqual(self.aliases(), {"champion": str(first)})
        self.assertNotIn("previous_champion_version", self.tags())

    def test_apply_moves_the_alias_and_records_the_previous_version(self):
        first, second = self.version(), self.version({**GOOD, "validation_average_precision": 0.97})
        self.set_champion(first)
        code, plan, _ = self.cli("--candidate-version", str(second), "--apply")
        self.assertEqual(code, 0)
        self.assertTrue(plan["changed"])
        self.assertEqual(self.aliases(), {"champion": str(second)})
        self.assertEqual(self.tags()["previous_champion_version"], str(first))

    def test_a_candidate_that_is_not_better_is_refused_even_with_apply(self):
        first = self.version()
        worse = self.version({**GOOD, "validation_average_precision": 0.90})
        equal = self.version()
        self.set_champion(first)
        for candidate in (worse, equal):
            code, plan, _ = self.cli("--candidate-version", str(candidate), "--apply")
            self.assertEqual(code, 1)
            self.assertFalse(plan["changed"])
            self.assertTrue(any("not above the current champion" in r for r in plan["reasons"]))
        self.assertEqual(self.aliases(), {"champion": str(first)})

    def test_required_improvement_is_enforced(self):
        first = self.version()
        slightly = self.version({**GOOD, "validation_average_precision": 0.951})
        self.set_champion(first)
        code, plan, _ = self.cli(
            "--candidate-version", str(slightly), "--min-improvement", "0.01"
        )
        self.assertEqual(code, 1)
        code, plan, _ = self.cli(
            "--candidate-version", str(slightly), "--min-improvement", "0.0005"
        )
        self.assertEqual(code, 0)

    def test_floors_are_enforced_even_if_better_than_the_champion(self):
        first = self.version({**GOOD, "validation_average_precision": 0.86})
        low_precision = self.version({
            "validation_average_precision": 0.99,
            "validation_precision": 0.5, "validation_recall": 1.0,
        })
        self.set_champion(first)
        code, plan, _ = self.cli("--candidate-version", str(low_precision), "--apply")
        self.assertEqual(code, 1)
        self.assertTrue(any("validation_precision" in r for r in plan["reasons"]))
        self.assertEqual(self.aliases(), {"champion": str(first)})

    def test_missing_metrics_are_refused(self):
        candidate = self.version({"validation_average_precision": 0.99})
        code, plan, _ = self.cli("--candidate-version", str(candidate), "--apply")
        self.assertEqual(code, 1)
        self.assertTrue(any("Missing metric validation_recall" in r for r in plan["reasons"]))
        self.assertEqual(self.aliases(), {})

    def test_test_set_evidence_is_refused(self):
        opened = self.version(test_evaluated="true")
        unknown = self.version(test_evaluated=None)
        code, plan, _ = self.cli("--candidate-version", str(opened), "--apply")
        self.assertEqual(code, 1)
        self.assertIn("The test split was opened for this version.", plan["reasons"])
        code, plan, _ = self.cli("--candidate-version", str(unknown), "--apply")
        self.assertEqual(code, 1)
        self.assertTrue(any("validation-only evidence" in r for r in plan["reasons"]))
        self.assertEqual(self.aliases(), {})

    def test_evidence_is_found_through_the_source_run_tag(self):
        candidate = self.version(via_source=True)
        code, plan, _ = self.cli("--candidate-version", str(candidate))
        self.assertEqual(code, 0)
        self.assertEqual(len(plan["candidate_evidence"]["run_ids"]), 2)
        self.assertEqual(plan["candidate_evidence"]["test_evaluated"], "false")

    def test_first_champion_needs_only_the_floors_and_has_no_previous(self):
        candidate = self.version()
        code, plan, _ = self.cli("--candidate-version", str(candidate), "--apply")
        self.assertEqual(code, 0)
        self.assertIsNone(plan["current_version"])
        self.assertEqual(self.aliases(), {"champion": str(candidate)})
        self.assertNotIn("previous_champion_version", self.tags())

    def test_challenger_needs_only_the_floors_not_a_comparison(self):
        champion = self.version({**GOOD, "validation_average_precision": 0.99})
        challenger = self.version({**GOOD, "validation_average_precision": 0.90})
        self.set_champion(champion)
        code, plan, _ = self.cli(
            "--alias", "challenger", "--candidate-version", str(challenger), "--apply"
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self.aliases(), {"champion": str(champion), "challenger": str(challenger)}
        )

    def test_rollback_restores_the_previous_alias_and_can_be_repeated(self):
        first, second = self.version(), self.version({**GOOD, "validation_average_precision": 0.97})
        self.set_champion(first)
        self.cli("--candidate-version", str(second), "--apply")

        code, plan, _ = self.cli("--rollback")
        self.assertEqual((code, plan["dry_run"], plan["changed"]), (0, True, False))
        self.assertEqual(plan["restore_version"], str(first))
        self.assertEqual(self.aliases(), {"champion": str(second)})

        code, plan, _ = self.cli("--rollback", "--apply")
        self.assertEqual((code, plan["changed"]), (0, True))
        self.assertEqual(self.aliases(), {"champion": str(first)})
        self.assertEqual(self.tags()["previous_champion_version"], str(second))

        self.cli("--rollback", "--apply")
        self.assertEqual(self.aliases(), {"champion": str(second)})

    def test_rollback_without_a_recorded_previous_version_stops(self):
        self.set_champion(self.version())
        code, plan, err = self.cli("--rollback", "--apply")
        self.assertEqual(code, 2)
        self.assertIsNone(plan)
        self.assertIn("No previous champion version is recorded", err)

    def test_unknown_model_or_version_stops_with_exit_code_2(self):
        code, _, err = self.cli("--candidate-version", "99")
        self.assertEqual(code, 2)
        self.assertIn("Model version not found", err)
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            code = promote.main([
                "--name", "No-Such-Model", "--tracking-uri", self.uri,
                "--candidate-version", "1",
            ])
        self.assertEqual(code, 2)
        self.assertIn("Registered model not found", captured.getvalue())

    def test_promoting_the_version_that_already_holds_the_alias_is_refused(self):
        first = self.version()
        self.set_champion(first)
        code, plan, _ = self.cli("--candidate-version", str(first), "--apply")
        self.assertEqual(code, 1)
        self.assertFalse(plan["changed"])

    def test_missing_default_database_stops_without_creating_it(self):
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "tracking.db"
            with patch.object(promote, "TRACKING_DATABASE", missing):
                with contextlib.redirect_stderr(io.StringIO()) as captured:
                    code = promote.main(["--candidate-version", "1"])
            self.assertEqual(code, 2)
            self.assertIn("MLflow database is missing", captured.getvalue())
            self.assertFalse(missing.exists())

    def test_the_default_target_is_the_project_database_not_the_test_one(self):
        self.assertEqual(promote.TRACKING_DATABASE, ROOT / "runtime/mlflow/tracking.db")
        self.assertNotIn(self.folder.name, str(promote.TRACKING_DATABASE))


if __name__ == "__main__":
    unittest.main()
