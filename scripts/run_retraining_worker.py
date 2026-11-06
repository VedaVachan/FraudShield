"""Inspect queued requests or explicitly execute one request."""

import argparse
import json
from pathlib import Path
import subprocess

from backend.retraining_queue import connect
from backend.retraining_worker import execute_request


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path,
        default=ROOT / "runtime/retraining/queue.db",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true")
    action.add_argument("--request-id")
    args = parser.parse_args()

    if args.list:
        connection = connect(args.database)
        try:
            rows = [
                dict(row) for row in connection.execute(
                    "SELECT request_id, scope, created_at, status "
                    "FROM requests ORDER BY created_at DESC LIMIT 50"
                )
            ]
        finally:
            connection.close()
        print(json.dumps(rows, indent=2))
        return 0

    sources = [
        "backend/process_runner.py",
        "backend/retraining_worker.py",
        "backend/retraining_queue.py",
        "scripts/run_retraining_worker.py",
        "scripts/train_xgboost_tracked.py",
        "scripts/check_model_gate.py",
    ]
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", *sources],
        cwd=ROOT, check=True, stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", *sources],
        cwd=ROOT, check=True,
    )

    result = execute_request(args.database, args.request_id)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
