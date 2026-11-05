"""Construct the local canary gateway with explicit upstream settings."""

import json
import os
from pathlib import Path

from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.model_client import ModelClient


ROOT = Path(__file__).resolve().parents[1]


def create_gateway():
    registry = json.loads(
        Path(os.environ.get(
            "GATEWAY_REGISTRY_FILE",
            str(ROOT / "reports/mlflow_registry.json"),
        )).read_text()
    )["models"]["xgboost"]
    admin_key = Path(os.environ.get(
        "GATEWAY_ADMIN_KEY_FILE",
        str(ROOT / "runtime/gateway-admin.key"),
    )).read_text().strip()
    timeout = float(os.environ.get("MODEL_TIMEOUT_SECONDS", "2.0"))
    routing = CanaryRouter(
        champion=ModelClient(
            os.environ.get("CHAMPION_URL", "http://127.0.0.1:5001"),
            expected_threshold=registry["threshold"],
            timeout=timeout,
        ),
        challenger=ModelClient(
            os.environ.get("CHALLENGER_URL", "http://127.0.0.1:5002"),
            expected_threshold=float(os.environ.get(
                "CHALLENGER_THRESHOLD", "0.8567820191383362"
            )),
            timeout=timeout,
        ),
        weight=0,
        rollback_on_failure=True,
    )
    return create_app(routing, admin_key)
