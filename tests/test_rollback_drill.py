"""Test the registry rollback drill on a throwaway database; the real one is never used."""

import contextlib
import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from mlflow.tracking import MlflowClient


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "rollback_drill_under_test", ROOT / "scripts/rollback_drill.py"
)
drill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drill)

GOOD = {
    "validation_average_precision": 0.95,
    "validation_precision": 0.93,
    "validation_recall": 1.0,
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_source(folder, metrics):
    """A throwaway registry holding FraudShield-XGBoost version 1."""
    database = Path(folder) / "source.db"
    client = MlflowClient(tracking_uri=f"sqlite:///{database}")
    experiment = client.create_experiment(
        "source", artifact_location=(Path(folder) / "artifacts").as_uri()
    )
    run = client.create_run(experiment, tags={"test_evaluated": "false"})
    for key, value in metrics.items():
        client.log_metric(run.info.run_id, key, value)
    client.set_terminated(run.info.run_id, "FINISHED")
    client.create_registered_model(drill.MODEL)
    client.create_model_version(
        drill.MODEL, source="file:///nowhere/v1", run_id=run.info.run_id
    )
    return database


class RollbackDrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Built once: MLflow creates its tables slowly.
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.source = make_source(cls.folder.name, GOOD)
        cls.source_hash = sha(cls.source)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / "reports/rollback_drill.json"

    def run_main(self, *arguments, source=None):
        out, err = io.StringIO(), io.StringIO()
        original = drill.REAL_DATABASE
        # The "real" database is the throwaway source, so its hash is checked too.
        drill.REAL_DATABASE = Path(source or self.source)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = drill.main([
                    "--source-database", str(source or self.source),
                    "--output", str(self.output), *arguments,
                ])
        finally:
            drill.REAL_DATABASE = original
        return code, out.getvalue(), err.getvalue()

    def report(self):
        return json.loads(self.output.read_text())

    def test_drill_passes_and_the_report_lists_every_step(self):
        code, out, _ = self.run_main()
        self.assertEqual(code, 0, out)
        report = self.report()
        self.assertTrue(report["passed"])
        self.assertEqual(
            [step["step"] for step in report["steps"]],
            ["reset champion on the copy", "a. register a second version",
             "b. promote v1 (apply)", "c. promote v2 (apply)", "d. roll back (apply)"],
        )
        for step in report["steps"]:
            self.assertTrue(step["passed"], step)
            self.assertLessEqual(step["started_utc"], step["finished_utc"])
        self.assertIn("Not a drill of what the API or gateway serve", report["scope"])

    def test_champion_moves_v1_then_v2_then_back_to_v1(self):
        self.run_main()
        steps = {step["step"][0]: step for step in self.report()["steps"]}
        self.assertEqual(steps["a"]["output"]["versions"], [1, 2])
        self.assertEqual(steps["b"]["registry_after"]["aliases"], {"champion": "1"})
        self.assertEqual(steps["c"]["registry_after"]["aliases"], {"champion": "2"})
        self.assertEqual(
            steps["c"]["registry_after"]["tags"], {"previous_champion_version": "1"}
        )
        self.assertEqual(steps["d"]["registry_after"]["aliases"], {"champion": "1"})
        self.assertEqual(steps["d"]["output"]["restore_version"], "1")
        self.assertTrue(all(step["exit_code"] == 0 for step in
                            (steps["b"], steps["c"], steps["d"])))

    def test_second_version_is_marked_synthetic_and_only_slightly_better(self):
        self.run_main()
        registered = next(
            s for s in self.report()["steps"] if s["step"].startswith("a.")
        )["output"]
        self.assertEqual(registered["registered_version"], "2")
        self.assertAlmostEqual(
            registered["synthetic_metrics"]["validation_average_precision"],
            GOOD["validation_average_precision"] + drill.IMPROVEMENT,
        )

    def test_source_database_is_byte_identical_and_hashes_are_recorded(self):
        self.run_main()
        self.assertEqual(sha(self.source), self.source_hash)
        recorded = self.report()["real_database"]
        self.assertEqual(recorded["sha256_before"], self.source_hash)
        self.assertEqual(recorded["sha256_after"], self.source_hash)
        self.assertTrue(recorded["unchanged"])

    def test_report_is_create_only(self):
        self.assertEqual(self.run_main()[0], 0)
        first = self.output.read_bytes()
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("never overwritten", err)
        self.assertEqual(self.output.read_bytes(), first)

    def test_refuses_a_work_folder_that_holds_the_real_database(self):
        real_folder = str(self.source.parent)
        code, _, err = self.run_main("--work-dir", real_folder)
        self.assertEqual(code, 2)
        self.assertIn("real MLflow database", err)
        self.assertFalse(self.output.exists())
        self.assertEqual(sha(self.source), self.source_hash)

    def test_refuses_the_projects_real_database_folder_without_touching_it(self):
        self.assertTrue(drill.REAL_DATABASE.parent.name == "mlflow")
        with self.assertRaises(drill.DrillRefused):
            drill.refuse_real_database(drill.REAL_DATABASE.parent, drill.REAL_DATABASE)

    def test_refuses_to_overwrite_an_existing_copy(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / "tracking.db").write_bytes(b"already here")
        code, _, err = self.run_main("--work-dir", folder.name)
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        self.assertEqual((Path(folder.name) / "tracking.db").read_bytes(), b"already here")

    def test_missing_source_database_stops_cleanly(self):
        code, _, err = self.run_main(source=Path(self.folder.name) / "absent.db")
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        self.assertFalse(self.output.exists())

    def test_a_failing_step_gives_exit_1_and_still_writes_the_report(self):
        weak = tempfile.TemporaryDirectory()
        self.addCleanup(weak.cleanup)
        source = make_source(weak.name, {
            "validation_average_precision": 0.5,
            "validation_precision": 0.9,
            "validation_recall": 1.0,
        })
        code, _, _ = self.run_main(source=source)
        self.assertEqual(code, 1)
        report = self.report()
        self.assertFalse(report["passed"])
        failed = [s["step"] for s in report["steps"] if not s["passed"]]
        self.assertIn("b. promote v1 (apply)", failed)
        self.assertTrue(report["real_database"]["unchanged"])


if __name__ == "__main__":
    unittest.main()
