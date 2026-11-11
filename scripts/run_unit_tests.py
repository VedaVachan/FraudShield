"""Run the unit-test suite and record a small result file for the DVC stage."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "reports/unit_tests_result.json"


def git_state(root):
    """Return the current commit hash and whether the working tree has changes."""
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    dirty = bool(subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True
    ).strip())
    return commit, dirty


def spark_environment(python, root):
    """Environment variables scripts/run_ci.py sets for the test run."""
    return {
        "PYTHONPATH": f"{Path(root) / 'src'}:{root}",
        "PYSPARK_PYTHON": str(python),
        "PYSPARK_DRIVER_PYTHON": str(python),
        "SPARK_LOCAL_IP": "127.0.0.1",
        "PYSPARK_SUBMIT_ARGS": "--driver-memory 1g pyspark-shell",
    }


def summarize(result, commit, dirty, seconds):
    """Turn a unittest result into the JSON-serializable record."""
    return {
        "schema_version": 1,
        "command": "unittest discover -s tests",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "successful": result.wasSuccessful(),
        "python_version": platform.python_version(),
        "git_commit": commit,
        "git_dirty": dirty,
        "duration_seconds": round(seconds, 3),
        "finished_utc": datetime.now(timezone.utc).isoformat(),
    }


def run(test_dir, result_path, root, stream=None):
    """Discover and run tests, write the result file and return an exit code."""
    stream = sys.stderr if stream is None else stream
    suite = unittest.TestLoader().discover(
        str(test_dir), top_level_dir=str(test_dir)
    )
    started = time.perf_counter()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    commit, dirty = git_state(root)
    record = summarize(result, commit, dirty, time.perf_counter() - started)

    result_path = Path(result_path)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return 0 if record["successful"] else 1


def main():
    """Set the CI environment, run the project tests and return the exit code."""
    os.environ.update(spark_environment(sys.executable, ROOT))
    sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
    os.chdir(ROOT)
    return run(ROOT / "tests", RESULT, ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
