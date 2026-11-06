"""Scan recent local notifications and queue eligible drift requests."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from backend.retraining_queue import enqueue_notification


ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1024 * 1024
MAX_AGE_SECONDS = 900


def scan_once(history, database, *, now=None):
    clock = datetime.now(timezone.utc).timestamp() if now is None else now
    summary = {
        "history_available": False,
        "invalid_records": 0,
        "outside_time_window": 0,
        "decisions": {},
        "new_requests": [],
    }
    try:
        with Path(history).open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - MAX_BYTES)
            handle.seek(start)
            data = handle.read(MAX_BYTES)
    except FileNotFoundError:
        return summary

    summary["history_available"] = True
    if start:
        # The first line may have been cut by the bounded tail read.
        boundary = data.find(b"\n")
        data = data[boundary + 1:] if boundary >= 0 else b""

    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            summary["invalid_records"] += 1
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("Expected a record.")
            received = datetime.fromisoformat(record["received_utc"])
            if received.tzinfo is None:
                raise ValueError("Expected a timezone.")
            age = clock - received.timestamp()
            if not -60 <= age <= MAX_AGE_SECONDS:
                summary["outside_time_window"] += 1
                continue
            # Validate input separately from storage errors.
            if not isinstance(record.get("delivery_id"), str):
                raise ValueError("Missing delivery ID.")
            from uuid import UUID
            UUID(record["delivery_id"])
        except (ValueError, TypeError, KeyError, UnicodeError, OverflowError):
            summary["invalid_records"] += 1
            continue

        try:
            decisions = enqueue_notification(database, record, now=clock)
        except (ValueError, TypeError):
            summary["invalid_records"] += 1
            continue

        for decision in decisions:
            name = decision["decision"]
            summary["decisions"][name] = summary["decisions"].get(name, 0) + 1
            if name == "queued":
                summary["new_requests"].append(decision)

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history", type=Path,
        default=ROOT / "runtime/alerts/notifications.jsonl",
    )
    parser.add_argument(
        "--database", type=Path,
        default=ROOT / "runtime/retraining/queue.db",
    )
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()

    while True:
        result = scan_once(args.history, args.database)
        print(json.dumps(result, allow_nan=False), flush=True)
        if not args.watch:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
