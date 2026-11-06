"""Execute one queued request, preserving the champion deployment."""

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID

from backend.retraining_queue import connect
from backend.process_runner import run_process


ROOT = Path(__file__).resolve().parents[1]


def execute_request(database, request_id, *, root=ROOT, runner=None):
    UUID(request_id)
    root = Path(root).resolve()
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    runner = run_process if runner is None else runner

    with (database.parent / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "busy"}

        connection = connect(database)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown request ID.")
            if row["status"] != "queued":
                connection.commit()
                return {
                    "request_id": request_id,
                    "status": "not_queued",
                    "request_status": row["status"],
                }
            connection.execute(
                "UPDATE requests SET status = 'running' WHERE request_id = ?",
                (request_id,),
            )
            connection.commit()

            destination = root / "runtime/retraining/runs" / request_id
            outcome = {
                "request_id": request_id,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "status": "failed",
                "scope": "candidate training and validation gate",
                "registered_or_promoted": False,
            }

            try:
                destination.mkdir(parents=True, exist_ok=False)
                receipt_path = destination / "training_receipt.json"
                environment = os.environ.copy()
                environment["PYTHONPATH"] = os.pathsep.join(
                    (str(root / "src"), str(root))
                )
                train_command = [
                    sys.executable,
                    str(root / "scripts/train_xgboost_tracked.py"),
                    "--receipt", str(receipt_path),
                    "--trigger", "controlled_drift",
                    "--request-id", request_id,
                ]
                outcome["training_command"] = train_command

                with (destination / "training.log").open("x") as log:
                    trained = runner(
                        train_command, cwd=root, env=environment,
                        stdout=log, stderr=subprocess.STDOUT,
                        timeout=900, check=False,
                        pass_fds=(lock.fileno(),),
                    )
                outcome["training_exit_code"] = trained.returncode
                if trained.returncode != 0:
                    raise RuntimeError("Training command failed; inspect training.log.")

                receipt = json.loads(receipt_path.read_text())
                run_id = receipt["run_id"]
                if not isinstance(run_id, str) or not re.fullmatch(
                    r"[0-9a-f]{32}", run_id
                ):
                    raise ValueError("Invalid MLflow run ID.")

                model_dir = root / "artifacts/xgboost_tracked" / run_id
                expected_model = model_dir / "model.joblib"
                expected_report = model_dir / "validation.json"
                if (
                    receipt.get("retraining_request_id") != request_id
                    or receipt.get("trigger") != "controlled_drift"
                    or receipt.get("test_evaluated") is not False
                    or receipt.get("registered_or_promoted") is not False
                    or receipt.get("metric_readback") != "PASS"
                    or receipt.get("artifact_checksum") != "PASS"
                    or receipt.get("model_path")
                    != expected_model.relative_to(root).as_posix()
                    or receipt.get("validation_report")
                    != expected_report.relative_to(root).as_posix()
                ):
                    raise ValueError("Training receipt does not match this request.")

                # Confine the candidate bundle to the expected artifact tree.
                expected_model.resolve().relative_to(
                    (root / "artifacts/xgboost_tracked").resolve()
                )
                expected_report.resolve().relative_to(
                    (root / "artifacts/xgboost_tracked").resolve()
                )

                gate_command = [
                    sys.executable, str(root / "scripts/check_model_gate.py"),
                    "--model-dir", str(model_dir),
                ]
                outcome["gate_command"] = gate_command
                with (
                    (destination / "gate.json").open("x") as output,
                    (destination / "gate.stderr.log").open("x") as errors,
                ):
                    gated = runner(
                        gate_command, cwd=root, env=environment,
                        stdout=output, stderr=errors,
                        timeout=60, check=False,
                        pass_fds=(lock.fileno(),),
                    )

                outcome["gate_exit_code"] = gated.returncode
                gate = json.loads((destination / "gate.json").read_text())
                outcome["mlflow_run_id"] = run_id
                outcome["model_sha256"] = receipt["model_sha256"]

                if gated.returncode == 1 and gate.get("status") == "FAIL":
                    outcome["status"] = "rejected"
                    outcome["gate_failures"] = gate.get("failures", [])
                elif (
                    gated.returncode == 0
                    and gate.get("status") == "PASS"
                    and gate.get("test_set_opened") is False
                    and gate.get("identity", {}).get("model_sha256")
                    == receipt["model_sha256"]
                    and gate.get("identity", {}).get("report_sha256")
                    == hashlib.sha256(expected_report.read_bytes()).hexdigest()
                ):
                    outcome["status"] = "succeeded"
                else:
                    raise ValueError("Gate result or artifact identity is inconsistent.")

            except Exception as exc:
                outcome["status"] = "failed"
                outcome["error"] = str(exc)[:1000]

            outcome["finished_utc"] = datetime.now(timezone.utc).isoformat()
            outcome["worker_sha256"] = hashlib.sha256(
                Path(__file__).read_bytes()
            ).hexdigest()

            # Persist evidence before committing the terminal queue state.
            # A storage failure leaves 'running' for operator investigation.
            with (destination / "result.json").open("x") as handle:
                json.dump(outcome, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

            connection.execute(
                "UPDATE requests SET status = ? WHERE request_id = ?",
                (outcome["status"], request_id),
            )
            return outcome
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
