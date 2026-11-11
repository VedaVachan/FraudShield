"""Inspect queued requests, execute one request, or (opt-in) watch the queue.

Drift detection and queueing are automatic. Execution is automatic only with --watch,
which is off by default. Watching uses exactly the same safe path as --request-id: the
worker lock, the committed-source check and candidate-only training plus the validation
gate. It never registers, promotes or deploys a model; promotion stays a separate,
manual, gated step.
"""

import argparse
import json
import logging
import math
from pathlib import Path
import signal
import subprocess
import threading

from backend.logging_config import configure_logging
from backend.retraining_queue import connect
from backend.retraining_recovery import exit_code, reconcile
from backend.retraining_worker import execute_request


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)
DEFAULT_INTERVAL = 30.0
SOURCES = [
    "backend/result_storage.py",
    "backend/process_runner.py",
    "backend/retraining_worker.py",
    "backend/retraining_queue.py",
    "backend/retraining_recovery.py",
    "scripts/run_retraining_worker.py",
    "scripts/train_xgboost_tracked.py",
    "scripts/check_model_gate.py",
]
INTERRUPTED_PREFIX = "Command interrupted by"


def verify_committed_sources(root=ROOT, sources=SOURCES):
    """Raise unless every worker source is tracked and identical to HEAD."""
    subprocess.run(
        ["git", "ls-files", "--error-unmatch", *sources],
        cwd=root, check=True, stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", *sources],
        cwd=root, check=True,
    )


def queued_request_ids(database):
    """Return the IDs of queued requests, oldest first (read-only query)."""
    connection = connect(database)
    try:
        return [
            row["request_id"] for row in connection.execute(
                "SELECT request_id FROM requests WHERE status = 'queued' "
                "ORDER BY created_at ASC, request_id ASC"
            )
        ]
    finally:
        connection.close()


def watch(database, interval, *, execute=execute_request,
          verify=verify_committed_sources, stop=None, max_cycles=None):
    """Poll the queue and run queued requests one at a time; return an exit code.

    Each request goes through `execute` (the normal worker path, which takes the worker
    lock and records its own outcome). The loop stops with code 0 on SIGINT or SIGTERM
    (or when `stop` is set) and with code 1 if the worker sources are not committed
    or a request aborts before recording a terminal state. A request is never retried:
    terminal states are final, and a "busy" lock is simply tried again next cycle.
    """
    if (isinstance(interval, bool) or not isinstance(interval, (int, float))
            or not math.isfinite(interval) or interval <= 0):
        raise ValueError("The watch interval must be a positive, finite number of seconds.")
    stop = threading.Event() if stop is None else stop

    def request_stop(signum, frame):
        LOGGER.info("Watch mode received %s; stopping after the current step.",
                    signal.Signals(signum).name)
        stop.set()

    previous = {}
    if threading.current_thread() is threading.main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, request_stop)
    LOGGER.info(
        "Watch mode started: database=%s interval=%ss. Requests run candidate-only; "
        "nothing is registered, promoted or deployed.", database, interval,
    )
    try:
        cycles = 0
        while not stop.is_set():
            cycles += 1
            for request_id in queued_request_ids(database):
                if stop.is_set():
                    break
                try:
                    verify()
                except (subprocess.CalledProcessError, OSError):
                    LOGGER.error(
                        "Worker sources are not committed and unchanged; watch mode "
                        "stops without running request %s.", request_id,
                    )
                    return 1
                LOGGER.info("Watch mode: running queued request %s.", request_id)
                try:
                    result = execute(database, request_id)
                except Exception:
                    LOGGER.error(
                        "Watch mode: request %s aborted before a terminal state was "
                        "recorded; stopping so an operator can inspect it.",
                        request_id, exc_info=True,
                    )
                    return 1
                status = result.get("status")
                LOGGER.info("Watch mode: request %s finished with status %s.",
                            request_id, status)
                if status == "busy":
                    LOGGER.warning("Watch mode: the worker lock is held elsewhere; "
                                   "will try again next cycle.")
                    break
                if str(result.get("error", "")).startswith(INTERRUPTED_PREFIX):
                    stop.set()
                    break
            if max_cycles is not None and cycles >= max_cycles:
                break
            stop.wait(interval)
        LOGGER.info("Watch mode stopped.")
        return 0
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv=None):
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
    action.add_argument(
        "--watch", action="store_true",
        help="opt-in: poll the queue and run queued requests automatically "
             "(candidate-only; never registers, promotes or deploys)",
    )
    parser.add_argument("--confirm-result-sha256")
    parser.add_argument("--reason")
    parser.add_argument(
        "--interval", type=float, metavar="SECONDS",
        help=f"seconds between queue polls with --watch (default {DEFAULT_INTERVAL:g})",
    )
    args = parser.parse_args(argv)
    extras = (args.confirm_result_sha256, args.reason)
    if args.recover and not all(extras):
        parser.error("--recover needs --confirm-result-sha256 and --reason.")
    if not args.recover and any(extras):
        parser.error("--confirm-result-sha256/--reason are only for --recover.")
    if args.interval is not None and not args.watch:
        parser.error("--interval is only for --watch.")
    if args.watch:
        if args.interval is None:
            args.interval = DEFAULT_INTERVAL
        if not math.isfinite(args.interval) or args.interval <= 0:
            parser.error("--interval must be a positive number of seconds.")

    if args.watch:
        return watch(args.database, args.interval)

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

    verify_committed_sources()

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
