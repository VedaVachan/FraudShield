"""Write a reproducibility manifest: which commit and which run produced the deployed model.

Everything is read-only. The model file is hashed, the MLflow database is opened
through SQLite in read-only mode (so no MLflow migration or write can happen), and
git is only queried. The one thing written is the manifest, created exclusively:
an existing file is never overwritten. Anything that cannot be found is recorded
as "missing"; no value is inferred.
"""

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DEPLOYED_MODEL = ROOT / "artifacts/xgboost_v1/model.joblib"
REGISTRY_RECEIPT = ROOT / "reports/mlflow_registry.json"
TRACKING_DATABASE = ROOT / "runtime/mlflow/tracking.db"
OUTPUT = ROOT / "reports/reproducibility_manifest.json"
DATA_POINTER = ROOT / "paysim.dvc"
DVC_LOCK = ROOT / "dvc.lock"
REPORT_FILES = (
    "reports/xgboost_validation_gate.json",
    "reports/mlflow_registry.json",
    "reports/drift_reference.json",
    "reports/xgboost_validation.json",
    "reports/xgboost_model_selection.json",
)
LOCK_FILES = ("requirements-ml.lock.txt", "requirements-serving.lock.txt")
PACKAGES = (
    "mlflow", "scikit-learn", "xgboost", "pandas", "numpy", "scipy", "joblib",
    "shap", "fastapi", "pydantic", "prometheus-client", "pyspark", "dvc",
)
MISSING = "missing"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
# Tags that can name a commit, kept verbatim so their different meanings stay visible.
COMMIT_TAGS = (
    "git_commit", "mlflow.source.git.commit", "training_code_commit",
    "import_code_commit",
)
# Order in which a tag is accepted as "the commit recorded on this run".
RECORDED_COMMIT_TAGS = ("git_commit", "mlflow.source.git.commit")
MAX_RUN_CHAIN = 5


def sha256_file(path):
    """Return the SHA-256 of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(root, *args):
    """Run a read-only git command and return its output, or None on failure."""
    try:
        return subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def commit_exists(root, commit):
    """True or False if git knows the commit; None when git cannot answer."""
    if git(root, "rev-parse", "--git-dir") is None:
        return None
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=root,
            capture_output=True, check=False,
        )
    except OSError:
        return None
    return result.returncode == 0


def open_read_only(path):
    """Open a SQLite file read-only; raises if it does not exist."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"MLflow database not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def table_columns(connection, table):
    """Column names of a table, empty if the table does not exist."""
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def describe_run(connection, root, run_id, role):
    """Return identity and commit tags of one run, or a 'missing' record."""
    row = connection.execute(
        "SELECT name, status, lifecycle_stage, experiment_id FROM runs "
        "WHERE run_uuid = ?", (run_id,),
    ).fetchone()
    if row is None:
        return {"run_id": run_id, "role": role, "found": False}
    tags = dict(connection.execute(
        "SELECT key, value FROM tags WHERE run_uuid = ?", (run_id,)
    ))
    recorded = MISSING
    for key in RECORDED_COMMIT_TAGS:
        if COMMIT_PATTERN.fullmatch(tags.get(key, "")):
            recorded = tags[key]
            break
    return {
        "run_id": run_id,
        "role": role,
        "found": True,
        "run_name": row[0],
        "status": row[1],
        "lifecycle_stage": row[2],
        "experiment_id": row[3],
        "git_commit": recorded,
        "git_commit_exists_in_repository": (
            commit_exists(root, recorded) if recorded != MISSING else None
        ),
        "commit_tags": {key: tags[key] for key in COMMIT_TAGS if key in tags},
        "training_code_commit": tags.get("training_code_commit", MISSING),
        "git_dirty": tags.get("git_dirty", MISSING),
        "model_sha256_tag": tags.get("model_sha256", MISSING),
        "source_run_id": tags.get("source_run_id", MISSING),
    }


def run_chain(connection, root, first_run_id):
    """Follow source_run_id tags from a version's run; return the runs in order."""
    runs, seen, queue = [], set(), [first_run_id]
    while queue and len(runs) < MAX_RUN_CHAIN:
        run_id = queue.pop(0)
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        role = "registered_run" if not runs else "linked_by_source_run_id"
        record = describe_run(connection, root, run_id, role)
        runs.append(record)
        source = record.get("source_run_id", MISSING)
        if record["found"] and source != MISSING:
            queue.append(source)
    return runs


def registry_versions(connection, root, model_sha256):
    """List every registered model version with the runs that link to it."""
    columns = table_columns(connection, "model_versions")
    if not {"name", "version", "run_id"} <= columns:
        return []
    aliases = {}
    if table_columns(connection, "registered_model_aliases") >= {"name", "alias", "version"}:
        for name, alias, version in connection.execute(
            "SELECT name, alias, version FROM registered_model_aliases"
        ):
            aliases.setdefault((name, str(version)), []).append(alias)
    versions = []
    query = "SELECT name, version, run_id, status FROM model_versions ORDER BY name, version"
    for name, version, run_id, status in connection.execute(query):
        runs = run_chain(connection, root, run_id) if run_id else []
        tagged = [run["model_sha256_tag"] for run in runs if run["found"]]
        versions.append({
            "registry_name": name,
            "registry_version": str(version),
            "status": status,
            "aliases": sorted(aliases.get((name, str(version)), [])),
            "runs": runs,
            "model_sha256_tags_on_runs": sorted({t for t in tagged if t != MISSING}),
            "is_deployed_model": model_sha256 in tagged,
        })
    return versions


def runs_tagged_with_model(connection, root, model_sha256, already_listed):
    """Runs (for example analysis runs) that carry the deployed model's hash in a tag."""
    found = []
    query = (
        "SELECT run_uuid FROM tags WHERE key = 'model_sha256' AND value = ? "
        "ORDER BY run_uuid"
    )
    for (run_id,) in connection.execute(query, (model_sha256,)):
        if run_id not in already_listed:
            found.append(describe_run(connection, root, run_id, "tagged_with_model_sha256"))
    return found


def data_record(pointer_path, lock_path):
    """Read the DVC data pointer and dvc.lock hashes without running DVC."""
    record = {"pointer_file": Path(pointer_path).name, "lock_file": Path(lock_path).name}
    try:
        pointer = yaml.safe_load(Path(pointer_path).read_text())
        out = pointer["outs"][0]
        record["raw_data"] = {
            "path": out["path"], "md5": out["md5"], "size": out["size"],
        }
    except (OSError, KeyError, IndexError, TypeError, yaml.YAMLError):
        record["raw_data"] = MISSING
    stages = {}
    try:
        lock = yaml.safe_load(Path(lock_path).read_text())
        for name, stage in lock["stages"].items():
            stages[name] = {
                "dependencies": {
                    item["path"]: item.get("md5", MISSING)
                    for item in stage.get("deps", [])
                    if str(item["path"]).startswith(("data/", "src/", "scripts/"))
                },
                "outputs": {
                    item["path"]: item.get("md5", MISSING)
                    for item in stage.get("outs", [])
                },
            }
    except (OSError, KeyError, TypeError, AttributeError, yaml.YAMLError):
        stages = MISSING
    record["dvc_lock_stages"] = stages
    raw = record["raw_data"]
    lock_hash = MISSING
    if isinstance(stages, dict):
        for stage in stages.values():
            lock_hash = stage["dependencies"].get(
                raw["path"] if raw != MISSING else "", lock_hash
            )
    record["raw_data_md5_in_dvc_lock"] = lock_hash
    record["pointer_and_lock_agree"] = (
        raw != MISSING and lock_hash != MISSING and raw["md5"] == lock_hash
    )
    return record


def file_record(root, relative):
    """Hash a report file and name the last commit that changed it."""
    path = Path(root) / relative
    if not path.is_file():
        return {"present": False, "last_commit": MISSING}
    last = git(root, "log", "-1", "--format=%H", "--", relative)
    status = git(root, "status", "--porcelain", "--", relative)
    return {
        "present": True,
        "sha256": sha256_file(path),
        "last_commit": last or MISSING,
        "uncommitted_changes": None if status is None else bool(status),
    }


def gate_identity(root, model_sha256):
    """Say which model the validation gate file describes (it may not be the deployed one)."""
    path = Path(root) / "reports/xgboost_validation_gate.json"
    try:
        gate = json.loads(path.read_text())
        gated = gate["gate"]["identity"]["model_sha256"]
        recorded_commit = gate.get("git_commit", MISSING)
    except (OSError, ValueError, KeyError, TypeError):
        return {"gate_file_model_sha256": MISSING, "describes_deployed_model": None}
    return {
        "gate_file_model_sha256": gated,
        "gate_file_recorded_git_commit": recorded_commit,
        "describes_deployed_model": gated == model_sha256,
    }


def package_versions():
    """Installed versions in the environment that runs this script."""
    versions = {}
    for name in PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "not installed in this environment"
    return versions


def build_manifest(root, model_path, receipt_path, database_path):
    """Collect the manifest as a dict; reads files, the database and git only."""
    root = Path(root)
    model_sha256 = sha256_file(model_path)
    try:
        receipt = json.loads(Path(receipt_path).read_text())["models"]["xgboost"]
    except (OSError, ValueError, KeyError):
        receipt = None

    with open_read_only(database_path) as connection:
        versions = registry_versions(connection, root, model_sha256)
        listed = {run["run_id"] for v in versions for run in v["runs"]}
        extra = runs_tagged_with_model(connection, root, model_sha256, listed)

    head = git(root, "rev-parse", "HEAD")
    dirty = git(root, "status", "--porcelain")
    lock_files = {
        name: sha256_file(root / name) if (root / name).is_file() else MISSING
        for name in LOCK_FILES
    }
    return {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "generator": "scripts/build_reproducibility_manifest.py",
        "repository": {
            "head_commit": head or MISSING,
            "uncommitted_changes": None if dirty is None else bool(dirty),
        },
        "deployed_model": {
            "path": Path(model_path).relative_to(root).as_posix()
            if Path(model_path).is_relative_to(root) else str(model_path),
            "sha256": model_sha256,
            "size_bytes": Path(model_path).stat().st_size,
            "registry_receipt": (
                {
                    "file": Path(receipt_path).name,
                    "name": receipt.get("name"),
                    "version": receipt.get("version"),
                    "model_sha256": receipt.get("model_sha256"),
                    "matches_deployed_file": receipt.get("model_sha256") == model_sha256,
                } if receipt else MISSING
            ),
        },
        "registry": {
            "tracking_database": Path(database_path).name,
            "opened_read_only": True,
            "versions": versions,
            "other_runs_tagged_with_model_sha256": extra,
        },
        "data": data_record(root / "paysim.dvc", root / "dvc.lock"),
        "validation_gate": gate_identity(root, model_sha256),
        "report_files": {name: file_record(root, name) for name in REPORT_FILES},
        "dependency_lock_files": lock_files,
        "environment": {
            "python": platform.python_version(),
            "packages": package_versions(),
            "note": (
                "Versions are those of the environment that ran this script; they "
                "are not proof of the versions used when the model was trained."
            ),
        },
        "limitations": [
            "Values come from files, git and the MLflow database; none were inferred.",
            "'missing' means the source holds no value for that field.",
            "A commit tag records the code state at logging time, not necessarily "
            "the code that trained the model; see training_code_commit.",
        ],
    }


def write_manifest(manifest, output):
    """Create the manifest file; raise FileExistsError instead of overwriting."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, allow_nan=False)
        handle.write("\n")
    return output


def main(argv=None):
    """Parse arguments, build the manifest and write it once."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEPLOYED_MODEL)
    parser.add_argument("--receipt", type=Path, default=REGISTRY_RECEIPT)
    parser.add_argument("--database", type=Path, default=TRACKING_DATABASE)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    if args.output.exists():
        print(f"STOP: {args.output} already exists and is never overwritten.",
              file=sys.stderr)
        return 2
    try:
        manifest = build_manifest(args.root, args.model, args.receipt, args.database)
        write_manifest(manifest, args.output)
    except FileExistsError:
        print(f"STOP: {args.output} already exists and is never overwritten.",
              file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(f"STOP: could not read an input: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
