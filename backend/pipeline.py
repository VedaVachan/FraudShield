"""Read-only API for recorded pipeline command executions."""

from collections import deque
import json
import math
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query


ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "runtime/pipeline_runs.jsonl"
MAX_BYTES = 1024 * 1024
router = APIRouter()


def short_text(value, length=200):
    return value[:length] if isinstance(value, str) else None


def read_history(path, limit):
    if not path.exists():
        return {
            "history_available": False,
            "truncated": False,
            "invalid_records": 0,
            "runs": [],
        }

    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        start = max(0, size - MAX_BYTES)
        handle.seek(start)
        if start:
            handle.readline()
        lines = handle.read(MAX_BYTES).splitlines()

    runs = deque(maxlen=limit)
    invalid = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Record must be an object.")
            if row.get("status") not in ("success", "failed"):
                raise ValueError("Unsupported status.")
            duration = row.get("duration_seconds")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration < 0
            ):
                raise ValueError("Invalid duration.")

            gate = row.get("gate")
            failures = gate.get("failures", []) if isinstance(gate, dict) else []
            runs.append({
                "run_id": short_text(row.get("run_id")),
                "stage": short_text(row.get("stage")),
                "status": row["status"],
                "started_utc": short_text(row.get("started_utc")),
                "finished_utc": short_text(row.get("finished_utc")),
                "duration_seconds": duration,
                "git_commit": short_text(row.get("git_commit"), 64),
                "git_dirty": row.get("git_dirty") is True,
                "failure_reasons": [
                    item[:300] for item in failures[:10]
                    if isinstance(item, str)
                ] if isinstance(failures, list) else [],
            })
        except (ValueError, TypeError, UnicodeError):
            invalid += 1

    return {
        "history_available": True,
        "truncated": start > 0,
        "invalid_records": invalid,
        "runs": list(reversed(runs)),
    }


@router.get("/pipeline/runs")
def pipeline_runs(limit: int = Query(default=50, ge=1, le=100)):
    try:
        return read_history(HISTORY, limit)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Pipeline history is temporarily unavailable.",
        ) from exc
