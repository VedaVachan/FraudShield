"""Receive local Alertmanager notifications and preserve delivery evidence."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request


MAX_BYTES = 65536
MAX_ALERTS = 20
ALLOWED_LABELS = ("alertname", "severity", "job", "instance", "feature")


def normalize_notification(payload):
    if not isinstance(payload, dict):
        raise ValueError("Expected an object.")
    if payload.get("status") not in ("firing", "resolved"):
        raise ValueError("Invalid notification status.")

    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not 1 <= len(alerts) <= MAX_ALERTS:
        raise ValueError("Expected between 1 and 20 alerts.")

    normalized = []
    for alert in alerts:
        if not isinstance(alert, dict):
            raise ValueError("Invalid alert.")
        if alert.get("status") not in ("firing", "resolved"):
            raise ValueError("Invalid alert status.")
        labels = alert.get("labels")
        if not isinstance(labels, dict):
            raise ValueError("Invalid labels.")
        if not isinstance(labels.get("alertname"), str) or not labels["alertname"]:
            raise ValueError("Missing alert name.")

        selected = {}
        for key in ALLOWED_LABELS:
            if key in labels:
                value = labels[key]
                if not isinstance(value, str) or len(value) > 256:
                    raise ValueError("Invalid label value.")
                selected[key] = value

        normalized.append({"status": alert["status"], "labels": selected})

    return {"status": payload["status"], "alerts": normalized}


def create_app(history=None):
    destination = Path(
        history if history is not None else os.environ.get(
            "ALERT_HISTORY_FILE", "runtime/alerts/notifications.jsonl"
        )
    )
    lock = Lock()
    app = FastAPI(title="FraudShield Local Alert Receiver")

    @app.get("/health")
    def health():
        return {"status": "configured"}

    @app.post("/alerts")
    async def receive_alerts(request: Request):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BYTES:
                raise HTTPException(status_code=413, detail="Notification too large.")

        try:
            notification = normalize_notification(json.loads(body))
        except (ValueError, UnicodeError):
            raise HTTPException(status_code=422, detail="Invalid notification.")

        record = {
            "schema_version": 1,
            "delivery_id": str(uuid4()),
            "received_utc": datetime.now(timezone.utc).isoformat(),
            **notification,
        }
        try:
            with lock:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, allow_nan=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError:
            raise HTTPException(
                status_code=503, detail="Notification storage unavailable."
            )

        return {"status": "recorded", "delivery_id": record["delivery_id"]}

    return app
