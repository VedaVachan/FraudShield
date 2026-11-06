"""Fault injection uses temporary results and private queues only."""

import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from backend.result_storage import publish_result
import test_retraining_worker as worker_tests


class ResultStorageTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / "result.json"
        self.outcome = {"status": "failed", "registered_or_promoted": False}

    def test_complete_json_is_published_without_temporary_file(self):
        publish_result(self.path, self.outcome)
        self.assertEqual(json.loads(self.path.read_text()), self.outcome)
        self.assertEqual(list(self.root.glob(".result-*.tmp")), [])

    def test_invalid_json_value_never_creates_result(self):
        with self.assertRaises(ValueError):
            publish_result(self.path, {"invalid": float("nan")})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_file_sync_failure_never_publishes_partial_result(self):
        with patch("backend.result_storage.os.fsync", side_effect=OSError("file sync failed")):
            with self.assertRaises(OSError):
                publish_result(self.path, self.outcome)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_result_is_never_overwritten(self):
        self.path.write_bytes(b"original evidence\n")
        with self.assertRaises(FileExistsError):
            publish_result(self.path, self.outcome)
        self.assertEqual(self.path.read_bytes(), b"original evidence\n")
        self.assertEqual(list(self.root.glob(".result-*.tmp")), [])

    def test_directory_sync_failure_preserves_complete_published_result(self):
        real_sync = os.fsync
        calls = []
        def sync(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("directory sync failed")
            real_sync(descriptor)
        with patch("backend.result_storage.os.fsync", side_effect=sync):
            with self.assertRaises(OSError):
                publish_result(self.path, self.outcome)
        self.assertEqual(json.loads(self.path.read_text()), self.outcome)

    def check_killed_publisher(self, point):
        marker = self.root / "paused"
        driver = '''
import json, os, sys, time
from pathlib import Path
from unittest.mock import patch
from backend.result_storage import publish_result
path, marker, point = map(str, sys.argv[1:])
real_link, real_sync = os.link, os.fsync
calls = []
def link(*args, **kwargs):
    if point == "before":
        Path(marker).write_text("paused")
        time.sleep(30)
    return real_link(*args, **kwargs)
def sync(descriptor):
    calls.append(descriptor)
    if point == "after" and len(calls) == 2:
        Path(marker).write_text("paused")
        time.sleep(30)
    return real_sync(descriptor)
with patch("backend.result_storage.os.link", side_effect=link), patch("backend.result_storage.os.fsync", side_effect=sync):
    publish_result(path, {"status": "failed", "registered_or_promoted": False})
'''
        process = subprocess.Popen([sys.executable, "-c", driver, str(self.path), str(marker), point])
        try:
            deadline = time.monotonic() + 8
            while not marker.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("Dummy publisher did not reach the injection point.")
                time.sleep(0.02)
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(process.returncode, -signal.SIGKILL)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_sigkill_before_publication_leaves_no_final_result(self):
        self.check_killed_publisher("before")
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.root.glob(".result-*.tmp"))), 1)

    def test_sigkill_after_publication_leaves_complete_json(self):
        self.check_killed_publisher("after")
        self.assertEqual(json.loads(self.path.read_text()), self.outcome)


class WorkerStorageTests(unittest.TestCase):
    def setUp(self):
        self.worker = worker_tests.RetrainingWorkerTests()
        self.worker.setUp()
        self.addCleanup(self.worker.doCleanups)
        self.path = self.worker.root / "runtime/retraining/runs" / self.worker.request_id / "result.json"

    def test_failed_publication_leaves_running_and_blocks_replay(self):
        with patch("backend.result_storage.os.fsync", side_effect=OSError("injected storage failure")):
            with self.assertRaises(OSError):
                self.worker.execute()
        self.assertEqual(self.worker.status(), "running")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.worker.execute()["status"], "not_queued")
        self.assertEqual(len(self.worker.calls), 2)

    def test_database_failure_preserves_result_without_replaying_training(self):
        from backend.retraining_queue import connect
        real_connection = connect(self.worker.database)
        self.addCleanup(real_connection.close)
        class FailingConnection:
            def execute(self, query, parameters=()):
                if query.startswith("UPDATE requests SET status = ?"):
                    raise sqlite3.OperationalError("injected terminal update failure")
                return real_connection.execute(query, parameters)
            def __getattr__(self, name):
                return getattr(real_connection, name)
        with patch("backend.retraining_worker.connect", return_value=FailingConnection()):
            with self.assertRaises(sqlite3.OperationalError):
                self.worker.execute()
        self.assertEqual(self.worker.status(), "running")
        result = json.loads(self.path.read_text())
        self.assertEqual(result["status"], "succeeded")
        self.assertIs(result["registered_or_promoted"], False)
        self.assertEqual(self.worker.execute()["status"], "not_queued")
        self.assertEqual(len(self.worker.calls), 2)


if __name__ == "__main__":
    unittest.main()
