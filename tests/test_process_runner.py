"""Exercise real dummy subprocesses; no training, model loading or live queue."""

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

from backend.process_runner import run_process


def running(pid):
    # pidfds distinguish an exited/zombie process from a live process without
    # depending on how /proc is mounted in the test environment.
    import select
    try:
        descriptor = os.pidfd_open(pid)
    except AttributeError:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return False
        state = stat.rsplit(")", 1)[1].split()[0]
        return state not in ("Z", "X")
    except ProcessLookupError:
        return False
    try:
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        return not poller.poll(0)
    finally:
        os.close(descriptor)


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Dummy process did not reach the expected state.")


def make_process_tree(root):
    parent = root / "parent.py"
    pids = root / "pids.json"
    child = root / "child.py"
    child.write_text(
        "import signal,time\nfrom pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path(__file__).with_suffix('.ready').write_text('ready')\n"
        "time.sleep(30)\n"
    )
    parent.write_text(
        "import subprocess,sys,time,os,json\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, {str(child)!r}])\n"
        f"ready = Path({str(child.with_suffix('.ready'))!r})\n"
        "while not ready.exists(): time.sleep(0.01)\n"
        f"Path({str(pids)!r}).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(30)\n"
    )
    return parent, pids


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.parent, self.pids = make_process_tree(self.root)
        self.ids = None
        self.addCleanup(self.cleanup_group)

    def cleanup_group(self):
        if self.pids.exists():
            ids = json.loads(self.pids.read_text())
            try:
                os.killpg(ids[0], signal.SIGKILL)
            except ProcessLookupError:
                for pid in ids:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def assert_children_stopped(self):
        ids = json.loads(self.pids.read_text())
        wait_for(lambda: all(not running(pid) for pid in ids))

    def test_return_code_and_file_output(self):
        with (self.root / "output.txt").open("w") as output:
            result = run_process(
                [sys.executable, "-c", "print('done');raise SystemExit(3)"],
                timeout=5, stdout=output, stderr=subprocess.STDOUT,
            )
        self.assertEqual(result.returncode, 3)
        self.assertEqual((self.root / "output.txt").read_text().strip(), "done")

    def test_timeout_stops_child_and_term_resistant_grandchild(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, str(self.parent)], timeout=2, cleanup_grace=0.15)
        self.assert_children_stopped()

    def test_sigterm_interrupts_runner_and_cleans_process_group(self):
        result = self.root / "interrupted.txt"
        code = (
            "from backend.process_runner import run_process,ProcessInterrupted\n"
            "from pathlib import Path\n"
            "try:\n"
            f" run_process([{sys.executable!r},{str(self.parent)!r}],timeout=20,cleanup_grace=0.15)\n"
            "except ProcessInterrupted as error:\n"
            f" Path({str(result)!r}).write_text(str(error))\n"
        )
        driver = subprocess.Popen([sys.executable, "-c", code])
        try:
            wait_for(self.pids.exists)
            driver.send_signal(signal.SIGTERM)
            self.assertEqual(driver.wait(timeout=8), 0)
            self.assertIn("SIGTERM", result.read_text())
            self.assert_children_stopped()
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.wait()

    def test_child_retains_lock_after_worker_sigkill(self):
        lock_path = self.root / "worker.lock"
        code = (
            "import fcntl\nfrom backend.process_runner import run_process\n"
            f"with open({str(lock_path)!r},'a') as lock:\n"
            " fcntl.flock(lock,fcntl.LOCK_EX)\n"
            f" run_process([{sys.executable!r},{str(self.parent)!r}],timeout=20,"
            "pass_fds=(lock.fileno(),),cleanup_grace=0.15)\n"
        )
        driver = subprocess.Popen([sys.executable, "-c", code])
        try:
            wait_for(self.pids.exists)
            driver.kill()
            driver.wait(timeout=5)
            with lock_path.open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.cleanup_group()
                self.assert_children_stopped()
                wait_for(lambda: self.acquire(lock))
        finally:
            if driver.poll() is None:
                driver.kill()
                driver.wait()

    @staticmethod
    def acquire(lock):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


if __name__ == "__main__":
    unittest.main()
