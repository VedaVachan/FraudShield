"""Recovery tests: private temporary queues and dummy artifacts only.

Evidence is produced by the real execute_request with a fake runner and an
injected terminal-update failure, so result.json has the worker's real shape.
No training, model loading, MLflow, or live queue is involved.
"""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend import retraining_recovery as recovery
from backend.retraining_queue import connect, enqueue_notification
from backend.retraining_worker import execute_request


REPO = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RecoveryBase(unittest.TestCase):
    scenario = "succeeded"

    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / "runtime/retraining/queue.db"
        record = {
            "delivery_id": str(uuid4()), "status": "firing", "alerts": [{
                "status": "firing", "labels": {
                    "alertname": "FraudShieldInputDrift", "job": "fraudshield-api",
                    "instance": "private-fixture:8000", "feature": "amount",
                },
            }],
        }
        self.request_id = enqueue_notification(self.database, record, now=100)[0]["request_id"]
        self.calls = []
        self.run_id = "b" * 32
        self.run_dir = self.root / "runtime/retraining/runs" / self.request_id
        self.result_path = self.run_dir / "result.json"
        self.model_dir = self.root / "artifacts/xgboost_tracked" / self.run_id

    # -- fake training/gate ------------------------------------------------
    def runner(self, command, **kwargs):
        self.calls.append(command)
        if command[1].endswith("train_xgboost_tracked.py"):
            if self.scenario == "failed":
                return SimpleNamespace(returncode=1)
            self.model_dir.mkdir(parents=True)
            model = self.model_dir / "model.joblib"
            model.write_bytes(b"dummy model bundle")
            self.model_hash = digest(model)
            report = self.model_dir / "validation.json"
            report.write_text(json.dumps({
                "model": "xgboost", "evaluation_split": "validation",
                "test_evaluated": False, "model_sha256": self.model_hash,
            }) + "\n")
            self.report_hash = digest(report)
            receipt = {
                "run_id": self.run_id, "retraining_request_id": self.request_id,
                "trigger": "controlled_drift", "test_evaluated": False,
                "registered_or_promoted": False, "metric_readback": "PASS",
                "artifact_checksum": "PASS",
                "model_path": model.relative_to(self.root).as_posix(),
                "validation_report": report.relative_to(self.root).as_posix(),
                "model_sha256": self.model_hash,
            }
            Path(command[command.index("--receipt") + 1]).write_text(json.dumps(receipt))
            return SimpleNamespace(returncode=0)
        rejected = self.scenario == "rejected"
        json.dump({
            "status": "FAIL" if rejected else "PASS", "test_set_opened": False,
            "identity": {"model_sha256": self.model_hash, "report_sha256": self.report_hash},
            "failures": ["Dummy metric below floor."] if rejected else [],
        }, kwargs["stdout"])
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=1 if rejected else 0)

    def run_worker(self):
        return execute_request(self.database, self.request_id, root=self.root, runner=self.runner)

    def interrupt_publication(self):
        """Publish a real result.json, then fail the terminal SQLite update."""
        real = connect(self.database)
        self.addCleanup(real.close)

        class Failing:
            def execute(inner, query, parameters=()):
                if query.startswith("UPDATE requests SET status = ?"):
                    raise sqlite3.OperationalError("injected terminal update failure")
                return real.execute(query, parameters)

            def __getattr__(inner, name):
                return getattr(real, name)

        with patch("backend.retraining_worker.connect", return_value=Failing()):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_worker()
        self.assertEqual(self.status(), "running")
        self.assertTrue(self.result_path.is_file())

    # -- helpers -----------------------------------------------------------
    def query(self, sql, parameters=()):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def status(self):
        return self.query("SELECT status FROM requests WHERE request_id = ?",
                          (self.request_id,))[0][0]

    def audit_rows(self):
        if not self.query("SELECT 1 FROM sqlite_master WHERE name = 'recovery_audit'"):
            return []
        return self.query("SELECT request_id, previous_status, new_status, result_sha256, reason "
                          "FROM recovery_audit")

    def inspect(self):
        return recovery.reconcile(self.database, self.request_id, root=self.root)

    def apply(self, confirm=None, reason="operator reviewed evidence"):
        confirm = confirm or digest(self.result_path)
        return recovery.reconcile(self.database, self.request_id, root=self.root, apply=True,
                                  confirm_result_sha256=confirm, reason=reason)

    def rewrite_result(self, change):
        data = json.loads(self.result_path.read_text())
        change(data)
        self.result_path.write_text(json.dumps(data, indent=2) + "\n")

    def assert_unchanged_refusal(self, result, code="insufficient_evidence"):
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["reason_code"], code)
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.audit_rows(), [])


class ValidInterruptedPublication(RecoveryBase):
    def test_inspect_is_read_only_and_reports_proposal(self):
        self.interrupt_publication()
        before_db, before_result = digest(self.database), self.result_path.read_bytes()
        result = self.inspect()
        self.assertEqual(result["decision"], "recoverable")
        self.assertEqual(result["proposed_status"], "succeeded")
        self.assertEqual(result["apply_requires"]["confirm_result_sha256"], digest(self.result_path))
        self.assertEqual(digest(self.database), before_db)
        self.assertEqual(self.result_path.read_bytes(), before_result)
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.audit_rows(), [])

    def check_apply(self, expected):
        self.interrupt_publication()
        original = self.result_path.read_bytes()
        trained = len(self.calls)
        result = self.apply()
        self.assertEqual(result["decision"], "recovered")
        self.assertEqual(result["new_status"], expected)
        self.assertEqual(self.status(), expected)
        self.assertEqual(self.result_path.read_bytes(), original)
        self.assertEqual(len(self.calls), trained)  # nothing was rerun
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], (self.request_id, "running", expected))
        self.assertEqual(rows[0][3], hashlib.sha256(original).hexdigest())
        self.assertFalse((self.root / "artifacts/xgboost_v1").exists())

    def test_apply_succeeded(self):
        self.check_apply("succeeded")

    def test_apply_rejected(self):
        self.scenario = "rejected"
        self.check_apply("rejected")

    def test_apply_failed_needs_no_artifacts(self):
        self.scenario = "failed"
        self.check_apply("failed")

    def test_repeat_invocation_is_safe(self):
        self.interrupt_publication()
        self.assertEqual(self.apply()["decision"], "recovered")
        self.assertEqual(self.apply()["decision"], "already_recovered")
        self.assertEqual(self.inspect()["decision"], "already_recovered")
        self.assertEqual(len(self.audit_rows()), 1)
        self.assertEqual(self.status(), "succeeded")

    def test_normal_completion_is_not_audited(self):
        self.run_worker()
        self.assertEqual(self.inspect()["decision"], "already_terminal")
        self.assertEqual(self.apply()["decision"], "already_terminal")
        self.assertEqual(self.audit_rows(), [])

    def test_recovery_does_not_enable_replay(self):
        self.interrupt_publication()
        self.apply()
        self.assertEqual(self.run_worker()["status"], "not_queued")
        self.assertEqual(len(self.calls), 2)


class LockBehaviour(RecoveryBase):
    def test_busy_lock_blocks_inspect_and_apply(self):
        self.interrupt_publication()
        digest_value = digest(self.result_path)
        with (self.database.parent / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.inspect()["decision"], "busy")
            self.assertEqual(self.apply(confirm=digest_value)["decision"], "busy")
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.inspect()["decision"], "recoverable")

    def test_recovery_holds_the_workers_lock(self):
        self.interrupt_publication()
        seen = []
        real = recovery.check_evidence

        def spying(*args, **kwargs):
            seen.append(self.run_worker())
            return real(*args, **kwargs)

        with patch("backend.retraining_recovery.check_evidence", side_effect=spying):
            self.assertEqual(self.inspect()["decision"], "recoverable")
        self.assertEqual(seen, [{"status": "busy"}])

    def test_inherited_child_lock_blocks_recovery(self):
        self.interrupt_publication()
        lock = (self.database.parent / "worker.lock").open("a")
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 pass_fds=(lock.fileno(),))
        self.addCleanup(lambda: (child.kill(), child.wait()))
        lock.close()  # the 'worker' died; only the child holds the lock now
        self.assertEqual(self.inspect()["decision"], "busy")
        child.kill()
        child.wait()
        self.assertEqual(self.inspect()["decision"], "recoverable")


class InsufficientEvidence(RecoveryBase):
    def test_killed_worker_without_result_is_not_recoverable(self):
        self.run_dir.mkdir(parents=True)
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE requests SET status = 'running' WHERE request_id = ?",
                           (self.request_id,))
        connection.commit()
        connection.close()
        (self.run_dir / "training.log").write_text("partial")
        (self.run_dir / ".result-abc.tmp").write_text(
            json.dumps({"status": "succeeded", "registered_or_promoted": False}))
        result = self.inspect()
        self.assert_unchanged_refusal(result)
        self.assertEqual(result["temporary_files_ignored"], 1)
        self.assert_unchanged_refusal(self.apply(confirm="0" * 64))

    def test_malformed_result_files(self):
        self.interrupt_publication()
        good = self.result_path.read_text()
        for label, content in {
            "truncated": good[: len(good) // 2],
            "nan": good.replace('"registered_or_promoted": false', '"x": NaN'),
            "array": "[]\n",
            "duplicate key": '{"status": "failed", "status": "failed"}\n',
            "binary": "\xff\xfe",
        }.items():
            with self.subTest(label):
                self.result_path.write_bytes(content.encode("latin-1", "replace")
                                             if label == "binary" else content.encode())
                self.assert_unchanged_refusal(self.inspect())

    def test_symlinked_result_is_not_evidence(self):
        self.interrupt_publication()
        elsewhere = self.root / "elsewhere.json"
        elsewhere.write_bytes(self.result_path.read_bytes())
        self.result_path.unlink()
        self.result_path.symlink_to(elsewhere)
        self.assert_unchanged_refusal(self.inspect())

    def test_mismatched_result_fields(self):
        self.interrupt_publication()
        original = self.result_path.read_bytes()
        mutations = {
            "other request": lambda d: d.update(request_id=str(uuid4())),
            "non-terminal status": lambda d: d.update(status="running"),
            "unknown status": lambda d: d.update(status="done"),
            "promoted": lambda d: d.update(registered_or_promoted=True),
            "scope": lambda d: d.update(scope="register and promote"),
            "bad timestamps": lambda d: d.update(finished_utc="2000-01-01T00:00:00+00:00"),
            "naive timestamp": lambda d: d.update(started_utc="2026-01-01T00:00:00"),
            "error on success": lambda d: d.update(error="boom"),
            "bad exit code": lambda d: d.update(gate_exit_code=1),
            "bad run id": lambda d: d.update(mlflow_run_id="../../etc"),
            "bad worker hash": lambda d: d.update(worker_sha256="zz"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                self.result_path.write_bytes(original)
                self.rewrite_result(mutate)
                self.assert_unchanged_refusal(self.inspect())

    def test_failed_result_needs_error_message(self):
        self.scenario = "failed"
        self.interrupt_publication()
        self.rewrite_result(lambda d: d.update(error="  "))
        self.assert_unchanged_refusal(self.inspect())

    def test_artifact_and_receipt_mismatches_for_succeeded(self):
        self.interrupt_publication()
        originals = {path: path.read_bytes() for path in
                     (self.run_dir / "training_receipt.json", self.run_dir / "gate.json",
                      self.model_dir / "model.joblib", self.model_dir / "validation.json")}

        def restore():
            for path, content in originals.items():
                path.write_bytes(content)

        def edit(name, change):
            path = self.run_dir / name
            data = json.loads(path.read_text())
            change(data)
            path.write_text(json.dumps(data))

        cases = {
            "model bytes changed": lambda: (self.model_dir / "model.joblib").write_bytes(b"x"),
            "receipt missing": lambda: (self.run_dir / "training_receipt.json").unlink(),
            "receipt other request": lambda: edit(
                "training_receipt.json", lambda d: d.update(retraining_request_id="x")),
            "receipt promoted": lambda: edit(
                "training_receipt.json", lambda d: d.update(registered_or_promoted=True)),
            "receipt path outside tree": lambda: edit(
                "training_receipt.json", lambda d: d.update(model_path="../model.joblib")),
            "receipt other run": lambda: edit(
                "training_receipt.json", lambda d: d.update(run_id="c" * 32)),
            "gate missing": lambda: (self.run_dir / "gate.json").unlink(),
            "gate fail": lambda: edit("gate.json", lambda d: d.update(status="FAIL")),
            "gate opened test": lambda: edit("gate.json", lambda d: d.update(test_set_opened=True)),
            "gate model identity": lambda: edit(
                "gate.json", lambda d: d["identity"].update(model_sha256="0" * 64)),
            "gate report identity": lambda: edit(
                "gate.json", lambda d: d["identity"].update(report_sha256="0" * 64)),
            "report changed after gate": lambda: (self.model_dir / "validation.json").write_text("{}\n"),
            "report evaluated test": lambda: (self.model_dir / "validation.json").write_text(
                json.dumps({"evaluation_split": "test", "test_evaluated": True,
                            "model_sha256": self.model_hash})),
            "model dir symlink": lambda: (
                (self.model_dir.parent / "real").mkdir(),
                self.model_dir.rename(self.model_dir.parent / "real" / self.run_id),
                self.model_dir.symlink_to(self.model_dir.parent / "real" / self.run_id)),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                if self.model_dir.is_symlink():
                    self.model_dir.unlink()
                    (self.model_dir.parent / "real" / self.run_id).rename(self.model_dir)
                restore()
                mutate()
                self.assert_unchanged_refusal(self.inspect())

    def test_artifact_mismatches_for_rejected(self):
        self.scenario = "rejected"
        self.interrupt_publication()
        self.assertEqual(self.inspect()["proposed_status"], "rejected")
        gate_path = self.run_dir / "gate.json"
        good = gate_path.read_text()
        for label, change in {
            "gate says pass": lambda d: d.update(status="PASS"),
            "failures differ": lambda d: d.update(failures=["Other."]),
            "wrong identity": lambda d: d["identity"].update(model_sha256="0" * 64),
        }.items():
            with self.subTest(label):
                gate_path.write_text(good)
                data = json.loads(good)
                change(data)
                gate_path.write_text(json.dumps(data))
                self.assert_unchanged_refusal(self.inspect())
        gate_path.write_text(good)
        self.rewrite_result(lambda d: d.update(gate_failures=[]))
        self.assert_unchanged_refusal(self.inspect())


class StateAndConfirmation(RecoveryBase):
    def test_conflicting_terminal_state_is_never_changed(self):
        self.interrupt_publication()
        connection = sqlite3.connect(self.database)
        connection.execute("UPDATE requests SET status = 'failed' WHERE request_id = ?",
                           (self.request_id,))
        connection.commit()
        connection.close()
        for result in (self.inspect(), self.apply()):
            self.assertEqual(result["decision"], "refused")
            self.assertEqual(result["reason_code"], "conflict")
        self.assertEqual(self.status(), "failed")
        self.assertEqual(self.audit_rows(), [])

    def test_queued_row_is_refused(self):
        self.assertEqual(self.inspect()["reason_code"], "not_running")
        self.assertEqual(self.status(), "queued")

    def test_unknown_request_and_missing_database(self):
        result = recovery.reconcile(self.database, str(uuid4()), root=self.root)
        self.assertEqual(result["reason_code"], "unknown_request")
        missing = self.root / "nowhere/queue.db"
        result = recovery.reconcile(missing, self.request_id, root=self.root)
        self.assertEqual(result["reason_code"], "database_missing")
        self.assertFalse(missing.parent.exists())

    def test_invalid_arguments_raise_before_any_access(self):
        with self.assertRaises(ValueError):
            recovery.reconcile(self.database, "not-a-uuid", root=self.root)
        self.interrupt_publication()
        with self.assertRaises(ValueError):
            self.apply(reason="   ")
        with self.assertRaises(ValueError):
            self.apply(confirm="short")
        self.assertEqual(self.audit_rows(), [])

    def test_wrong_confirmation_hash_changes_nothing(self):
        self.interrupt_publication()
        self.assert_unchanged_refusal(self.apply(confirm="0" * 64), "confirmation_mismatch")

    def test_evidence_edited_after_inspection_is_refused(self):
        self.interrupt_publication()
        reviewed = self.inspect()["apply_requires"]["confirm_result_sha256"]
        self.rewrite_result(lambda d: d.update(note="edited after review"))
        self.assert_unchanged_refusal(self.apply(confirm=reviewed), "confirmation_mismatch")


class DatabaseFailure(RecoveryBase):
    def failing(self, fragment):
        real = recovery.open_database(self.database, writable=True)
        self.addCleanup(real.close)

        class Failing:
            def execute(inner, query, parameters=()):
                if fragment in query:
                    raise sqlite3.OperationalError("injected failure")
                return real.execute(query, parameters)

            def __getattr__(inner, name):
                return getattr(real, name)

        return Failing()

    def test_failure_at_status_update_leaves_row_and_audit_untouched(self):
        self.interrupt_publication()
        with patch("backend.retraining_recovery.open_database",
                   return_value=self.failing("UPDATE requests")):
            result = self.apply()
        self.assertEqual(result["decision"], "database_error")
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.apply()["decision"], "recovered")

    def test_failure_writing_audit_rolls_back_status_update(self):
        self.interrupt_publication()
        with patch("backend.retraining_recovery.open_database",
                   return_value=self.failing("INSERT INTO recovery_audit")):
            result = self.apply()
        self.assertEqual(result["decision"], "database_error")
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.apply()["decision"], "recovered")
        self.assertEqual(self.status(), "succeeded")

    def test_conditional_update_guards_against_a_stale_read(self):
        # A competing writer changes the row just before our transaction and the
        # in-transaction re-read is forced to look stale; only the conditional
        # UPDATE (status = 'running') can then prevent an overwrite.
        self.interrupt_publication()
        real = recovery.open_database(self.database, writable=True)
        self.addCleanup(real.close)
        database, state = self.database, {"begun": False}

        class Racing:
            def execute(inner, query, parameters=()):
                if query == "BEGIN IMMEDIATE":
                    other = sqlite3.connect(database)
                    other.execute("UPDATE requests SET status = 'failed'")
                    other.commit()
                    other.close()
                    state["begun"] = True
                elif state["begun"] and query.startswith("SELECT status FROM requests"):
                    return SimpleNamespace(fetchone=lambda: {"status": "running"})
                return real.execute(query, parameters)

            def __getattr__(inner, name):
                return getattr(real, name)

        with patch("backend.retraining_recovery.open_database", return_value=Racing()):
            result = self.apply()
        self.assertEqual(result["reason_code"], "conflict")
        self.assertEqual(self.status(), "failed")
        self.assertEqual(self.audit_rows(), [])

    def test_database_that_cannot_be_opened_reports_error(self):
        self.interrupt_publication()
        with patch("backend.retraining_recovery.open_database",
                   side_effect=sqlite3.OperationalError("cannot open")):
            self.assertEqual(self.inspect()["decision"], "database_error")
        self.assertEqual(self.status(), "running")


class CommandLine(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / "queue.db"
        record = {
            "delivery_id": str(uuid4()), "status": "firing", "alerts": [{
                "status": "firing", "labels": {
                    "alertname": "FraudShieldInputDrift", "job": "fraudshield-api",
                    "instance": "cli-fixture:8000", "feature": "amount"},
            }],
        }
        self.request_id = enqueue_notification(self.database, record, now=1)[0]["request_id"]
        connection = connect(self.database)
        connection.execute("UPDATE requests SET status = 'running'")
        connection.close()

    def cli(self, *arguments):
        environment = {**os.environ, "PYTHONPATH": str(REPO)}
        return subprocess.run(
            [sys.executable, str(REPO / "scripts/run_retraining_worker.py"),
             "--database", str(self.database), *arguments],
            cwd=REPO, env=environment, capture_output=True, text=True,
        )

    def test_inspect_without_evidence_exits_one_and_prints_json(self):
        done = self.cli("--inspect-recovery", self.request_id)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["reason_code"], "insufficient_evidence")

    def test_recover_requires_confirmation_and_reason(self):
        self.assertEqual(self.cli("--recover", self.request_id).returncode, 2)
        self.assertEqual(self.cli("--recover", self.request_id,
                                  "--reason", "x").returncode, 2)

    def test_confirmation_flags_rejected_outside_recover(self):
        done = self.cli("--inspect-recovery", self.request_id, "--reason", "x")
        self.assertEqual(done.returncode, 2)

    def test_queue_untouched_by_cli_inspect(self):
        connection = sqlite3.connect(self.database)
        before = connection.execute("SELECT status FROM requests").fetchall()
        connection.close()
        self.cli("--inspect-recovery", self.request_id)
        connection = sqlite3.connect(self.database)
        self.assertEqual(connection.execute("SELECT status FROM requests").fetchall(), before)
        connection.close()


if __name__ == "__main__":
    unittest.main()
