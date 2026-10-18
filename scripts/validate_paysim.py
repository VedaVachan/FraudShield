"""Validate PaySim sequentially using Python's standard library."""

import csv
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/paysim.csv"
REPORT = ROOT / "reports/paysim_validation.json"

COLUMNS = [
    "step", "type", "amount", "nameOrig", "oldbalanceOrg",
    "newbalanceOrig", "nameDest", "oldbalanceDest",
    "newbalanceDest", "isFraud", "isFlaggedFraud",
]
NUMERIC = [
    "amount", "oldbalanceOrg", "newbalanceOrig",
    "oldbalanceDest", "newbalanceDest",
]
TYPES = {"CASH_IN", "CASH_OUT", "DEBIT", "PAYMENT", "TRANSFER"}


def main():
    if REPORT.exists():
        raise SystemExit(f"STOP: report already exists: {REPORT}")

    errors = Counter()
    types = Counter()
    labels = Counter()
    flagged = Counter()
    fraud_by_type = Counter()
    rows = 0
    minimum_step = None
    maximum_step = None
    previous_step = None
    step_decreases = 0

    with SOURCE.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        if next(reader, None) != COLUMNS:
            raise SystemExit("FAIL: CSV header differs from expected schema.")

        for row in reader:
            rows += 1
            if len(row) != len(COLUMNS):
                errors["incorrect_column_count"] += 1
                continue

            record = dict(zip(COLUMNS, row))

            for column, value in record.items():
                if not value.strip():
                    errors[f"missing:{column}"] += 1

            transaction_type = record["type"]
            if transaction_type in TYPES:
                types[transaction_type] += 1
            else:
                errors["invalid_type"] += 1

            for column in NUMERIC:
                try:
                    value = float(record[column])
                    if not math.isfinite(value) or value < 0:
                        raise ValueError
                except ValueError:
                    errors[f"invalid_numeric:{column}"] += 1

            try:
                step = int(record["step"])
                if step < 1:
                    raise ValueError
                minimum_step = step if minimum_step is None else min(minimum_step, step)
                maximum_step = step if maximum_step is None else max(maximum_step, step)
                if previous_step is not None and step < previous_step:
                    step_decreases += 1
                previous_step = step
            except ValueError:
                errors["invalid_step"] += 1

            for column, counter in (
                ("isFraud", labels),
                ("isFlaggedFraud", flagged),
            ):
                value = record[column]
                if value in {"0", "1"}:
                    counter[value] += 1
                else:
                    errors[f"invalid_label:{column}"] += 1

            if record["isFraud"] == "1" and transaction_type in TYPES:
                fraud_by_type[transaction_type] += 1

            if rows % 1_000_000 == 0:
                print(f"Checked {rows:,} rows...", file=sys.stderr, flush=True)

    if rows == 0:
        errors["empty_dataset"] += 1

    result = {
        "source": "data/raw/paysim.csv",
        "file_size_bytes": SOURCE.stat().st_size,
        "rows": rows,
        "transaction_types": dict(types),
        "fraud_labels": dict(labels),
        "flagged_labels": dict(flagged),
        "fraud_by_type": dict(fraud_by_type),
        "fraud_fraction": labels["1"] / rows if rows else None,
        "step_min": minimum_step,
        "step_max": maximum_step,
        "step_decreases": step_decreases,
        "errors": dict(errors),
        "status": "PASS" if not errors else "FAIL",
    }

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with REPORT.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")

    print(json.dumps(result, indent=2))
    print(f"Report saved: {REPORT}")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
