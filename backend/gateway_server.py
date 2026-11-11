"""Construct the local canary gateway with explicit upstream settings."""

import json
import logging
import os
from pathlib import Path

from backend.canary import CanaryRouter
from backend.gateway import create_app
from backend.logging_config import configure_logging
from backend.model_client import ModelClient


ROOT = Path(__file__).resolve().parents[1]
LOGGER = logging.getLogger(__name__)


def create_gateway():
    """Build the canary gateway from the registry receipt, key file and environment."""
    configure_logging()
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
            expected_sha256=registry["model_sha256"],
            timeout=timeout,
        ),
        challenger=ModelClient(
            os.environ.get("CHALLENGER_URL", "http://127.0.0.1:5002"),
            expected_threshold=float(os.environ.get(
                "CHALLENGER_THRESHOLD", "0.8567820191383362"
            )),
            expected_sha256=os.environ["CHALLENGER_MODEL_SHA256"],
            timeout=timeout,
        ),
        weight=0,
        rollback_on_failure=True,
    )
    LOGGER.info(
        "Gateway configured: champion_sha256_prefix=%s "
        "challenger_sha256_prefix=%s weight=0 rollback_on_failure=True "
        "timeout_seconds=%s",
        registry["model_sha256"][:12],
        os.environ["CHALLENGER_MODEL_SHA256"][:12], timeout,
    )
    return create_app(routing, admin_key)
