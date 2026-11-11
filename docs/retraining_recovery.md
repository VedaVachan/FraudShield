# Recovering a retraining request left `running`

## Which parts are automatic

| Step | Automatic? |
|---|---|
| Drift detection and queueing a request | Automatic |
| Executing a queued request | Manual (`--request-id`), or automatic only with the opt-in `--watch` |
| Registering, promoting or deploying a model | Never automatic; promotion is always a manual, gated step |

`scripts/run_retraining_worker.py --watch [--interval SECONDS]` is off by default. It polls
the queue and runs each queued request through the same function as `--request-id`
(`execute_request`): the same worker lock, the same committed-source check (before every
request) and the same candidate-only training and validation gate. It never registers,
promotes or deploys. The interval must be positive (default 30 s).

How it behaves, relevant to recovery:

- Each decision is logged (started, request run, final status, lock busy, stop).
- A request is never retried. Terminal states are final; a held worker lock only means
  the same request is tried again on the next poll.
- SIGINT or SIGTERM while waiting stops the watch with exit code 0. During a running
  request the worker's existing handling stops the training and its descendants and records
  the request `failed` with the interruption as the error; the watch then ends.
- If the worker sources are not committed and unchanged, the watch exits 1 without running
  anything. If a request aborts before a terminal state is recorded, the watch exits 1 and
  the row is left `running`: use the inspection and recovery commands below.
- Tested with fake training commands only; not run against real training.

## When this applies

The worker publishes `runtime/retraining/runs/<request_id>/result.json`
atomically and only then commits the terminal SQLite status. If the process
dies, or the database update fails, between those two steps the row stays
`running` while a complete result exists. The worker will not replay such a
row (`not_queued`), by design.

This is **not** the case where the worker was killed and no `result.json`
exists. A temporary `.result-*.tmp` file, elapsed time, or a missing process is
never proof of either outcome; recovery refuses those rows and leaves them for
operator investigation.

## Commands

Both commands take the same worker lock as `execute_request`. If a worker, or a
child that inherited the lock after the worker was killed, still holds it, they
return `busy` (exit 2) without reading or changing anything.

```bash
# 1. Read-only. Opens SQLite read-only, writes nothing.
python scripts/run_retraining_worker.py --inspect-recovery <request_id>

# 2. Explicit apply. Bound to the exact result.json you reviewed.
python scripts/run_retraining_worker.py --recover <request_id> \
    --confirm-result-sha256 <hash printed by step 1> \
    --reason "worker killed after result publication; evidence reviewed"
```

`--recover` additionally requires the worker sources (now including
`backend/retraining_recovery.py`) to be committed and unchanged, like
execution does. `--inspect-recovery` does not.

Exit codes: 0 ok (recoverable / recovered / already terminal), 1 refused or
database error, 2 busy.

## Evidence required, per outcome

All outcomes: `result.json` is a regular file (not a symlink), strict JSON (no
NaN, no duplicate keys), `request_id` equals the row, `scope` is the candidate
scope, `registered_or_promoted` is `false`, tz-aware ordered timestamps, a
valid `worker_sha256`.

| Outcome | Additionally required |
|---|---|
| `failed` | non-empty `error`. No artifacts are needed or consulted. |
| `rejected` | `training_exit_code` 0, `gate_exit_code` 1, non-empty `gate_failures`, no `error`; `training_receipt.json` matching this request/run/model with the worker's own checks; `model.joblib` hash equals the recorded hash; `gate.json` is `FAIL`, `test_set_opened=false`, failures identical to `result.json`, any identity hash equals the file on disk. |
| `succeeded` | as `rejected` but `gate_exit_code` 0 and no failures; `gate.json` is a clean `PASS` whose model and report hashes equal the files on disk; `validation.json` is a validation-only report (`test_evaluated=false`) naming the same model hash. |

Artifact paths are confined to `artifacts/xgboost_tracked/<run_id>/`.
If any condition fails the row is left unchanged and every problem is listed.

## What happens on apply

One `BEGIN IMMEDIATE` transaction: create `recovery_audit` if needed, re-read
the row (must still be `running`), re-hash `result.json`, run a conditional
`UPDATE ... WHERE status = 'running'`, insert the audit row, commit. Any
database error rolls everything back. `recovery_audit` has `UNIQUE(request_id)`
and records previous/new status, result path and hash, evidence facts, reason,
UTC time and the hash of the recovery code.

It never runs training or the gate, never requeues, never registers or
promotes a model, never changes routing, and never writes or replaces
`result.json`. Repeating `--recover` returns `already_recovered`.
A row that is already terminal but disagrees with `result.json` is reported as
a `conflict` and not changed.

After recovery the request is terminal, so the usual cooldown applies before a
new drift request for the same scope can be queued.
