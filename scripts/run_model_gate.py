"""Run the validation gate and append an auditable execution record."""

import argparse
from datetime import datetime, timezone
import json
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-ap", type=float, default=0.85)
    args = parser.parse_args()

    command = [
        sys.executable, str(ROOT / "scripts/check_model_gate.py"),
        "--min-ap", str(args.min_ap),
    ]
    commit = git_output("rev-parse", "HEAD")
    dirty = bool(git_output("status", "--porcelain"))
    started = datetime.now(timezone.utc).isoformat()
    clock = time.perf_counter()

    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, check=False,
    )
    duration = time.perf_counter() - clock

    try:
        gate = json.loads(result.stdout)
    except ValueError:
        gate = None

    passed = (
        result.returncode == 0
        and isinstance(gate, dict)
        and gate.get("status") == "PASS"
    )
    record = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "stage": "xgboost_validation_gate",
        "status": "success" if passed else "failed",
        "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": duration,
        "git_commit": commit,
        "git_dirty": dirty,
        "command": command,
        "exit_code": result.returncode,
        "gate": gate,
        "stderr": result.stderr[-4000:],
    }

    history = ROOT / "runtime/pipeline_runs.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, allow_nan=False) + "\n")

    print(json.dumps(record, indent=2, allow_nan=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
