"""Reconcile a retraining request left 'running' after a result was published.

Scope: the worker published a complete runs/<request_id>/result.json but never
committed the terminal SQLite update (crash, SIGKILL, database error). This
module can inspect that situation read-only, and can explicitly apply the
terminal status the published evidence supports. It never runs training or the
gate, never requeues, registers, promotes or routes a model, and never writes
or replaces result.json. A killed worker with NO published result.json is a
different case and is always refused here.
"""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID


ROOT = Path(__file__).resolve().parents[1]
RUNS = Path("runtime/retraining/runs")
TREE = Path("artifacts/xgboost_tracked")
SCOPE = "candidate training and validation gate"
TERMINAL = ("succeeded", "rejected", "failed")
SHA256 = re.compile(r"[0-9a-f]{64}")
RUN_ID = re.compile(r"[0-9a-f]{32}")
OK_DECISIONS = {"recoverable", "recovered", "already_recovered", "already_terminal"}

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS recovery_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    previous_status TEXT NOT NULL CHECK (previous_status = 'running'),
    new_status TEXT NOT NULL CHECK (new_status IN ('succeeded', 'rejected', 'failed')),
    result_path TEXT NOT NULL,
    result_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    recovered_utc TEXT NOT NULL,
    tool_sha256 TEXT NOT NULL
)
"""


def sha256_file(path):
    """Return the SHA-256 of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def exit_code(result):
    """Map a reconciliation decision to the command exit code."""
    if result["decision"] in OK_DECISIONS:
        return 0
    return 2 if result["decision"] == "busy" else 1


def open_database(path, *, writable):
    """Open an existing queue database; never creates a file or a table."""
    mode = "rw" if writable else "ro"
    connection = sqlite3.connect(
        f"{Path(path).resolve().as_uri()}?mode={mode}",
        uri=True, timeout=10, isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    return connection


# --- evidence ---------------------------------------------------------------

def _regular(path):
    """Return True if the path is a regular file and not a symlink."""
    try:
        return stat.S_ISREG(Path(path).lstat().st_mode)
    except OSError:
        return False


def _directory(path):
    """Return True if the path is a real directory and not a symlink."""
    try:
        return stat.S_ISDIR(Path(path).lstat().st_mode)
    except OSError:
        return False


def _strict_json(path):
    """Read a JSON file, rejecting non-finite numbers and duplicate keys."""
    def constant(name):
        """Reject NaN and Infinity in JSON input."""
        raise ValueError(f"non-finite JSON number {name}")

    def pairs(items):
        """Build an object, rejecting duplicate keys."""
        keys = [key for key, _ in items]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate JSON key")
        return dict(items)

    raw = Path(path).read_bytes()
    return raw, json.loads(
        raw.decode("utf-8"), parse_constant=constant, object_pairs_hook=pairs
    )


def _timestamp(value):
    """Parse an ISO timestamp; return None unless it is timezone-aware."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_int(value):
    """Return True for integers, excluding booleans."""
    return isinstance(value, int) and not isinstance(value, bool)


def _load(path, name, problems, facts):
    """Load one required evidence file; record its hash; None on any problem."""
    if not _regular(path):
        problems.append(f"{name} is missing or not a regular file.")
        return None
    try:
        raw, data = _strict_json(path)
    except (OSError, ValueError) as exc:
        problems.append(f"{name} is unreadable or malformed: {exc}")
        return None
    if not isinstance(data, dict):
        problems.append(f"{name} must contain a JSON object.")
        return None
    facts[f"{name}_sha256"] = hashlib.sha256(raw).hexdigest()
    return data


def _check_result(result, request_id, problems):
    """Check result.json fields against the request and return its status."""
    status = result.get("status")
    if result.get("request_id") != request_id:
        problems.append("result.json belongs to a different request ID.")
    if status not in TERMINAL:
        problems.append(f"result.json status {status!r} is not a terminal outcome.")
    if result.get("scope") != SCOPE:
        problems.append("result.json scope is not the candidate-only scope.")
    if result.get("registered_or_promoted") is not False:
        problems.append("result.json does not record registered_or_promoted=false.")
    started = _timestamp(result.get("started_utc"))
    finished = _timestamp(result.get("finished_utc"))
    if started is None or finished is None or finished < started:
        problems.append("result.json timestamps are missing, naive or out of order.")
    worker_hash = result.get("worker_sha256")
    if not (isinstance(worker_hash, str) and SHA256.fullmatch(worker_hash)):
        problems.append("result.json lacks a valid worker_sha256.")
    command = result.get("training_command")
    if command is not None and not (
        isinstance(command, list) and all(isinstance(item, str) for item in command)
    ):
        problems.append("training_command is malformed.")
    return status


def _check_failed(result, problems):
    """Require a non-empty error message in a failed result."""
    error = result.get("error")
    if not (isinstance(error, str) and error.strip()):
        problems.append("A failed result must carry a non-empty error message.")


def _check_gated(result, status, problems):
    """Fields shared by succeeded and rejected results; returns (run_id, sha)."""
    if "error" in result:
        problems.append(f"A {status} result must not carry an error field.")
    if not (_is_int(result.get("training_exit_code")) and result["training_exit_code"] == 0):
        problems.append("Training exit code must be 0 for a gated result.")
    expected_gate = 0 if status == "succeeded" else 1
    if not (_is_int(result.get("gate_exit_code")) and result["gate_exit_code"] == expected_gate):
        problems.append(f"Gate exit code must be {expected_gate} for {status}.")
    if not isinstance(result.get("gate_command"), list):
        problems.append("gate_command is missing.")
    failures = result.get("gate_failures")
    if status == "rejected":
        if not (
            isinstance(failures, list) and failures
            and all(isinstance(item, str) for item in failures)
        ):
            problems.append("A rejected result needs a non-empty gate_failures list.")
    elif failures:
        problems.append("A succeeded result must not list gate failures.")
    run_id, model_sha = result.get("mlflow_run_id"), result.get("model_sha256")
    if not (isinstance(run_id, str) and RUN_ID.fullmatch(run_id)):
        problems.append("mlflow_run_id is missing or invalid.")
        run_id = None
    if not (isinstance(model_sha, str) and SHA256.fullmatch(model_sha)):
        problems.append("model_sha256 is missing or invalid.")
        model_sha = None
    return run_id, model_sha


def _check_artifacts(root, directory, request_id, result, status, run_id, model_sha,
                     problems, facts):
    """Cross-check the receipt, model, gate and validation files of a finished run."""
    receipt = _load(directory / "training_receipt.json", "training_receipt.json",
                    problems, facts)
    model_dir = root / TREE / run_id
    tree = (root / TREE).resolve()
    if not (_directory(model_dir) and model_dir.resolve() == tree / run_id):
        problems.append("Candidate artifact directory is missing or outside the artifact tree.")
        return
    model_path, report_path = model_dir / "model.joblib", model_dir / "validation.json"
    for path in (model_path, report_path):
        if not _regular(path):
            problems.append(f"{path.name} is missing or not a regular file.")
            return
    actual_model, actual_report = sha256_file(model_path), sha256_file(report_path)
    facts["model_sha256"], facts["validation_report_sha256"] = actual_model, actual_report
    if actual_model != model_sha:
        problems.append("model.joblib hash differs from the hash recorded in result.json.")

    if receipt is not None:
        expected = {
            "run_id": run_id,
            "retraining_request_id": request_id,
            "trigger": "controlled_drift",
            "test_evaluated": False,
            "registered_or_promoted": False,
            "metric_readback": "PASS",
            "artifact_checksum": "PASS",
            "model_path": (TREE / run_id / "model.joblib").as_posix(),
            "validation_report": (TREE / run_id / "validation.json").as_posix(),
            "model_sha256": model_sha,
        }
        for key, value in expected.items():
            if receipt.get(key) != value or type(receipt.get(key)) is not type(value):
                problems.append(f"Training receipt field {key!r} does not match this request.")

    gate = _load(directory / "gate.json", "gate.json", problems, facts)
    if gate is None:
        return
    if gate.get("test_set_opened") is not False:
        problems.append("gate.json does not declare test_set_opened=false.")
    identity = gate.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    if status == "succeeded":
        if gate.get("status") != "PASS" or gate.get("failures"):
            problems.append("gate.json is not a clean PASS.")
        if identity.get("model_sha256") != actual_model:
            problems.append("Gate model identity differs from the artifact on disk.")
        if identity.get("report_sha256") != actual_report:
            problems.append("Gate report identity differs from validation.json on disk.")
        try:
            _, report = _strict_json(report_path)
        except (OSError, ValueError) as exc:
            problems.append(f"validation.json is malformed: {exc}")
            return
        if not (
            isinstance(report, dict)
            and report.get("evaluation_split") == "validation"
            and report.get("test_evaluated") is False
            and report.get("model_sha256") == actual_model
        ):
            problems.append("validation.json is not a validation-only report for this model.")
    else:
        if gate.get("status") != "FAIL":
            problems.append("gate.json is not a FAIL for a rejected result.")
        if gate.get("failures") != result.get("gate_failures"):
            problems.append("gate.json failures differ from result.json gate_failures.")
        for key, actual in (("model_sha256", actual_model), ("report_sha256", actual_report)):
            if key in identity and identity[key] != actual:
                problems.append(f"Gate {key} differs from the artifact on disk.")


def check_evidence(root, request_id):
    """Validate published evidence for one request; read-only."""
    root = Path(root).resolve()
    directory = root / RUNS / request_id
    problems, facts = [], {}
    report = {
        "problems": problems, "facts": facts, "proposed_status": None,
        "result_status": None, "result_sha256": None, "temporary_files_ignored": 0,
    }
    if not _directory(directory):
        problems.append("The request's run directory does not exist.")
        return report
    report["temporary_files_ignored"] = len(list(directory.glob(".result-*.tmp")))
    result = _load(directory / "result.json", "result.json", problems, facts)
    if result is None:
        problems.append("Temporary .result-*.tmp files are never evidence.")
        return report
    report["result_sha256"] = facts.pop("result.json_sha256")
    report["result_status"] = result.get("status")
    try:
        status = _check_result(result, request_id, problems)
        if status == "failed":
            _check_failed(result, problems)
        elif status in ("succeeded", "rejected"):
            run_id, model_sha = _check_gated(result, status, problems)
            if run_id and model_sha:
                _check_artifacts(root, directory, request_id, result, status,
                                 run_id, model_sha, problems, facts)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        problems.append(f"Unexpected evidence structure: {exc}")
    if not problems:
        report["proposed_status"] = status
    return report


# --- reconciliation ---------------------------------------------------------

def _audit_exists(connection, request_id):
    """Return True if a recovery audit row already exists for the request."""
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'recovery_audit'"
    ).fetchone():
        return False
    return connection.execute(
        "SELECT 1 FROM recovery_audit WHERE request_id = ?", (request_id,)
    ).fetchone() is not None


def _refuse(out, code, message, **extra):
    """Build a refusal result with a reason code and message."""
    return {**out, "decision": "refused", "reason_code": code, "message": message, **extra}


def _apply(connection, out, root, request_id, evidence, reason):
    """Update the request row and write the audit row in one transaction."""
    status = evidence["proposed_status"]
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(AUDIT_DDL)
        row = connection.execute(
            "SELECT status FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None or row["status"] != "running":
            connection.rollback()
            return _refuse(out, "conflict", "The row changed before the update; nothing written.")
        # The worker cannot run (we hold its lock), but confirm the bytes once
        # more so the audited hash is exactly what was committed against.
        if sha256_file(root / RUNS / request_id / "result.json") != evidence["result_sha256"]:
            connection.rollback()
            return _refuse(out, "evidence_changed", "result.json changed during recovery.")
        cursor = connection.execute(
            "UPDATE requests SET status = ? WHERE request_id = ? AND status = 'running'",
            (status, request_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return _refuse(out, "conflict", "Conditional update matched no running row.")
        connection.execute(
            "INSERT INTO recovery_audit (request_id, previous_status, new_status, "
            "result_path, result_sha256, evidence_json, reason, recovered_utc, tool_sha256) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id, status, (RUNS / request_id / "result.json").as_posix(),
                evidence["result_sha256"],
                json.dumps(evidence["facts"], sort_keys=True, allow_nan=False),
                reason.strip(), datetime.now(timezone.utc).isoformat(),
                sha256_file(__file__),
            ),
        )
        connection.commit()
    except sqlite3.Error as exc:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        return {**out, "decision": "database_error", "error": str(exc)[:500],
                "message": ("Transaction rolled back. Inspect again to confirm "
                            "the row is still 'running'.")}
    except Exception:
        connection.rollback()
        raise
    readback = connection.execute(
        "SELECT status FROM requests WHERE request_id = ?", (request_id,)
    ).fetchone()["status"]
    return {**out, "decision": "recovered", "previous_status": "running",
            "new_status": status, "row_status": readback}


def _reconcile_locked(database, request_id, root, apply, confirm, reason, base):
    """Inspect or apply recovery for one request while the worker lock is held."""
    try:
        connection = open_database(database, writable=apply)
    except sqlite3.Error as exc:
        return {**base, "decision": "database_error", "error": str(exc)[:500]}
    try:
        try:
            row = connection.execute(
                "SELECT status FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                return _refuse(base, "unknown_request", "No such request in this queue.")
            audited = _audit_exists(connection, request_id)
        except sqlite3.Error as exc:
            return {**base, "decision": "database_error", "error": str(exc)[:500]}

        status = row["status"]
        evidence = check_evidence(root, request_id)
        out = {**base, "row_status": status, "result_sha256": evidence["result_sha256"],
               "temporary_files_ignored": evidence["temporary_files_ignored"],
               "facts": evidence["facts"]}

        if status == "queued":
            return _refuse(out, "not_running",
                           "The row is queued; recovery only reconciles 'running' rows.")
        if status in TERMINAL:
            seen = evidence["result_status"]
            if seen is not None and seen != status:
                return _refuse(out, "conflict",
                               f"Row is {status!r} but result.json says {seen!r}; left unchanged.")
            return {**out, "decision": "already_recovered" if audited else "already_terminal",
                    "message": "Nothing to do; the row is already terminal."}

        if evidence["problems"]:
            return _refuse(out, "insufficient_evidence",
                           "Published evidence does not justify a transition; row left unchanged.",
                           problems=evidence["problems"])
        out["proposed_status"] = evidence["proposed_status"]
        if not apply:
            return {**out, "decision": "recoverable",
                    "apply_requires": {"confirm_result_sha256": evidence["result_sha256"]}}
        if confirm != evidence["result_sha256"]:
            return _refuse(out, "confirmation_mismatch",
                           "Confirmation hash does not match result.json; inspect again.")
        return _apply(connection, out, root, request_id, evidence, reason)
    finally:
        connection.close()


def reconcile(database, request_id, *, root=ROOT, apply=False,
              confirm_result_sha256=None, reason=None):
    """Inspect (default) or apply recovery for one request; see module docstring."""
    UUID(request_id)
    if apply:
        if not (isinstance(reason, str) and reason.strip() and len(reason) <= 500):
            raise ValueError("A reason of 1-500 characters is required.")
        if not (isinstance(confirm_result_sha256, str)
                and SHA256.fullmatch(confirm_result_sha256)):
            raise ValueError("Confirm with the result.json SHA-256 shown by inspection.")
    root, database = Path(root).resolve(), Path(database)
    base = {"request_id": request_id, "mode": "apply" if apply else "inspect"}
    if not database.is_file():
        return _refuse(base, "database_missing", "Queue database does not exist; nothing created.")

    # The same lock file the worker uses; a live worker or an inherited-lock
    # child therefore blocks both inspection and recovery.
    with (database.parent / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**base, "decision": "busy",
                    "message": "The worker lock is held; nothing was read or changed."}
        return _reconcile_locked(
            database, request_id, root, apply, confirm_result_sha256, reason, base
        )
