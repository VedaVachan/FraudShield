"""Test the opt-in --watch mode. No real training: commands are replaced by fakes."""

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from backend.retraining_queue import connect, enqueue_notification
from backend.retraining_worker import execute_request


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_retraining_worker_under_test", ROOT / "scripts/run_retraining_worker.py"
)
worker_script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker_script)


def queue_request(database, instance, now):
    """Queue one drift request and return its ID."""
    record = {
        "delivery_id": str(uuid4()), "received_utc": "2026-11-12T09:00:00+00:00",
        "status": "firing", "alerts": [{
            "status": "firing",
            "labels": {"alertname": "FraudShieldInputDrift", "job": "fraudshield-api",
                       "instance": instance, "feature": "amount"},
        }],
    }
    return enqueue_notification(database, record, now=now)[0]["request_id"]


def set_status(database, request_id, status):
    connection = connect(database)
    try:
        connection.execute(
            "UPDATE requests SET status = ? WHERE request_id = ?", (status, request_id)
        )
    finally:
        connection.close()


def status_of(database, request_id):
    connection = connect(database)
    try:
        return connection.execute(
            "SELECT status FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()[0]
    finally:
        connection.close()


class WatchBase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.database = self.root / "runtime/retraining/queue.db"
        self.calls = []
        self.verified = 0

    def verify(self):
        self.verified += 1

    def fake_execute(self, database, request_id):
        self.calls.append(request_id)
        set_status(database, request_id, "succeeded")
        return {"request_id": request_id, "status": "succeeded"}

    def watch(self, **overrides):
        options = dict(execute=self.fake_execute, verify=self.verify, max_cycles=1)
        options.update(overrides)
        with self.assertLogs("run_retraining_worker_under_test", "INFO") as logs:
            code = worker_script.watch(self.database, 0.01, **options)
            LOGGER_PROBE.info("probe")  # keeps assertLogs satisfied when nothing is logged
        self.logs = "\n".join(logs.output)
        return code


# assertLogs needs the logger it watches to emit; watch() always logs on start.
LOGGER_PROBE = logging.getLogger("run_retraining_worker_under_test")


class WatchLoopTests(WatchBase):
    def test_nothing_queued_runs_nothing_and_exits_zero(self):
        self.assertEqual(self.watch(), 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.verified, 0)
        self.assertIn("Watch mode started", self.logs)
        self.assertIn("Watch mode stopped", self.logs)

    def test_queued_requests_run_oldest_first_and_only_once(self):
        newer = queue_request(self.database, "api-b:8000", now=200)
        older = queue_request(self.database, "api-a:8000", now=100)
        self.assertEqual(self.watch(max_cycles=3), 0)
        self.assertEqual(self.calls, [older, newer])
        self.assertEqual(self.verified, 2)

    def test_every_decision_is_logged(self):
        request_id = queue_request(self.database, "api:8000", now=100)
        self.watch()
        self.assertIn(f"running queued request {request_id}", self.logs)
        self.assertIn(f"request {request_id} finished with status succeeded", self.logs)

    def test_requests_that_are_not_queued_are_never_started(self):
        for state in ("running", "succeeded", "rejected", "failed"):
            with self.subTest(state=state):
                request_id = queue_request(
                    self.database, f"api-{state}:8000", now=100 + len(state)
                )
                set_status(self.database, request_id, state)
        self.assertEqual(self.watch(), 0)
        self.assertEqual(self.calls, [])

    def test_busy_worker_lock_stops_the_cycle_and_retries_next_cycle(self):
        first = queue_request(self.database, "api-a:8000", now=100)
        queue_request(self.database, "api-b:8000", now=200)
        outcomes = iter([{"status": "busy"}])

        def execute(database, request_id):
            self.calls.append(request_id)
            try:
                return next(outcomes)
            except StopIteration:
                set_status(database, request_id, "succeeded")
                return {"status": "succeeded"}

        self.assertEqual(self.watch(execute=execute, max_cycles=2), 0)
        self.assertEqual(self.calls[:2], [first, first])
        self.assertIn("worker lock is held elsewhere", self.logs)

    def test_uncommitted_sources_stop_the_watch_before_any_request_runs(self):
        queue_request(self.database, "api:8000", now=100)

        def refuse():
            raise subprocess.CalledProcessError(1, ["git", "diff"])

        self.assertEqual(self.watch(verify=refuse), 1)
        self.assertEqual(self.calls, [])
        self.assertIn("not committed and unchanged", self.logs)

    def test_an_aborted_request_stops_the_watch_and_is_not_retried(self):
        queue_request(self.database, "api-a:8000", now=100)
        queue_request(self.database, "api-b:8000", now=200)

        def explode(database, request_id):
            self.calls.append(request_id)
            raise OSError("disk full")

        self.assertEqual(self.watch(execute=explode, max_cycles=3), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("aborted before a terminal state", self.logs)

    def test_failed_and_rejected_outcomes_do_not_stop_the_watch(self):
        first = queue_request(self.database, "api-a:8000", now=100)
        second = queue_request(self.database, "api-b:8000", now=200)
        states = {first: "failed", second: "rejected"}

        def execute(database, request_id):
            self.calls.append(request_id)
            set_status(database, request_id, states[request_id])
            return {"status": states[request_id]}

        self.assertEqual(self.watch(execute=execute), 0)
        self.assertEqual(self.calls, [first, second])

    def test_an_interrupted_request_ends_the_watch_cleanly(self):
        first = queue_request(self.database, "api-a:8000", now=100)
        queue_request(self.database, "api-b:8000", now=200)

        def execute(database, request_id):
            self.calls.append(request_id)
            set_status(database, request_id, "failed")
            return {"status": "failed", "error": "Command interrupted by SIGTERM."}

        self.assertEqual(self.watch(execute=execute, max_cycles=5), 0)
        self.assertEqual(self.calls, [first])

    def test_stop_event_ends_the_watch_without_running_further_requests(self):
        queue_request(self.database, "api:8000", now=100)
        stop = threading.Event()
        stop.set()
        self.assertEqual(self.watch(stop=stop, max_cycles=None), 0)
        self.assertEqual(self.calls, [])

    def test_non_positive_or_invalid_interval_is_refused(self):
        for value in (0, -1, float("nan"), float("inf"), True, "5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    worker_script.watch(
                        self.database, value, execute=self.fake_execute,
                        verify=self.verify, max_cycles=1,
                    )
        self.assertEqual(self.calls, [])


class WatchSignalTests(WatchBase):
    def check_signal(self, number):
        before = signal.getsignal(number)
        timer = threading.Timer(0.3, os.kill, (os.getpid(), number))
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertLogs("run_retraining_worker_under_test", "INFO") as logs:
            code = worker_script.watch(
                self.database, 60, execute=self.fake_execute, verify=self.verify
            )
        self.assertEqual(code, 0)
        self.assertEqual(signal.getsignal(number), before)
        self.assertTrue(any(signal.Signals(number).name in line for line in logs.output))

    def test_sigterm_stops_the_watch_cleanly_and_restores_the_handler(self):
        self.check_signal(signal.SIGTERM)

    def test_sigint_stops_the_watch_cleanly_and_restores_the_handler(self):
        self.check_signal(signal.SIGINT)


class WatchUsesTheNormalSafePathTests(WatchBase):
    """Real execute_request, real worker lock and queue; only the commands are fake."""

    def setUp(self):
        super().setUp()
        self.commands = []
        self.request_id = queue_request(self.database, "api:8000", now=100)

    def runner(self, command, **kwargs):
        self.commands.append(command)
        if command[1].endswith("train_xgboost_tracked.py"):
            run_id = "b" * 32
            directory = self.root / "artifacts/xgboost_tracked" / run_id
            directory.mkdir(parents=True)
            model, report = directory / "model.joblib", directory / "validation.json"
            model.write_bytes(b"mock model")
            report.write_text('{"mock": true}\n')
            self.model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
            self.report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
            Path(command[command.index("--receipt") + 1]).write_text(json.dumps({
                "run_id": run_id, "retraining_request_id": self.request_id,
                "trigger": "controlled_drift", "test_evaluated": False,
                "registered_or_promoted": False, "metric_readback": "PASS",
                "artifact_checksum": "PASS",
                "model_path": model.relative_to(self.root).as_posix(),
                "validation_report": report.relative_to(self.root).as_posix(),
                "model_sha256": self.model_hash,
            }))
            return SimpleNamespace(returncode=0)
        json.dump({
            "status": "PASS", "test_set_opened": False, "failures": [],
            "identity": {"model_sha256": self.model_hash,
                         "report_sha256": self.report_hash},
        }, kwargs["stdout"])
        kwargs["stdout"].flush()
        return SimpleNamespace(returncode=0)

    def execute(self, database, request_id):
        return execute_request(database, request_id, root=self.root, runner=self.runner)

    def test_watch_runs_a_queued_request_candidate_only(self):
        self.assertEqual(self.watch(execute=self.execute, max_cycles=2), 0)
        self.assertEqual(status_of(self.database, self.request_id), "succeeded")
        saved = json.loads((
            self.root / "runtime/retraining/runs" / self.request_id / "result.json"
        ).read_text())
        self.assertEqual(saved["status"], "succeeded")
        self.assertIs(saved["registered_or_promoted"], False)
        self.assertEqual(len(self.commands), 2)
        self.assertTrue(self.commands[0][1].endswith("scripts/train_xgboost_tracked.py"))
        self.assertTrue(self.commands[1][1].endswith("scripts/check_model_gate.py"))
        self.assertIn("--trigger", self.commands[0])
        self.assertFalse((self.root / "artifacts/xgboost_v1").exists())

    def test_a_held_worker_lock_leaves_the_request_queued(self):
        lock_path = self.database.parent / "worker.lock"
        with lock_path.open("a") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.watch(execute=self.execute, max_cycles=2), 0)
        self.assertEqual(status_of(self.database, self.request_id), "queued")
        self.assertEqual(self.commands, [])
        self.assertIn("worker lock is held elsewhere", self.logs)


class CommittedSourceCheckTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name)
        self.sources = ["a.py", "b.py"]
        for name in self.sources:
            (self.repo / name).write_text("print(1)\n")
        self.git("init", "-q")
        self.git("add", *self.sources)
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-q", "-m", "x")

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def check(self):
        worker_script.verify_committed_sources(self.repo, self.sources)

    def test_committed_and_unchanged_sources_pass(self):
        self.check()

    def test_a_modified_source_is_refused(self):
        (self.repo / "a.py").write_text("print(2)\n")
        with self.assertRaises(subprocess.CalledProcessError):
            self.check()

    def test_an_untracked_source_is_refused(self):
        (self.repo / "c.py").write_text("print(3)\n")
        with self.assertRaises(subprocess.CalledProcessError):
            worker_script.verify_committed_sources(self.repo, [*self.sources, "c.py"])

    def test_the_project_lists_the_worker_files_it_always_checked(self):
        for name in ("backend/retraining_worker.py", "scripts/run_retraining_worker.py",
                     "scripts/train_xgboost_tracked.py", "scripts/check_model_gate.py"):
            self.assertIn(name, worker_script.SOURCES)


class WatchCommandLineTests(unittest.TestCase):
    def run_main(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = worker_script.main(list(arguments))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_watch_is_off_unless_requested(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        database = Path(folder.name) / "queue.db"
        with patch.object(worker_script, "watch") as watch:
            code, out, _ = self.run_main("--database", str(database), "--list")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), [])
        watch.assert_not_called()

    def test_watch_flag_calls_watch_with_the_default_interval(self):
        with patch.object(worker_script, "watch", return_value=0) as watch:
            code, _, _ = self.run_main("--database", "q.db", "--watch")
        self.assertEqual(code, 0)
        watch.assert_called_once_with(Path("q.db"), worker_script.DEFAULT_INTERVAL)

    def test_interval_is_passed_through(self):
        with patch.object(worker_script, "watch", return_value=0) as watch:
            self.run_main("--database", "q.db", "--watch", "--interval", "5")
        watch.assert_called_once_with(Path("q.db"), 5.0)

    def test_non_positive_interval_is_refused_before_watching(self):
        for value in ("0", "-3", "nan", "inf", "-inf"):
            with self.subTest(value=value), patch.object(worker_script, "watch") as watch:
                code, _, err = self.run_main("--database", "q.db", "--watch",
                                             "--interval", value)
                self.assertEqual(code, 2)
                self.assertIn("--interval", err)
                watch.assert_not_called()

    def test_interval_without_watch_is_refused(self):
        code, _, err = self.run_main("--database", "q.db", "--list", "--interval", "5")
        self.assertEqual(code, 2)
        self.assertIn("only for --watch", err)

    def test_watch_cannot_be_combined_with_another_action(self):
        code, _, _ = self.run_main("--database", "q.db", "--watch", "--request-id", "x")
        self.assertEqual(code, 2)

    def test_the_script_never_registers_promotes_or_deploys(self):
        text = (ROOT / "scripts/run_retraining_worker.py").read_text()
        for forbidden in ("promote_model", "set_registered_model_alias",
                          "create_model_version", "register_model", "docker"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
