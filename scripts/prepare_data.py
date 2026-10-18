"""Create chronological PaySim splits without loading the dataset into RAM."""

import csv
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/paysim.csv"
OUTPUT = ROOT / "data/processed/paysim_v1"
TEMP = ROOT / "data/processed/paysim_v1.tmp"

EXPECTED_SHA256 = (
    "16910f90577b0d981bf8ff289714510bb89bc71bff7d3f220f024e287e4eea6b"
)
FIELDS = ["step", "type", "amount", "oldbalanceOrg", "isFraud"]
EXPECTED_COUNTS = {
    "train": (6082007, 5781),
    "validation": (191147, 1180),
    "test": (89466, 1252),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    if OUTPUT.exists() or TEMP.exists():
        raise SystemExit("STOP: output or temporary directory already exists.")

    print("Verifying raw dataset checksum...", flush=True)
    source_hash = sha256(SOURCE)
    if source_hash != EXPECTED_SHA256:
        raise SystemExit("FAIL: raw dataset checksum differs from validated input.")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir()
    summary = {
        name: {"rows": 0, "fraud": 0, "step_min": None, "step_max": None}
        for name in EXPECTED_COUNTS
    }

    with ExitStack() as stack:
        writers = {}
        for name in summary:
            handle = stack.enter_context(
                (TEMP / f"{name}.csv").open(
                    "x", newline="", encoding="utf-8"
                )
            )
            writers[name] = csv.DictWriter(handle, fieldnames=FIELDS)
            writers[name].writeheader()

        source = stack.enter_context(
            SOURCE.open(newline="", encoding="utf-8")
        )
        reader = csv.DictReader(source)
        if not set(FIELDS).issubset(reader.fieldnames or []):
            raise ValueError("Required columns are missing.")

        previous_step = 0
        for index, row in enumerate(reader, start=1):
            step = int(row["step"])
            if not 1 <= step <= 743 or step < previous_step:
                raise ValueError("Invalid or out-of-order step.")
            previous_step = step

            name = (
                "train" if step <= 520
                else "validation" if step <= 631
                else "test"
            )
            writers[name].writerow({field: row[field] for field in FIELDS})

            stats = summary[name]
            stats["rows"] += 1
            stats["fraud"] += int(row["isFraud"])
            if stats["step_min"] is None:
                stats["step_min"] = step
            stats["step_max"] = step

            if index % 1_000_000 == 0:
                print(f"Prepared {index:,} rows...", flush=True)

    for name, (rows, fraud) in EXPECTED_COUNTS.items():
        stats = summary[name]
        if (stats["rows"], stats["fraud"]) != (rows, fraud):
            raise ValueError(f"Unexpected counts in {name}.")
        stats["sha256"] = sha256(TEMP / f"{name}.csv")

    if not (
        summary["train"]["step_max"] < summary["validation"]["step_min"]
        and summary["validation"]["step_max"] < summary["test"]["step_min"]
    ):
        raise ValueError("Split time ranges overlap.")

    manifest = {
        "source_sha256": source_hash,
        "model_inputs": ["type", "amount", "oldbalanceOrg"],
        "audit_column": "step",
        "target": "isFraud",
        "train_end_step": 520,
        "validation_end_step": 631,
        "splits": summary,
    }
    with (TEMP / "manifest.json").open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2)
        handle.write("\n")

    TEMP.rename(OUTPUT)
    print(json.dumps(manifest, indent=2))
    print("PASS: checksum, split counts, and time separation verified.")
    print(f"Output: {OUTPUT}")


if __name__ == "__main__":
    main()

