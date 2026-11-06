"""Check interruption state using a private SQLite queue and dummy training."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from backend.retraining_queue import connect, enqueue_notification
from backend.retraining_worker import execute_request
import test_process_runner as helpers


class WorkerInterruptionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        parent, self.pids = helpers.make_process_tree(self.root)
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "train_xgboost_tracked.py").write_text(parent.read_text())
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
        self.addCleanup(self.cleanup_group)
        code = (
            "from backend.retraining_worker import execute_request\n"
            f"execute_request({str(self.database)!r},{self.request_id!r},root={str(self.root)!r})\n"
        )
        # Capture output to files; no pipe is inherited by a lingering child.
        self.log = (self.root / "driver.log").open("w")
        self.addCleanup(self.log.close)
        self.driver = subprocess.Popen(
            [sys.executable, "-c", code], stdout=self.log, stderr=subprocess.STDOUT,
        )
        self.addCleanup(self.stop_driver)
        helpers.wait_for(self.pids.exists)

    def stop_driver(self):
        if self.driver.poll() is None:
            self.driver.kill()
            self.driver.wait()

    def cleanup_group(self):
        if self.pids.exists():
            try:
                os.killpg(json.loads(self.pids.read_text())[0], signal.SIGKILL)
            except ProcessLookupError:
                for pid in json.loads(self.pids.read_text()):
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def status(self):
        connection = connect(self.database)
        try:
            return connection.execute(
                "SELECT status FROM requests WHERE request_id=?", (self.request_id,)
            ).fetchone()[0]
        finally:
            connection.close()

    def test_sigterm_stops_descendants_before_recording_failure(self):
        self.driver.send_signal(signal.SIGTERM)
        self.assertEqual(self.driver.wait(timeout=10), 0)
        ids = json.loads(self.pids.read_text())
        helpers.wait_for(lambda: all(not helpers.running(pid) for pid in ids))
        self.assertEqual(self.status(), "failed")
        result_path = self.root / "runtime/retraining/runs" / self.request_id / "result.json"
        result = json.loads(result_path.read_text())
        self.assertEqual(result["status"], "failed")
        self.assertIn("SIGTERM", result["error"])
        self.assertNotIn("gate_command", result)
        self.assertIs(result["registered_or_promoted"], False)
        self.assertEqual(
            execute_request(self.database, self.request_id, root=self.root)["status"],
            "not_queued",
        )

    def test_sigkill_retains_running_state_and_child_holds_lock(self):
        self.driver.kill()
        self.driver.wait(timeout=5)
        self.assertEqual(self.status(), "running")
        result = execute_request(self.database, self.request_id, root=self.root)
        self.assertEqual(result["status"], "busy")
        self.cleanup_group()
        ids = json.loads(self.pids.read_text())
        helpers.wait_for(lambda: all(not helpers.running(pid) for pid in ids))
        # Lock release must not silently requeue a possibly executed request.
        import time
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            result = execute_request(self.database, self.request_id, root=self.root)
            if result["status"] != "busy":
                break
            time.sleep(0.02)
        self.assertEqual(result["status"], "not_queued")
        self.assertEqual(result["request_status"], "running")


if __name__ == "__main__":
    unittest.main()
