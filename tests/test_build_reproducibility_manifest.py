"""Test the reproducibility manifest on a throwaway MLflow database and git repository."""

import contextlib
import hashlib
import importlib.util
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from mlflow.tracking import MlflowClient


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "manifest_under_test", ROOT / "scripts/build_reproducibility_manifest.py"
)
manifest_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_module)

MODEL_BYTES = b"not a real model, only bytes to hash"
MODEL_SHA = hashlib.sha256(MODEL_BYTES).hexdigest()
OTHER_SHA = "f" * 64
UNKNOWN_COMMIT = "0" * 40


def run_git(folder, *args):
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", *args],
        cwd=folder, check=True, capture_output=True,
    )


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One database and repository for the class: MLflow creates its tables slowly.
        cls.folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.folder.cleanup)
        cls.root = Path(cls.folder.name) / "repo"
        (cls.root / "reports").mkdir(parents=True)
        (cls.root / "artifacts").mkdir()

        (cls.root / "README.txt").write_text("one file so a commit exists\n")
        run_git(cls.root, "init", "-q")
        run_git(cls.root, "add", "README.txt")
        run_git(cls.root, "commit", "-q", "-m", "first")
        cls.commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cls.root, text=True
        ).strip()

        cls.model = cls.root / "artifacts/model.joblib"
        cls.model.write_bytes(MODEL_BYTES)
        cls.receipt = cls.root / "reports/mlflow_registry.json"
        cls.receipt.write_text(json.dumps({"models": {"xgboost": {
            "name": "Throwaway-XGBoost", "version": 1, "model_sha256": MODEL_SHA,
        }}}))
        (cls.root / "reports/xgboost_validation_gate.json").write_text(json.dumps({
            "git_commit": cls.commit,
            "gate": {"identity": {"model_sha256": OTHER_SHA}},
        }))
        (cls.root / "paysim.dvc").write_text(
            "outs:\n- md5: abc123\n  size: 10\n  hash: md5\n  path: data/raw/paysim.csv\n"
        )
        (cls.root / "dvc.lock").write_text(
            "schema: '2.0'\nstages:\n  spark_prepare:\n    deps:\n"
            "    - path: data/raw/paysim.csv\n      hash: md5\n      md5: abc123\n"
            "    outs:\n    - path: data/processed/x\n      hash: md5\n"
            "      md5: def456.dir\n"
        )

        cls.database = Path(cls.folder.name) / "tracking.db"
        cls.uri = f"sqlite:///{cls.database}"
        cls.client = MlflowClient(tracking_uri=cls.uri)
        experiment = cls.client.create_experiment(
            "manifest-tests",
            artifact_location=(Path(cls.folder.name) / "artifacts").as_uri(),
        )

        def make_run(tags):
            run = cls.client.create_run(experiment, tags=tags)
            cls.client.set_terminated(run.info.run_id, "FINISHED")
            return run.info.run_id

        cls.training_run = make_run({
            "mlflow.source.git.commit": UNKNOWN_COMMIT,
            "training_code_commit": "not_established",
            "model_sha256": MODEL_SHA,
        })
        cls.packaging_run = make_run({
            "mlflow.source.git.commit": cls.commit,
            "source_run_id": cls.training_run,
            "model_sha256": MODEL_SHA,
        })
        cls.analysis_run = make_run({
            "git_commit": cls.commit, "git_dirty": "false", "model_sha256": MODEL_SHA,
        })
        cls.untagged_run = make_run({})
        cls.client.create_registered_model("Throwaway-XGBoost")
        cls.client.create_model_version(
            "Throwaway-XGBoost", source="file:///nowhere/deployed",
            run_id=cls.packaging_run,
        )
        cls.client.set_registered_model_alias("Throwaway-XGBoost", "champion", "1")
        cls.client.create_registered_model("Throwaway-Other")
        cls.client.create_model_version(
            "Throwaway-Other", source="file:///nowhere/other", run_id=cls.untagged_run,
        )

    def build(self):
        return manifest_module.build_manifest(
            self.root, self.model, self.receipt, self.database
        )

    def version(self, manifest, name):
        return next(
            item for item in manifest["registry"]["versions"]
            if item["registry_name"] == name
        )

    def test_model_hash_and_receipt(self):
        deployed = self.build()["deployed_model"]
        self.assertEqual(deployed["sha256"], MODEL_SHA)
        self.assertEqual(deployed["size_bytes"], len(MODEL_BYTES))
        self.assertTrue(deployed["registry_receipt"]["matches_deployed_file"])

    def test_receipt_for_a_different_model_is_reported_as_mismatch(self):
        other = self.root / "artifacts/other.joblib"
        other.write_bytes(b"different bytes")
        self.addCleanup(other.unlink)
        manifest = manifest_module.build_manifest(
            self.root, other, self.receipt, self.database
        )
        self.assertFalse(
            manifest["deployed_model"]["registry_receipt"]["matches_deployed_file"]
        )

    def test_registered_version_links_both_runs_in_order(self):
        version = self.version(self.build(), "Throwaway-XGBoost")
        self.assertEqual(version["registry_version"], "1")
        self.assertEqual(version["aliases"], ["champion"])
        self.assertTrue(version["is_deployed_model"])
        self.assertEqual(
            [(run["run_id"], run["role"]) for run in version["runs"]],
            [(self.packaging_run, "registered_run"),
             (self.training_run, "linked_by_source_run_id")],
        )

    def test_commit_tag_is_reported_and_checked_against_git(self):
        version = self.version(self.build(), "Throwaway-XGBoost")
        packaging, training = version["runs"]
        self.assertEqual(packaging["git_commit"], self.commit)
        self.assertTrue(packaging["git_commit_exists_in_repository"])
        self.assertEqual(training["git_commit"], UNKNOWN_COMMIT)
        self.assertFalse(training["git_commit_exists_in_repository"])

    def test_not_established_commit_is_kept_and_never_invented(self):
        training = self.version(self.build(), "Throwaway-XGBoost")["runs"][1]
        self.assertEqual(training["training_code_commit"], "not_established")

    def test_run_without_commit_tag_says_missing(self):
        other = self.version(self.build(), "Throwaway-Other")
        run = other["runs"][0]
        self.assertEqual(run["git_commit"], "missing")
        self.assertEqual(run["training_code_commit"], "missing")
        self.assertEqual(run["model_sha256_tag"], "missing")
        self.assertIsNone(run["git_commit_exists_in_repository"])
        self.assertFalse(other["is_deployed_model"])

    def test_invalid_commit_tag_is_not_accepted(self):
        run_id = self.client.create_run(
            self.client.get_experiment_by_name("manifest-tests").experiment_id,
            tags={"git_commit": "not-a-hash"},
        ).info.run_id
        self.client.set_terminated(run_id, "FINISHED")
        with manifest_module.open_read_only(self.database) as connection:
            described = manifest_module.describe_run(connection, self.root, run_id, "x")
        self.assertEqual(described["git_commit"], "missing")
        self.assertEqual(described["commit_tags"], {"git_commit": "not-a-hash"})

    def test_unlisted_run_with_the_model_hash_is_reported_separately(self):
        extra = self.build()["registry"]["other_runs_tagged_with_model_sha256"]
        self.assertEqual([run["run_id"] for run in extra], [self.analysis_run])
        self.assertEqual(extra[0]["git_commit"], self.commit)
        self.assertEqual(extra[0]["git_dirty"], "false")

    def test_unknown_run_id_is_marked_not_found(self):
        with manifest_module.open_read_only(self.database) as connection:
            described = manifest_module.describe_run(connection, self.root, "nope", "x")
        self.assertEqual(described, {"run_id": "nope", "role": "x", "found": False})

    def test_data_pointer_and_lock_agree(self):
        data = self.build()["data"]
        self.assertEqual(data["raw_data"]["md5"], "abc123")
        self.assertEqual(data["raw_data_md5_in_dvc_lock"], "abc123")
        self.assertTrue(data["pointer_and_lock_agree"])
        self.assertEqual(
            data["dvc_lock_stages"]["spark_prepare"]["outputs"],
            {"data/processed/x": "def456.dir"},
        )

    def test_data_disagreement_and_missing_files_are_visible(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / "paysim.dvc").write_text(
            "outs:\n- md5: other\n  size: 1\n  path: data/raw/paysim.csv\n"
        )
        data = manifest_module.data_record(
            Path(folder.name) / "paysim.dvc", self.root / "dvc.lock"
        )
        self.assertFalse(data["pointer_and_lock_agree"])
        absent = manifest_module.data_record(
            Path(folder.name) / "no.dvc", Path(folder.name) / "no.lock"
        )
        self.assertEqual(absent["raw_data"], "missing")
        self.assertEqual(absent["dvc_lock_stages"], "missing")
        self.assertFalse(absent["pointer_and_lock_agree"])

    def test_gate_file_for_another_model_is_flagged(self):
        gate = self.build()["validation_gate"]
        self.assertEqual(gate["gate_file_model_sha256"], OTHER_SHA)
        self.assertFalse(gate["describes_deployed_model"])
        self.assertEqual(gate["gate_file_recorded_git_commit"], self.commit)

    def test_report_files_record_hash_commit_and_absence(self):
        files = self.build()["report_files"]
        gate = files["reports/xgboost_validation_gate.json"]
        self.assertEqual(
            gate["sha256"],
            hashlib.sha256(
                (self.root / "reports/xgboost_validation_gate.json").read_bytes()
            ).hexdigest(),
        )
        # Written after the only commit, so git knows no commit for it.
        self.assertEqual(gate["last_commit"], "missing")
        self.assertTrue(gate["uncommitted_changes"])
        self.assertEqual(
            files["reports/drift_reference.json"],
            {"present": False, "last_commit": "missing"},
        )

    def test_environment_lists_python_and_packages(self):
        environment = self.build()["environment"]
        self.assertRegex(environment["python"], r"^\d+\.\d+\.\d+")
        self.assertRegex(environment["packages"]["mlflow"], r"^\d")
        self.assertIn("not proof", environment["note"])

    def test_database_is_not_modified_or_created(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.build()
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        absent = Path(self.folder.name) / "absent.db"
        with self.assertRaises(FileNotFoundError):
            manifest_module.build_manifest(self.root, self.model, self.receipt, absent)
        self.assertFalse(absent.exists())

    def test_manifest_is_valid_json_without_nonfinite_numbers(self):
        text = json.dumps(self.build(), allow_nan=False)
        self.assertEqual(json.loads(text)["schema_version"], 1)

    def cli(self, output, database=None):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = manifest_module.main([
                "--root", str(self.root), "--model", str(self.model),
                "--receipt", str(self.receipt),
                "--database", str(database or self.database),
                "--output", str(output),
            ])
        return code, err.getvalue()

    def test_output_is_create_only(self):
        output = Path(self.folder.name) / "out/manifest.json"
        code, _ = self.cli(output)
        self.assertEqual(code, 0)
        first = output.read_bytes()
        self.assertEqual(json.loads(first)["deployed_model"]["sha256"], MODEL_SHA)

        code, message = self.cli(output)
        self.assertEqual(code, 2)
        self.assertIn("never overwritten", message)
        self.assertEqual(output.read_bytes(), first)

    def test_missing_database_stops_without_writing(self):
        output = Path(self.folder.name) / "none.json"
        code, message = self.cli(output, database=Path(self.folder.name) / "gone.db")
        self.assertEqual(code, 2)
        self.assertIn("could not read an input", message)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
