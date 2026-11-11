# FraudShield Operator Guide

This guide is for the person who runs the system. It holds the commands, the
monitoring and recovery notes, and the security limits. Day-to-day use of the web
page is described in [user_manual.md](user_manual.md). `<project-directory>` stands
for the folder that contains this repository.

## Scope and prerequisites

FraudShield is a local demonstration using synthetic PaySim data. Assessments do
not authorize payments. These instructions resume the existing prepared Ubuntu VM
at `<project-directory>`; they are not a complete fresh-machine
restoration procedure. Existing images, model packages, data, runtime directories,
Python environments and ignored credentials must already be present.

## Start or resume after a VM restart

Run commands in the VM terminal:

```bash
cd <project-directory>
docker compose up -d --no-build --wait --wait-timeout 180
docker compose ps
```

This starts the API, frontend, Prometheus and Grafana. Existing files and saved
reports do not need to be recreated after an ordinary restart. `--no-build` uses
existing images; build the affected image separately after changing its source.

Optional canary stack, after the main stack is ready:

```bash
export GATEWAY_UID="$(id -u)"
export GATEWAY_GID="$(id -g)"
docker compose -f compose.canary.yaml --profile canary up -d --no-build --wait --wait-timeout 180
docker compose -f compose.canary.yaml --profile canary ps -a
```

The exported IDs let the non-root containers read the prepared model packages and
local key. Keep the existing `runtime/gateway-admin.key` private; do not print,
commit or paste its value. Export the IDs again in a new shell when required.

Optional alert stack, after the main network exists:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
mkdir -p runtime/alerts
docker compose -f compose.alerts.yaml up -d --no-build --wait --wait-timeout 180
docker compose -f compose.alerts.yaml ps -a
```

Slow VM startup can exceed a health-check deadline. Check current state and logs
before repeating commands. If both models have become healthy but the gateway is
still stopped, repeat the canary `up` command. Do not rebuild or retrain merely
because startup was slow.

## Addresses

| Purpose | Browser address |
|---|---|
| Transaction interface | `http://127.0.0.1:8080` |
| Pipeline interface | `http://127.0.0.1:8080/pipeline.html` |
| Direct API readiness | `http://127.0.0.1:8000/health` |
| API interactive documentation | `http://127.0.0.1:8000/docs` |
| Prometheus | `http://127.0.0.1:9095` |
| Grafana | `http://127.0.0.1:3010` |
| Optional gateway routing state | `http://127.0.0.1:8020/routing` |

Opening `http://localhost:8000` without a route returns `Not Found`; that is not
the transaction webpage. Use port 8080 for the webpage and `/health` or `/docs`
on port 8000. These addresses refer to the machine running the browser. A browser
on another machine needs a separately configured access arrangement.

## Inspect the API and canary gateway

These read-only checks do not change routing:

```bash
curl -fsS --max-time 10 http://127.0.0.1:8000/health
curl -fsS --max-time 10 http://127.0.0.1:8020/routing
```

The gateway normally starts at zero challenger weight. Its `/health` reports
configuration status, not a successful upstream prediction. `/routing` shows
weight, challenger configuration, fallback attempts and automatic removals.
Counters and weight are process-local and reset on gateway restart.

Gateway predictions use `/predict`. Weight changes require the admin endpoint
and the private key. Keep weight at zero during ordinary inspection. A failed
challenger request triggers one champion fallback and, when enabled, removes
challenger traffic. Restarting the challenger does not re-enable its weight.
This removes traffic; it does not replace an artifact or promote a new model.
See `reports/compose_canary_drill.json` for the completed controlled drill rather
than stopping a service during normal use.

## Monitoring and notifications

In Grafana open **FraudShield — Service Monitoring**. API and gateway scrape
health should show 1 when Prometheus reaches them. Gateway panels display weight,
fallbacks, automatic removals, request rate and estimated p95 latency. Rate and
latency plots need recent requests and enough scrape samples; an idle panel can
show no data. The panels describe different API and gateway traffic paths.

Log in using the existing local Grafana credentials. Changing the initial password
in an environment file does not necessarily reset an existing Grafana database.
Do not publish credentials in screenshots or logs.

Input drift compares recent successful direct-API predictions with training
reference distributions. It needs at least 100 observations in its 500-input
window. Explanation and gateway requests do not populate that monitor. Drift is
a distribution-change signal, not measured accuracy loss. The configured alert
also requires recent prediction traffic and a healthy API scrape.

With the optional alert stack running, Alertmanager sends firing and resolved
notifications to the local receiver. Sanitized delivery records are appended to
`runtime/alerts/notifications.jsonl`. Notification delivery can repeat; the queue
applies duplicate, active-request and cooldown controls. No public receiver port
or external messaging service is required.

## Pipeline history and controlled retraining

The pipeline page displays saved summaries and recorded execution outcomes. Its
summary is a historical snapshot, not a live query of MLflow or the registry.
The local CI entry point is:

```bash
.venv/bin/python scripts/run_ci.py
```

Run it when verification is needed, not on every browser refresh. It records test
and DVC command outcomes separately. An unchanged DVC stage may be skipped.

To inspect delivered notifications and queued requests:

```bash
PYTHONPATH=src:. .venv/bin/python scripts/enqueue_drift_requests.py
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --list
```

The scanner can run with `--watch`; it is not automatically installed as a
continuous service. It ignores synthetic delivery checks, resolved alerts and
out-of-window records. An empty list means there is no eligible listed work;
do not create synthetic requests just to populate it.

To execute an eligible queued request, substitute its actual UUID:

```bash
PYTHONPATH=src:. OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 \
.venv/bin/python scripts/run_retraining_worker.py --request-id REQUEST_UUID
```

Run one worker at a time. It trains a separate MLflow candidate and checks its
saved validation report and checksum. It does not promote or register a model,
change serving weights, or open the test set. Review the resulting receipt, gate,
logs and terminal queue state. A completed request is deliberately refused on
replay. An interrupted running request needs investigation; do not reset it
blindly or remove the lock to force concurrent work.

## Troubleshooting and shutdown

| Symptom | Action |
|---|---|
| Browser shows API root `Not Found` | Open port 8080 for the interface or port 8000 `/health` |
| API unavailable after connection edit | Restore the default and check API container state |
| Container healthy but prediction fails | Inspect upstream logs and make a scoring diagnostic; health alone is insufficient |
| Model package permission error | Check package readability and configured host UID/GID; do not make the key public |
| Gateway HTTP 503 under load | Preserve the report and diagnostic logs; earlier intermittent failures remain unresolved |
| Benchmark output already exists | Preserve it; choose a new output filename for a separately justified run |
| Shell shows only `>` after pasted code | Press Ctrl+C to cancel the incomplete command before starting again |
| VM freezes | Resume existing stacks and inspect resource/startup evidence; do not assume data loss or a proven cause |

Inspect a relevant stack with `docker compose ... ps -a` and
`docker compose ... logs --no-color --tail=80`. The `-f` and `--profile` options
must match the stack being inspected. Avoid pasting secrets from configuration.

Stop services without deleting volumes:

```bash
docker compose -f compose.alerts.yaml stop
docker compose -f compose.canary.yaml --profile canary stop
docker compose stop
```

Do not use `down -v` when you want to preserve monitoring data. Keep untracked
backups such as `compose.yaml.before-grafana` separate from intended commits.

## VM and container resource monitoring

Start the main stack first, then run:
`docker compose -f compose.resources.yaml up -d`

Dashboard: http://127.0.0.1:3010/d/fraudshield-resources

Six panels show VM CPU, memory, root disk usage, exporter health,
and container CPU and memory. Scrapes occur every 30 seconds.
CPU rates use a five-minute window.

Node Exporter reads host metrics. cAdvisor requires privileged host access.
Neither exporter publishes a host port. The existing Ubuntu VM and
fraudshield_default Docker network are required.

Stop with: `docker compose -f compose.resources.yaml stop`

Evidence: reports/resource_monitoring_verification.json.
Both exporters were up; all six queries passed. Container CPU and memory
queries each returned 11 series at verification time.
Resource metrics do not prove application correctness or model accuracy.

## Logging

The API, the canary gateway, the alert receiver and the retraining worker command
write log lines to standard error through `backend/logging_config.py`. Each line has
a timestamp, level, logger name and message. The level comes from the environment
variable `FRAUDSHIELD_LOG_LEVEL` (`DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`);
the default is `INFO`, and an unknown value falls back to `INFO`. Only the project's
own loggers (`backend`, `fraudshield`) are configured. Container output is read with
the `docker compose ... logs` command shown under Troubleshooting.

What is logged: service start-up and model load (the first 12 characters of the model
SHA-256, never the admin key), rejected requests (the URL path only), scoring failures
returned as HTTP 503, canary fallback and automatic removal, rejected or accepted
canary weight changes, notification receipt, and the retraining worker's start, finish
and outcome. Request bodies, transaction values and keys are not logged; tests in
`tests/test_logging.py` check this for the canary, gateway, API and receiver.

The container images copy `backend/` files one by one, so `backend/logging_config.py`
is listed in the `Dockerfile`. The images must be rebuilt to pick it up (not verified:
no image was rebuilt when this was written).

## Monitoring coverage

Which component is observed, and how, is listed in the table in
[high_level_design.md](high_level_design.md#monitoring-coverage). In short: the API
and gateway are scraped by Prometheus, the host and containers by exporters (optional
resource stack), the MLflow model servers only indirectly through the gateway's
metrics and health checks, and the retraining worker through the files it writes.

## Which commit and which run produced this model

`scripts/build_reproducibility_manifest.py` answers this from files, git and the MLflow
database, without training and without changing anything. It hashes
`artifacts/xgboost_v1/model.joblib`, opens `runtime/mlflow/tracking.db` read-only
through SQLite (no MLflow migration can run), and writes
`reports/reproducibility_manifest.json`. The manifest is **create-only**: if the file
exists the script stops with exit code 2 and leaves it alone. To refresh it, move the old
file away yourself (do not delete it) and run the script again.

```bash
git status --short          # commit first: the manifest records whether the tree is clean
PYTHONPATH=src:. .venv/bin/python scripts/build_reproducibility_manifest.py
```

Reading the manifest:

| Question | Where to look |
|---|---|
| Is this the model the registry describes? | `deployed_model.sha256`, and `registry_receipt.matches_deployed_file` |
| Which registered version is it? | `registry.versions[]` entries with `is_deployed_model: true` (name, version, aliases) |
| Which MLflow runs produced it? | `runs` inside that entry: the run registered with the version, then the run named by its `source_run_id` tag |
| Which commit did each run record? | `git_commit` on each run; `commit_tags` shows every commit-like tag verbatim |
| Does that commit exist in this repository? | `git_commit_exists_in_repository` (`false` means the tag names a commit this clone does not have) |
| Which commit trained it? | `training_code_commit`. The value `not_established` is kept as written: the historical imports could not establish it |
| Which data? | `data.raw_data.md5` from `paysim.dvc`, checked against `dvc.lock` (`pointer_and_lock_agree`) |
| Which commit holds the report and gate files? | `report_files.<path>.last_commit`, and `uncommitted_changes` |
| Does the validation gate file describe this model? | `validation_gate.describes_deployed_model` |
| Other runs about this model | `registry.other_runs_tagged_with_model_sha256` (for example the feature-importance run) |

Rules the script follows: a value that is not in the source is written as `"missing"`
and never guessed; a commit tag is accepted only if it is a 40-character hexadecimal
hash; package versions are those of the environment that ran the script, not proof of
what trained the model. Read `training_code_commit` and `git_commit` differently: the
second is the code state when the run was logged, which for a historical import is the
import, not the training.

Check the data fields and the gate field before relying on the answer. On 2026-11-12 a
dry read of the real files showed that `reports/xgboost_validation_gate.json` describes
a different model (`xgboost_dvc_v1`, hash starting `4b1fa5ce`) from the deployed one
(hash starting `1d2df493`), so that gate result is not evidence about the deployed file.

## Manual model rollback (documented, not drilled)

This procedure was written from the code. It has **not been executed or tested**, and
there is no recorded evidence of it working. The system has no automatic model
rollback and no promotion step (`docs/high_level_design.md`).

What decides which model the direct API serves: at start-up `backend/app.py` hashes
`artifacts/xgboost_v1/model.joblib` and refuses to start unless the hash and threshold
equal the `xgboost` entry of `reports/mlflow_registry.json`. Both files are mounted
read-only by `compose.yaml`. To return to an earlier model:

```bash
# 1. Find the earlier receipt and model file in Git history (if they were committed).
git log --oneline -- reports/mlflow_registry.json artifacts/xgboost_v1/model.joblib

# 2. Keep the current model file, then restore the earlier model and its receipt.
cp artifacts/xgboost_v1/model.joblib artifacts/xgboost_v1/model.joblib.current
git show <commit>:artifacts/xgboost_v1/model.joblib > artifacts/xgboost_v1/model.joblib
git show <commit>:reports/mlflow_registry.json > reports/mlflow_registry.json

# 3. Verify: the two hashes must be identical.
sha256sum artifacts/xgboost_v1/model.joblib
python3 -c "import json; print(json.load(open('reports/mlflow_registry.json'))['models']['xgboost']['model_sha256'])"

# 4. Restart the API so it reloads and re-checks the model, then confirm the hash.
docker compose restart api
curl -fsS --max-time 10 http://127.0.0.1:8000/health
```

Notes and limits:

- If the earlier model file was never committed, restore it from your own backup. Do
  not skip the hash check in step 3.
- Restoring `reports/mlflow_registry.json` changes a tracked evidence file; record why.
- If the canary stack is running, restart the gateway so it reads the same receipt:
  `docker compose -f compose.canary.yaml --profile canary restart canary-gateway`.
- The champion model-server package under `runtime/mlflow-serving/identity-v1/` is not
  covered. `scripts/package_identity_models.py` has the approved hashes written into
  the script and refuses to overwrite an existing output folder, so it cannot rebuild
  an earlier champion package as it stands.
- The commands above were not run; the `docker compose` service names come from the
  compose files, and whether a restart is enough to reload the mounted file is not
  verified.

## Security measures and limits

The measures that exist and those that do not are listed in
[high_level_design.md](high_level_design.md#reliability-privacy-and-security). The
deployment is local-only for a course project: every published port is bound to
`127.0.0.1` in the compose files, so TLS and encryption at rest were not added.
