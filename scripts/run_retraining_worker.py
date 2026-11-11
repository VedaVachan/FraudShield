"""Inspect queued requests or explicitly execute one request."""

import argparse
import json
from pathlib import Path
import subprocess

from backend.logging_config import configure_logging
from backend.retraining_queue import connect
from backend.retraining_recovery import exit_code, reconcile
from backend.retraining_worker import execute_request


ROOT = Path(__file__).resolve().parents[1]


def main():
    """Parse the command line and run the requested queue action."""
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database", type=Path,
        default=ROOT / "runtime/retraining/queue.db",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true")
    action.add_argument("--request-id")
    action.add_argument("--inspect-recovery", metavar="REQUEST_ID")
    action.add_argument("--recover", metavar="REQUEST_ID")
    parser.add_argument("--confirm-result-sha256")
    parser.add_argument("--reason")
    args = parser.parse_args()
    extras = (args.confirm_result_sha256, args.reason)
    if args.recover and not all(extras):
        parser.error("--recover needs --confirm-result-sha256 and --reason.")
    if not args.recover and any(extras):
        parser.error("--confirm-result-sha256/--reason are only for --recover.")

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

    if args.inspect_recovery:
        result = reconcile(args.database, args.inspect_recovery)
        print(json.dumps(result, indent=2, allow_nan=False))
        return exit_code(result)

    sources = [
        "backend/result_storage.py",
        "backend/process_runner.py",
        "backend/retraining_worker.py",
        "backend/retraining_queue.py",
        "backend/retraining_recovery.py",
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

    if args.recover:
        result = reconcile(
            args.database, args.recover, apply=True,
            confirm_result_sha256=args.confirm_result_sha256,
            reason=args.reason,
        )
        print(json.dumps(result, indent=2, allow_nan=False))
        return exit_code(result)

    result = execute_request(args.database, args.request_id)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
