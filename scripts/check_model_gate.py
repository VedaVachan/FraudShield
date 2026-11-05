"""Check a saved XGBoost validation report without opening the test set."""

import argparse
import hashlib
import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir", type=Path,
        default=ROOT / "artifacts/xgboost_dvc_v1",
    )
    parser.add_argument("--min-ap", type=float, default=0.85)
    parser.add_argument("--min-precision", type=float, default=0.70)
    parser.add_argument("--min-recall", type=float, default=0.90)
    args = parser.parse_args()

    floors = {
        "average_precision": args.min_ap,
        "precision": args.min_precision,
        "recall": args.min_recall,
    }
    for name, floor in floors.items():
        if not math.isfinite(floor) or not 0 <= floor <= 1:
            parser.error(f"{name} floor must be finite and in [0, 1].")

    report_path = args.model_dir / "validation.json"
    model_path = args.model_dir / "model.joblib"
    failures = []
    measured = {}
    identity = {}

    try:
        report = json.loads(report_path.read_text())
        identity["report_sha256"] = digest(report_path)
        identity["model_sha256"] = digest(model_path)

        if report["model"] != "xgboost":
            failures.append("Expected an XGBoost baseline report.")
        if report["evaluation_split"] != "validation":
            failures.append("Only validation reports are allowed.")
        if report["test_evaluated"] is not False:
            failures.append("Report must declare test_evaluated=false.")
        if report["serialization_check"] != "PASS":
            failures.append("Serialization check did not pass.")
        if report["model_sha256"] != identity["model_sha256"]:
            failures.append("Model checksum differs from the report.")

        selected = report["at_selected_threshold"]
        measured = {
            "average_precision": report["average_precision"],
            "precision": selected["precision"],
            "recall": selected["recall"],
            "threshold": selected["threshold"],
        }
        for name, value in measured.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                failures.append(f"{name} must be finite and in [0, 1].")
            elif name in floors and value < floors[name]:
                failures.append(f"{name}={value} is below {floors[name]}.")

    except (OSError, ValueError, KeyError, TypeError) as exc:
        failures.append(f"Artifact/report validation failed: {exc}")

    print(json.dumps({
        "status": "FAIL" if failures else "PASS",
        "scope": "saved validation report and model checksum",
        "policy": floors,
        "measured": measured,
        "identity": identity,
        "failures": failures,
        "test_set_opened": False,
    }, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
