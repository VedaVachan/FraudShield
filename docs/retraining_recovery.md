# Retraining: what runs on its own, and recovering a stuck request

## What is automatic

| Step | Automatic? |
|---|---|
| Spotting input drift and queueing a retraining request | Yes |
| Running a queued request | No by default (`--request-id`); yes only if you start the worker with `--watch` |
| Registering, promoting or deploying a model | Never. Promotion is always a separate manual step with its own gate |

### The `--watch` mode

`scripts/run_retraining_worker.py --watch [--interval SECONDS]` is off unless you start it.
It checks the queue every `--interval` seconds (30 by default; zero or negative is refused)
and runs each queued request through exactly the same function as `--request-id`. That
means the same worker lock, the same check that the worker code is committed (done before
every request), and the same candidate-only training and gate. It never registers,
promotes or deploys anything.

How it behaves, which matters if something goes wrong:

- It logs every decision: starting up, running a request, the result, finding the lock
  busy, stopping.
- It never retries a request. A finished request stays finished. If the worker lock is held
  by someone else, it just tries the same request again on the next poll.
- Ctrl+C or SIGTERM while it's waiting stops it cleanly with exit code 0. If a request is
  running at that moment, the worker's normal interrupt handling stops the training and its
  child processes and marks the request `failed`, with the interruption as the error. Then
  the watch ends.
- If the worker code isn't committed and unchanged, it exits with 1 before running
  anything. If a request crashes before its final state is saved, it also exits with 1 and
  leaves that row `running`; use the recovery steps below.

I've only tested `--watch` with fake training commands. It hasn't been run against real
training.

## When a request gets stuck in `running`

The worker first writes `runtime/retraining/runs/<request_id>/result.json` (atomically) and
only then saves the final status in SQLite. If the process dies, or the database update
fails, in between, you end up with a row still marked `running` even though a complete
result exists. The worker deliberately won't run that row again; it answers `not_queued`.

That's different from a worker that was killed before writing `result.json`. A leftover
`.result-*.tmp` file, how long ago it started, or a missing process prove nothing either
way. Recovery refuses those rows and leaves them for a person to investigate.

## The two commands

Both take the same worker lock as a normal run. If a worker, or a child process that kept
the lock after the worker was killed, still has it, they answer `busy` (exit 2) and don't
read or change anything.

```bash
# 1. Look only. Opens the database read-only and writes nothing.
python scripts/run_retraining_worker.py --inspect-recovery <request_id>

# 2. Actually recover. Tied to the exact result.json you just reviewed.
python scripts/run_retraining_worker.py --recover <request_id> \
    --confirm-result-sha256 <hash printed by step 1> \
    --reason "worker killed after result publication; evidence reviewed"
```

Like a normal run, `--recover` only works when the worker code (including
`backend/retraining_recovery.py`) is committed and unchanged. `--inspect-recovery` doesn't
need that.

Exit codes: 0 means fine (recoverable, recovered, or already finished), 1 means refused or a
database error, 2 means busy.

## What the evidence has to look like

For every outcome, `result.json` has to be a regular file (not a symlink) containing strict
JSON (no NaN, no duplicate keys). Its `request_id` has to match the row, `scope` has to be
the candidate scope, `registered_or_promoted` has to be `false`, the timestamps have to
carry a time zone and be in order, and `worker_sha256` has to be valid.

On top of that:

| Outcome | Also needed |
|---|---|
| `failed` | A non-empty `error`. No model files are needed or looked at. |
| `rejected` | `training_exit_code` 0, `gate_exit_code` 1, a non-empty `gate_failures` and no `error`. `training_receipt.json` must match this request, run and model and pass the worker's own checks. The hash of `model.joblib` must equal the recorded one. `gate.json` must say `FAIL` with `test_set_opened=false`, list the same failures as `result.json`, and any hash in it must match the file on disk. |
| `succeeded` | The same as `rejected`, except `gate_exit_code` is 0 with no failures, and `gate.json` is a clean `PASS` whose model and report hashes match the files on disk. `validation.json` must be a validation-only report (`test_evaluated=false`) naming the same model hash. |

Model files must be inside `artifacts/xgboost_tracked/<run_id>/`. If anything doesn't check
out, the row is left alone and every problem is listed.

## What `--recover` actually does

Everything happens in one `BEGIN IMMEDIATE` transaction. It creates the `recovery_audit`
table if needed, reads the row again (it must still be `running`), hashes `result.json`
again, runs `UPDATE ... WHERE status = 'running'`, adds an audit row and commits. Any
database error rolls the whole thing back. `recovery_audit` allows one row per request and
records the old and new status, the result file and its hash, the evidence checked, your
reason, the UTC time and the hash of the recovery code.

It never runs training or the gate, never puts a request back in the queue, never
registers or promotes a model, never touches routing, and never writes or replaces
`result.json`. Running `--recover` again just returns `already_recovered`. If a row is
already finished but disagrees with `result.json`, it's reported as a `conflict` and left
unchanged.

Once recovered, the request counts as finished, so the normal one-hour wait applies before
a new drift request for the same API instance can be queued.
