"""Persistent drift-request queue; enqueuing never launches training."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4


FEATURES = {"type", "amount", "oldbalanceOrg"}
COOLDOWN_SECONDS = 3600


def connect(path):
    """Open the queue database, creating its tables if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS deliveries (
            delivery_id TEXT PRIMARY KEY,
            processed_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            scope TEXT NOT NULL,
            created_at REAL NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'succeeded', 'rejected', 'failed')
            ),
            source_delivery_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_request_per_scope
        ON requests(scope) WHERE status IN ('queued', 'running');
    """)
    return connection


def enqueue_notification(database, record, *, now=None):
    """Return queue decisions for one receiver record.

    Duplicate deliveries and active requests are checked in one transaction.
    A cooldown also applies after a request finishes.
    """
    if not isinstance(record, dict):
        raise ValueError("Expected a notification record.")

    delivery_id = record.get("delivery_id")
    if not isinstance(delivery_id, str):
        raise ValueError("Missing delivery ID.")
    UUID(delivery_id)

    if record.get("status") not in ("firing", "resolved"):
        raise ValueError("Invalid notification status.")
    alerts = record.get("alerts")
    if not isinstance(alerts, list):
        raise ValueError("Invalid alert list.")

    # Build one request per API instance, combining affected features.
    scopes = {}
    if record["status"] == "firing":
        for alert in alerts:
            if not isinstance(alert, dict):
                raise ValueError("Invalid alert.")
            labels = alert.get("labels", {})
            if not isinstance(labels, dict):
                raise ValueError("Invalid labels.")
            if (
                alert.get("status") != "firing"
                or labels.get("alertname") != "FraudShieldInputDrift"
                or labels.get("job") != "fraudshield-api"
                or labels.get("feature") not in FEATURES
            ):
                continue
            instance = labels.get("instance")
            if not isinstance(instance, str) or not 1 <= len(instance) <= 256:
                raise ValueError("Invalid API instance.")
            scope = "fraudshield-api/" + instance
            scopes.setdefault(scope, set()).add(labels["feature"])

    clock = datetime.now(timezone.utc).timestamp() if now is None else now
    if isinstance(clock, bool) or not isinstance(clock, (int, float)):
        raise ValueError("Invalid queue timestamp.")
    import math
    if not math.isfinite(clock) or clock < 0:
        raise ValueError("Invalid queue timestamp.")

    connection = connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone():
            connection.commit()
            return [{"decision": "duplicate_delivery"}]

        decisions = []
        for scope, features in sorted(scopes.items()):
            active = connection.execute(
                "SELECT request_id FROM requests "
                "WHERE scope = ? AND status IN ('queued', 'running')",
                (scope,),
            ).fetchone()
            if active:
                decisions.append({
                    "decision": "already_active",
                    "request_id": active["request_id"],
                    "scope": scope,
                })
                continue

            latest = connection.execute(
                "SELECT MAX(created_at) FROM requests WHERE scope = ?", (scope,)
            ).fetchone()[0]
            if latest is not None and clock - latest < COOLDOWN_SECONDS:
                decisions.append({"decision": "cooldown", "scope": scope})
                continue

            request_id = str(uuid4())
            evidence = {
                "alertname": "FraudShieldInputDrift",
                "features": sorted(features),
                "receiver_delivery_id": delivery_id,
                "receiver_received_utc": record.get("received_utc"),
                "training_data_policy": "existing approved train/validation splits",
                "automatic_promotion": False,
            }
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, 'queued', ?, ?)",
                (
                    request_id, scope, clock, delivery_id,
                    json.dumps(evidence, allow_nan=False),
                ),
            )
            decisions.append({
                "decision": "queued", "request_id": request_id, "scope": scope,
            })

        connection.execute(
            "INSERT INTO deliveries VALUES (?, ?)", (delivery_id, clock)
        )
        connection.commit()
        return decisions or [{"decision": "ignored"}]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
