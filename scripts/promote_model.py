"""Move a model-registry alias (champion or challenger) behind a validation-only gate.

Dry run by default: nothing is changed unless --apply is given. --rollback restores
the version the alias pointed to before the last promotion. Only MLflow registry
aliases and one registered-model tag are written. The running API and gateway do not
read registry aliases, so moving an alias does not change what they serve.
"""

import argparse
import json
import sys
from pathlib import Path

from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient


ROOT = Path(__file__).resolve().parents[1]
TRACKING_DATABASE = ROOT / "runtime/mlflow/tracking.db"
DEFAULT_MODEL = "FraudShield-XGBoost"
ALIASES = ("champion", "challenger")
COMPARISON_METRIC = "validation_average_precision"
# Same floors as scripts/check_model_gate.py: AP 0.85, precision 0.70, recall 0.90.
FLOORS = (
    ("validation_average_precision", 0.85),
    ("validation_precision", 0.70),
    ("validation_recall", 0.90),
)
MAX_RUN_CHAIN = 3


class RegistryError(ValueError):
    """A lookup or usage problem that prevents any decision (exit code 2)."""


def previous_tag(alias):
    """Name of the registered-model tag that remembers the previous version."""
    return f"previous_{alias}_version"


def registry_state(client, name):
    """Return the alias-to-version map and tags of a registered model."""
    try:
        model = client.get_registered_model(name)
    except MlflowException as exc:
        raise RegistryError(f"Registered model not found: {name}") from exc
    return (
        {alias: str(version) for alias, version in dict(model.aliases).items()},
        dict(model.tags),
    )


def load_version(client, name, version):
    """Return a model version or raise RegistryError if it does not exist."""
    try:
        return client.get_model_version(name, str(version))
    except MlflowException as exc:
        raise RegistryError(f"Model version not found: {name} v{version}") from exc


def evidence(client, name, version):
    """Collect validation metrics and test-set status for one model version.

    The version's own run is read first, then runs named by a source_run_id tag
    (packaging runs point at the run that holds the metrics).
    """
    model_version = load_version(client, name, version)
    queue = [model_version.run_id] if model_version.run_id else []
    seen, runs = set(), []
    while queue and len(runs) < MAX_RUN_CHAIN:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        run = client.get_run(run_id)
        runs.append(run)
        source = run.data.tags.get("source_run_id")
        if source:
            queue.append(source)

    metrics = {}
    for run in runs:
        for key, value in run.data.metrics.items():
            metrics.setdefault(key, value)
    flags = {run.data.tags.get("test_evaluated") for run in runs} - {None}
    if "true" in flags:
        status = "true"
    elif "false" in flags:
        status = "false"
    else:
        status = "unknown"
    wanted = {key for key, _ in FLOORS} | {COMPARISON_METRIC}
    return {
        "version": str(version),
        "run_ids": [run.info.run_id for run in runs],
        "metrics": {key: metrics[key] for key in sorted(wanted) if key in metrics},
        "test_evaluated": status,
    }


def gate(candidate, current, alias, min_improvement):
    """Return (passed, reasons) from validation-only evidence."""
    reasons = []
    if candidate["test_evaluated"] == "true":
        reasons.append("The test split was opened for this version.")
    elif candidate["test_evaluated"] == "unknown":
        reasons.append("No run records test_evaluated=false (validation-only evidence).")
    for key, floor in FLOORS:
        value = candidate["metrics"].get(key)
        if value is None:
            reasons.append(f"Missing metric {key}.")
        elif value < floor:
            reasons.append(f"{key} {value:.6f} is below the floor {floor}.")
    if alias == "champion" and current is not None:
        new = candidate["metrics"].get(COMPARISON_METRIC)
        old = current["metrics"].get(COMPARISON_METRIC)
        if old is None:
            reasons.append(
                "The current champion has no validation_average_precision to compare."
            )
        elif new is not None and not new > old + min_improvement:
            reasons.append(
                f"{COMPARISON_METRIC} {new:.6f} is not above the current champion "
                f"{old:.6f} plus the required improvement {min_improvement}."
            )
    return not reasons, reasons


def plan_promotion(client, name, alias, candidate_version, min_improvement):
    """Evaluate a promotion without changing anything."""
    aliases, _ = registry_state(client, name)
    current_version = aliases.get(alias)
    candidate = evidence(client, name, candidate_version)
    result = {
        "action": "promote", "model": name, "alias": alias,
        "candidate_version": str(candidate_version),
        "current_version": current_version,
        "candidate_evidence": candidate,
    }
    if current_version == str(candidate_version):
        result.update(gate_passed=False, reasons=[f"{alias} already points to this version."])
        return result
    current = evidence(client, name, current_version) if (
        alias == "champion" and current_version is not None
    ) else None
    result["current_evidence"] = current
    passed, reasons = gate(candidate, current, alias, min_improvement)
    result.update(gate_passed=passed, reasons=reasons)
    return result


def plan_rollback(client, name, alias):
    """Work out which version a rollback would restore."""
    aliases, tags = registry_state(client, name)
    previous = tags.get(previous_tag(alias))
    if not previous:
        raise RegistryError(f"No previous {alias} version is recorded for {name}.")
    load_version(client, name, previous)
    current_version = aliases.get(alias)
    result = {
        "action": "rollback", "model": name, "alias": alias,
        "current_version": current_version, "restore_version": previous,
    }
    if previous == current_version:
        result.update(gate_passed=False, reasons=[f"{alias} already points to v{previous}."])
    else:
        result.update(gate_passed=True, reasons=[])
    return result


def remember(client, name, alias, version):
    """Record the version being replaced, or clear the record if there is none."""
    key = previous_tag(alias)
    if version is not None:
        client.set_registered_model_tag(name, key, str(version))
    elif key in registry_state(client, name)[1]:
        client.delete_registered_model_tag(name, key)


def apply_plan(client, plan):
    """Write the alias move described by a passing plan."""
    name, alias = plan["model"], plan["alias"]
    target = (
        plan["candidate_version"] if plan["action"] == "promote"
        else plan["restore_version"]
    )
    remember(client, name, alias, plan["current_version"])
    client.set_registered_model_alias(name, alias, str(target))


def run(client, args):
    """Plan, optionally apply, and return (result, exit code)."""
    if args.rollback:
        plan = plan_rollback(client, args.name, args.alias)
    else:
        plan = plan_promotion(
            client, args.name, args.alias, args.candidate_version, args.min_improvement
        )
    plan["dry_run"] = not args.apply
    plan["changed"] = False
    if plan["gate_passed"] and args.apply:
        apply_plan(client, plan)
        plan["changed"] = True
    return plan, (0 if plan["gate_passed"] else 1)


def main(argv=None):
    """Parse arguments, run the plan and print it as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_MODEL)
    parser.add_argument("--alias", choices=ALIASES, default="champion")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--candidate-version", type=int, metavar="N")
    action.add_argument("--rollback", action="store_true")
    parser.add_argument("--apply", action="store_true",
                        help="write the change (default is a dry run)")
    parser.add_argument("--min-improvement", type=float, default=0.0,
                        help="champion only: required gain in validation average precision")
    parser.add_argument("--tracking-uri", default=f"sqlite:///{TRACKING_DATABASE}")
    args = parser.parse_args(argv)
    if args.min_improvement < 0:
        parser.error("--min-improvement must not be negative.")
    if (args.tracking_uri == f"sqlite:///{TRACKING_DATABASE}"
            and not TRACKING_DATABASE.is_file()):
        print("STOP: expected MLflow database is missing.", file=sys.stderr)
        return 2
    try:
        result, code = run(MlflowClient(tracking_uri=args.tracking_uri), args)
    except RegistryError as exc:
        print(f"STOP: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    if result["dry_run"]:
        print("DRY RUN: nothing was changed. Add --apply to write the change.",
              file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
