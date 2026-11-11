"""Drill the registry-alias rollback on a TEMPORARY COPY of the MLflow database.

The real database (runtime/mlflow/tracking.db) is only read, through SQLite's backup
API in read-only mode, to make the copy. Every change is made on the copy, which is
deleted when the drill ends. The SHA-256 of the real database is recorded before and
after to show it was untouched; if it differs the drill fails.

Steps on the copy: register a second, synthetic version of FraudShield-XGBoost;
promote v1 (champion = v1); promote v2 (champion = v2, previous recorded as v1);
roll back and check that champion is v1 again. The steps call scripts/promote_model.py
unchanged. Only registry aliases and one registered-model tag move. What this does NOT
cover: the API and gateway do not read aliases, so the drill says nothing about which
model is served (see "Serving rollback" in docs/operator_guide.md).

The report reports/rollback_drill.json is create-only and is never overwritten.
Exit code: 0 all steps behaved as expected, 1 a step did not, 2 refused or could not run.
"""

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mlflow.tracking import MlflowClient


ROOT = Path(__file__).resolve().parents[1]
REAL_DATABASE = ROOT / "runtime/mlflow/tracking.db"
OUTPUT = ROOT / "reports/rollback_drill.json"
MODEL = "FraudShield-XGBoost"
DRILL_EXPERIMENT = "FraudShield-Rollback-Drill"
IMPROVEMENT = 0.0005

SPEC = importlib.util.spec_from_file_location(
    "promote_model_for_drill", ROOT / "scripts/promote_model.py"
)
promote = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promote)


class DrillRefused(RuntimeError):
    """The drill was pointed somewhere it must never write (exit code 2)."""


def now():
    """Current UTC time as an ISO string."""
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path):
    """SHA-256 of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def same_file(first, second):
    """True if two paths are the same file or resolve to the same location."""
    first, second = Path(first), Path(second)
    if first.resolve() == second.resolve():
        return True
    return first.exists() and second.exists() and first.samefile(second)


def refuse_real_database(work_dir, real_database):
    """Refuse any working location that could be, or could replace, the real database."""
    work_dir, real_database = Path(work_dir), Path(real_database)
    target = work_dir / "tracking.db"
    if same_file(target, real_database) or work_dir.resolve() == real_database.parent.resolve():
        raise DrillRefused(
            "The working copy would be the real MLflow database or its folder; refusing."
        )
    if target.exists():
        raise DrillRefused(f"The working copy already exists: {target}")
    return target


def copy_database(source, target):
    """Copy a SQLite database with the backup API, opening the source read-only."""
    source = Path(source)
    if not source.is_file():
        raise DrillRefused(f"Source MLflow database not found: {source}")
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(target)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()


def call_promote(uri, *arguments):
    """Run scripts/promote_model.py main() and return (exit code, parsed JSON, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = promote.main(["--name", MODEL, "--tracking-uri", uri, *arguments])
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue().strip()


def registry_view(client):
    """Aliases and tags of the model, read back independently of promote_model."""
    model = client.get_registered_model(MODEL)
    return {
        "aliases": {key: str(value) for key, value in dict(model.aliases).items()},
        "tags": dict(model.tags),
    }


def check(description, passed):
    """One named expectation and whether it held."""
    return {"check": description, "passed": bool(passed)}


def run_step(report, name, function):
    """Run one step; an exception is a failed step, never a crash."""
    step = {"step": name, "started_utc": now()}
    try:
        step.update(function())
    except Exception as exc:  # the report must show what went wrong
        step.update(error=f"{type(exc).__name__}: {exc}", checks=[
            check("step completed without an exception", False)
        ])
    step["finished_utc"] = now()
    step["passed"] = bool(step.get("checks")) and all(c["passed"] for c in step["checks"])
    report["steps"].append(step)
    return step


def register_second_version(client, folder):
    """Register v2 from a synthetic run whose validation metrics beat v1 slightly."""
    baseline = promote.evidence(client, MODEL, 1)
    base_ap = baseline["metrics"].get(promote.COMPARISON_METRIC)
    if base_ap is None or base_ap + IMPROVEMENT > 1:
        raise ValueError("Version 1 has no usable validation_average_precision.")
    experiment = client.create_experiment(
        DRILL_EXPERIMENT, artifact_location=(Path(folder) / "artifacts").as_uri()
    )
    run = client.create_run(experiment, tags={
        "test_evaluated": "false", "synthetic": "true",
        "purpose": "rollback drill on a copy of the registry; not a trained model",
    })
    run_id = run.info.run_id
    metrics = dict(baseline["metrics"])
    metrics[promote.COMPARISON_METRIC] = base_ap + IMPROVEMENT
    for key, value in metrics.items():
        client.log_metric(run_id, key, value)
    client.set_terminated(run_id, "FINISHED")
    version = client.create_model_version(
        MODEL, source="file:///nowhere/rollback-drill/synthetic-v2", run_id=run_id,
        tags={"synthetic": "true"},
    )
    versions = sorted(int(v.version) for v in client.search_model_versions(f"name='{MODEL}'"))
    return {
        "output": {"registered_version": str(version.version), "versions": versions,
                   "synthetic_metrics": metrics, "run_id": run_id},
        "checks": [
            check("two versions exist after registering", len(versions) == 2),
            check("the new version is v2", str(version.version) == "2"),
        ],
    }


def promotion_step(client, uri, version, expected_current, expected_previous):
    """Promote a version with --apply and check the result and the read-back state."""
    code, plan, stderr = call_promote(
        uri, "--candidate-version", str(version), "--apply"
    )
    view = registry_view(client)
    return {
        "command": f"promote_model.py --candidate-version {version} --apply",
        "exit_code": code, "output": plan, "stderr": stderr, "registry_after": view,
        "checks": [
            check("exit code 0", code == 0),
            check("gate passed", bool(plan and plan["gate_passed"])),
            check("alias was changed", bool(plan and plan["changed"])),
            check(f"champion was {expected_current} before", bool(
                plan and plan["current_version"] == expected_current)),
            check(f"champion is v{version} afterwards",
                  view["aliases"].get("champion") == str(version)),
            check(f"previous_champion_version is {expected_previous}",
                  view["tags"].get(promote.previous_tag("champion")) == expected_previous),
        ],
    }


def rollback_step(client, uri):
    """Run the rollback with --apply and check that champion is v1 again."""
    code, plan, stderr = call_promote(uri, "--rollback", "--apply")
    view = registry_view(client)
    return {
        "command": "promote_model.py --rollback --apply",
        "exit_code": code, "output": plan, "stderr": stderr, "registry_after": view,
        "checks": [
            check("exit code 0", code == 0),
            check("rollback restores v1", bool(plan and plan["restore_version"] == "1")),
            check("alias was changed", bool(plan and plan["changed"])),
            check("champion is v1 again", view["aliases"].get("champion") == "1"),
            check("the replaced version v2 is now recorded as previous",
                  view["tags"].get(promote.previous_tag("champion")) == "2"),
        ],
    }


def reset_copy(client):
    """Clear champion and its previous-version tag on the copy so the drill starts clean."""
    before = registry_view(client)
    if "champion" in before["aliases"]:
        client.delete_registered_model_alias(MODEL, "champion")
    if promote.previous_tag("champion") in before["tags"]:
        client.delete_registered_model_tag(MODEL, promote.previous_tag("champion"))
    after = registry_view(client)
    return {
        "output": {"before": before, "after": after},
        "checks": [
            check("champion alias is unset on the copy", "champion" not in after["aliases"]),
            check("version 1 exists on the copy", any(
                str(v.version) == "1"
                for v in client.search_model_versions(f"name='{MODEL}'"))),
        ],
    }


def run_drill(source_database, output, *, work_dir=None, real_database=None):
    """Run the whole drill on a copy and write the create-only report; return an exit code."""
    output = Path(output)
    real_database = REAL_DATABASE if real_database is None else real_database
    if output.exists():
        raise DrillRefused(f"{output} already exists and is never overwritten.")

    report = {
        "schema_version": 1,
        "drill": "registry alias rollback on a temporary copy",
        "started_utc": now(),
        "scope": (
            "MLflow registry aliases on a copy only. Not a drill of what the API or "
            "gateway serve; the serving rollback is documented, not drilled."
        ),
        "steps": [],
    }
    with tempfile.TemporaryDirectory(prefix="rollback_drill_") as temporary:
        folder = Path(work_dir) if work_dir is not None else Path(temporary)
        target = refuse_real_database(folder, real_database)
        real_before = sha256_file(real_database) if Path(real_database).is_file() else None
        copy_database(source_database, target)
        uri = f"sqlite:///{target}"
        client = MlflowClient(tracking_uri=uri)
        report["working_copy"] = "temporary directory, removed when the drill ends"

        run_step(report, "reset champion on the copy", lambda: reset_copy(client))
        run_step(report, "a. register a second version",
                 lambda: register_second_version(client, folder))
        run_step(report, "b. promote v1 (apply)",
                 lambda: promotion_step(client, uri, 1, None, None))
        run_step(report, "c. promote v2 (apply)",
                 lambda: promotion_step(client, uri, 2, "1", "1"))
        run_step(report, "d. roll back (apply)", lambda: rollback_step(client, uri))

        real_after = sha256_file(real_database) if Path(real_database).is_file() else None
        report["real_database"] = {
            "path": str(Path(real_database).relative_to(ROOT))
            if Path(real_database).is_relative_to(ROOT) else str(real_database),
            "sha256_before": real_before,
            "sha256_after": real_after,
            "unchanged": real_before is not None and real_before == real_after,
        }

    report["passed"] = (
        all(step["passed"] for step in report["steps"])
        and report["real_database"]["unchanged"]
    )
    report["finished_utc"] = now()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return 0 if report["passed"] else 1


def main(argv=None):
    """Parse arguments and run the drill."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-database", type=Path, default=REAL_DATABASE,
                        help="database to COPY (read-only); never modified")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--work-dir", type=Path,
                        help="folder for the copy (default: a new temporary folder)")
    args = parser.parse_args(argv)
    try:
        code = run_drill(args.source_database, args.output, work_dir=args.work_dir)
    except DrillRefused as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"STOP: could not run the drill: {exc}", file=sys.stderr)
        return 2
    report = json.loads(args.output.read_text())
    for step in report["steps"]:
        print(f"{'PASS' if step['passed'] else 'FAIL'}  {step['step']}")
    print(f"real database unchanged: {report['real_database']['unchanged']}")
    print(f"{'PASS' if code == 0 else 'FAIL'}: rollback drill; report in {args.output}")
    return code


if __name__ == "__main__":
    sys.exit(main())
