"""Run local tests and DVC reproduction, recording actual command outcomes."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]


def git_output(*args):
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True
    ).strip()


def main():
    python = ROOT / ".venv/bin/python"
    dvc = ROOT / "runtime/dvc-venv/bin/dvc"
    for executable in (python, dvc):
        if not executable.is_file():
            raise SystemExit(f"Missing executable: {executable}")

    run_id = str(uuid.uuid4())
    folder = ROOT / "runtime/ci" / run_id
    folder.mkdir(parents=True)
    commit = git_output("rev-parse", "HEAD")
    dirty = bool(git_output("status", "--porcelain"))

    env = os.environ.copy()
    env.update({
        "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
        "PYSPARK_PYTHON": str(python),
        "PYSPARK_DRIVER_PYTHON": str(python),
        "SPARK_LOCAL_IP": "127.0.0.1",
        "PYSPARK_SUBMIT_ARGS": "--driver-memory 1g pyspark-shell",
        "DVC_NO_ANALYTICS": "1",
    })
    steps = [
        ("automated_tests", [
            str(python), "-m", "unittest",
            "discover", "-s", "tests", "-v",
        ]),
        ("dvc_reproduction", [str(dvc), "repro"]),
    ]

    for stage, command in steps:
        log_path = folder / f"{stage}.log"
        started = datetime.now(timezone.utc).isoformat()
        clock = time.perf_counter()
        print(f"Running {stage}; log: {log_path}", flush=True)

        with log_path.open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    command, cwd=ROOT, env=env,
                    stdout=log, stderr=subprocess.STDOUT, check=False,
                )
                code = result.returncode
            except OSError as exc:
                log.write(f"Command could not start: {exc}\n")
                code = 127

        record = {
            "schema_version": 1,
            "run_id": run_id,
            "stage": stage,
            "status": "success" if code == 0 else "failed",
            "started_utc": started,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": time.perf_counter() - clock,
            "git_commit": commit,
            "git_dirty": dirty,
            "command": command,
            "exit_code": code,
            "log_path": log_path.relative_to(ROOT).as_posix(),
            "scope": "command execution; cached DVC stages may be skipped",
        }
        with (ROOT / "runtime/pipeline_runs.jsonl").open(
            "a", encoding="utf-8"
        ) as history:
            history.write(json.dumps(record, allow_nan=False) + "\n")

        print(log_path.read_text(), flush=True)
        if code != 0:
            print(f"FAIL: {stage}; later steps were not run.", flush=True)
            return 1

    print(f"PASS: local CI completed. Run ID: {run_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
