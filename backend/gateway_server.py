"""Construct the local canary gateway with explicit upstream settings."""

import json
from pathlib import Path

from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.model_client import ModelClient


ROOT = Path(__file__).resolve().parents[1]


def create_gateway():
    registry = json.loads(
        (ROOT / "reports/mlflow_registry.json").read_text()
    )["models"]["xgboost"]
    admin_key = (ROOT / "runtime/gateway-admin.key").read_text().strip()
    routing = CanaryRouter(
        champion=ModelClient(
            "http://127.0.0.1:5001",
            expected_threshold=registry["threshold"],
            timeout=2.0,
        ),
        challenger=ModelClient(
            "http://127.0.0.1:5002",
            expected_threshold=0.8567820191383362,
            timeout=2.0,
        ),
        weight=0,
        rollback_on_failure=True,
    )
    return create_app(routing, admin_key)
