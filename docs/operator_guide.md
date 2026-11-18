# FraudShield operator guide

This is for whoever runs the system: starting and stopping it, checking it, monitoring,
retraining, rollback and the security limits. Using the web page itself is covered in the
[user manual](user_manual.md). Below, `<project-directory>` means the folder this repository
is in.

## Before you start

FraudShield is a local demo on synthetic PaySim data, and its assessments don't authorise
any payments. These steps are for bringing the already-prepared Ubuntu VM back up. They are
not a full guide to setting it up on a new machine: the Docker images, model packages, data,
runtime folders, Python environments and the credentials that aren't in Git all need to be
there already.

## Starting after a VM restart

In a terminal on the VM:

```bash
cd <project-directory>
docker compose up -d --no-build --wait --wait-timeout 180
docker compose ps
```

That starts the API, the frontend, Prometheus and Grafana. After a normal restart nothing
needs to be recreated. `--no-build` reuses the existing images, so if you changed the code
for one of them, build that image separately first.

The canary stack is optional. Start it once the main stack is up:

```bash
export GATEWAY_UID="$(id -u)"
export GATEWAY_GID="$(id -g)"
docker compose -f compose.canary.yaml --profile canary up -d --no-build --wait --wait-timeout 180
docker compose -f compose.canary.yaml --profile canary ps -a
```

The two exported IDs let the non-root containers read the model packages and the local
key. You need to export them again in every new shell. Keep `runtime/gateway-admin.key`
private: don't print it, commit it or paste it anywhere.

The alert stack is optional too, and needs the main stack's network to exist:

```bash
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
mkdir -p runtime/alerts
docker compose -f compose.alerts.yaml up -d --no-build --wait --wait-timeout 180
docker compose -f compose.alerts.yaml ps -a
```

The VM is sometimes slow enough to miss a health-check deadline. Look at the current state
and the logs before running anything again. If both model servers are healthy but the
gateway stopped, just run the canary `up` command again. A slow start is not a reason to
rebuild or retrain.

## Addresses

| What | Address |
|---|---|
| Transaction page | `http://127.0.0.1:8080` |
| Pipeline page | `http://127.0.0.1:8080/pipeline.html` |
| API readiness | `http://127.0.0.1:8000/health` |
| API documentation | `http://127.0.0.1:8000/docs` |
| Prometheus | `http://127.0.0.1:9095` |
| Grafana | `http://127.0.0.1:3010` |
| Gateway routing state (canary stack) | `http://127.0.0.1:8020/routing` |

`http://localhost:8000` on its own returns `Not Found`. That's normal, it just isn't the web
page: use port 8080 for the page and `/health` or `/docs` on port 8000. These addresses only
work in a browser on the same machine; reaching them from another machine needs its own
setup.

## Checking the API and the gateway

These don't change anything:

```bash
curl -fsS --max-time 10 http://127.0.0.1:8000/health
curl -fsS --max-time 10 http://127.0.0.1:8020/routing
```

The gateway starts with the challenger weight at zero. Its `/health` only tells you it's
configured, not that a model answered. `/routing` shows the weight, whether a challenger is
configured, the number of fallbacks and the number of automatic removals. All of these live
in the gateway process and reset when it restarts.

Predictions through the gateway go to `/predict`. Changing the weight needs the admin
endpoint and the private key; leave it at zero when you're just looking around. If a
challenger request fails, the gateway tries the champion once and (when enabled) stops
sending traffic to the challenger. Restarting the challenger doesn't turn its traffic back
on. This only moves traffic; it doesn't swap a model file or promote anything. The
controlled drill I ran is recorded in `reports/compose_canary_drill.json`, so there's no
need to stop services to see it happen.

## Monitoring and alerts

In Grafana, open "FraudShield — Service Monitoring". API and gateway scrape health show 1
when Prometheus can reach them. The gateway panels show the weight, fallbacks, automatic
removals, request rate and estimated p95 latency. Rate and latency need recent traffic and a
few scrapes, so an idle panel can show "No data". The API and gateway panels describe two
different traffic paths.

Log in with the local Grafana credentials you already have. Changing the initial password in
`.env` doesn't necessarily change it in an existing Grafana database. Keep credentials out of
screenshots and logs.

The drift monitor compares the inputs of recent successful API predictions with the
training data. It needs at least 100 predictions in its 500-prediction window, and
explanations and gateway requests don't count. Drift means the inputs look different; it
doesn't mean accuracy dropped. The alert also needs recent prediction traffic and a healthy
API scrape before it fires.

With the alert stack running, Alertmanager sends firing and resolved notifications to the
local receiver, which appends a cleaned-up record to `runtime/alerts/notifications.jsonl`.
The same notification can arrive more than once; the queue handles duplicates, active
requests and the cooldown. Nothing needs a public port or an outside messaging service.

## Pipeline history and retraining

The pipeline page shows saved summaries and the commands that ran. Its summary is a
snapshot, not a live view of MLflow or the registry. To run local CI:

```bash
.venv/bin/python scripts/run_ci.py
```

Run it when you actually want to check something, not every time you refresh the page. It
records the test run and the DVC run separately, and DVC may skip stages that haven't
changed.

To look at delivered notifications and queued requests:

```bash
PYTHONPATH=src:. .venv/bin/python scripts/enqueue_drift_requests.py
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --list
```

The scanner can also run with `--watch`, but it isn't installed as a service. It ignores
synthetic test deliveries, resolved alerts and old records. An empty list just means there's
nothing to do; don't create fake requests to fill it.

To run a queued request, use its real UUID:

```bash
PYTHONPATH=src:. OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 \
.venv/bin/python scripts/run_retraining_worker.py --request-id REQUEST_UUID
```

Only run one worker at a time. It trains a separate MLflow candidate and checks its saved
validation report and checksum. It doesn't register or promote a model, change any serving
weights or open the test set. Afterwards, look at the receipt, the gate result, the logs and
the request's final status. Running a finished request again is refused on purpose. If a
request is stuck in `running`, investigate it; don't reset it or delete the lock to force
another worker through.

If you want queued requests to run on their own, start the worker in watch mode. It's off
unless you do this:

```bash
PYTHONPATH=src:. OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 \
.venv/bin/python scripts/run_retraining_worker.py --watch --interval 60
```

It checks the queue every `--interval` seconds (default 30, must be positive) and runs
requests one at a time through the same path as `--request-id`: same lock, same check that
the worker code is committed, same candidate-only training and gate. Stop it with Ctrl+C or
SIGTERM. It never registers, promotes or deploys a model; promoting a candidate is always a
separate manual step (`scripts/promote_model.py`). I've only tested watch mode with fake
training commands. [retraining_recovery.md](retraining_recovery.md) explains how it stops and
what to do if it exits with 1.

## When something goes wrong

| What you see | What to do |
|---|---|
| The browser shows `Not Found` for the API | Open port 8080 for the page, or `/health` on port 8000 |
| "API unavailable" after changing the address | Press "Restore default" and check the API container |
| Container is healthy but predictions fail | Check the upstream logs and send a test prediction; a health check alone isn't enough |
| Permission error on a model package | Check the package is readable and the UID/GID you exported; don't make the key public |
| Gateway returns 503 under load | Keep the report and the logs; the earlier 503s were never explained |
| A benchmark output file already exists | Keep it, and pick a new file name if you really need another run |
| The shell only shows `>` after pasting | Press Ctrl+C to cancel the half-finished command and start again |
| The VM freezes | Bring the stacks back up and look at the resource and startup evidence; don't assume data was lost or guess a cause |

To look at a stack, use `docker compose ... ps -a` and
`docker compose ... logs --no-color --tail=80`, with the same `-f` and `--profile` options
you started it with. Be careful not to paste secrets from configuration files.

To stop everything without deleting volumes:

```bash
docker compose -f compose.alerts.yaml stop
docker compose -f compose.canary.yaml --profile canary stop
docker compose stop
```

Don't use `down -v` if you want to keep the monitoring data. Keep local backup files such as
`compose.yaml.before-grafana` out of your commits.

## VM and container resources

Start the main stack first, then:

```bash
docker compose -f compose.resources.yaml up -d
```

The dashboard is at http://127.0.0.1:3010/d/fraudshield-resources. Its six panels show VM
CPU, memory and root disk, exporter health, and container CPU and memory. Prometheus scrapes
every 30 seconds and the CPU rates use a five-minute window.

Node Exporter reads the host's metrics and cAdvisor needs privileged access to the host.
Neither publishes a port on the host. Both need the existing VM and the
`fraudshield_default` Docker network.

Stop it with `docker compose -f compose.resources.yaml stop`.

When I checked it (`reports/resource_monitoring_verification.json`), both exporters were up,
all six queries worked, and the container CPU and memory queries each returned 11 series.
Resource numbers say nothing about whether the application or the model is right.

## Logging

The API, the canary gateway, the alert receiver and the retraining worker all write log
lines to standard error through `backend/logging_config.py`. Each line has a timestamp,
level, logger name and message. Set the level with `FRAUDSHIELD_LOG_LEVEL` (`DEBUG`, `INFO`,
`WARNING`, `ERROR` or `CRITICAL`); it defaults to `INFO`, and anything it doesn't recognise
also means `INFO`. Only the project's own loggers (`backend` and `fraudshield`) are set up.
For containers, read the output with the `docker compose ... logs` command above.

What gets logged: start-up and model loading (with the first 12 characters of the model
SHA-256, never the admin key), rejected requests (just the URL path), scoring failures
returned as 503, canary fallbacks and automatic removals, accepted or rejected weight
changes, received notifications, and the retraining worker's start, finish and result.
Request bodies, transaction values and keys are never logged; `tests/test_logging.py`
checks this for the canary, gateway, API and receiver.

The images copy `backend/` files one by one, so `backend/logging_config.py` is listed in the
`Dockerfile`. A container only gets it after its image is rebuilt.

## What's monitored

The full list is in the table in
[high_level_design.md](high_level_design.md#what-is-monitored). Briefly: Prometheus scrapes
the API and the gateway; the host and containers are covered by the exporters in the
optional resource stack; the MLflow model servers are only seen indirectly through the
gateway's metrics and health checks; and the retraining worker is followed through the files
it writes.

## Which commit and which run produced the served model

`scripts/build_reproducibility_manifest.py` works this out from files, git and the MLflow
database. It doesn't train or change anything. It hashes
`artifacts/xgboost_v1/model.joblib`, opens `runtime/mlflow/tracking.db` read-only through
SQLite (so MLflow can't run a migration on it) and writes
`reports/reproducibility_manifest.json`. It never overwrites that file: if it exists, the
script stops with exit code 2. To make a fresh one, move the old file somewhere else first
(don't delete it).

```bash
git status --short          # commit first; the manifest records whether the tree was clean
PYTHONPATH=src:. .venv/bin/python scripts/build_reproducibility_manifest.py
```

How to read it:

| Question | Where to look |
|---|---|
| Is this the model the registry describes? | `deployed_model.sha256` and `registry_receipt.matches_deployed_file` |
| Which registered version is it? | The `registry.versions[]` entry with `is_deployed_model: true` (name, version, aliases) |
| Which MLflow runs produced it? | That entry's `runs`: the run registered with the version, then the run its `source_run_id` tag points to |
| Which commit did each run record? | `git_commit` on each run; `commit_tags` shows every commit-like tag exactly as stored |
| Is that commit in this repository? | `git_commit_exists_in_repository` (`false` means this clone doesn't have it) |
| Which commit trained it? | `training_code_commit`. `not_established` is kept as written: the historical imports couldn't tell |
| Which data? | `data.raw_data.md5` from `paysim.dvc`, checked against `dvc.lock` (`pointer_and_lock_agree`) |
| Which commit holds the report and gate files? | `report_files.<path>.last_commit` and `uncommitted_changes` |
| Is the validation gate file about this model? | `validation_gate.describes_deployed_model` |
| Any other runs about this model? | `registry.other_runs_tagged_with_model_sha256` (the feature-importance run, for example) |

The script never guesses: anything it can't find is written as `"missing"`, a commit tag
only counts if it's a full 40-character hash, and the package versions it lists are from the
environment that ran the script, not necessarily the one that trained the model. Don't
confuse `git_commit` with `training_code_commit`: `git_commit` is the code at the moment the
run was logged, which for a historical import is the import, not the training.

Check the data and gate fields before relying on the answer. When I ran it on 2026-11-12,
it showed that `reports/xgboost_validation_gate.json` is about a different model
(`xgboost_dvc_v1`, hash starting `4b1fa5ce`) from the one being served (hash starting
`1d2df493`), so that gate result says nothing about the served file.

## Rolling back a model

"Rollback" can mean two different things here, and I've only drilled one of them.

| What | What it changes | Status |
|---|---|---|
| Moving the registry alias back (`scripts/promote_model.py --rollback`) | The MLflow `champion` alias and one registered-model tag | Drilled on a temporary copy of the MLflow database with `scripts/rollback_drill.py`, and unit-tested on throwaway databases |
| Serving rollback (model file and receipt, then restart the API) | Which model the API actually uses | Written down only. Never run, never drilled |

They're not connected. The API and gateway don't read registry aliases, so moving an alias
doesn't change what's served, and restoring a model file doesn't move any alias.

### Registry alias rollback (drilled on a copy)

```bash
PYTHONPATH=src:. .venv/bin/python scripts/rollback_drill.py
```

The drill copies `runtime/mlflow/tracking.db` into a temporary folder (the original is only
opened read-only), registers a made-up second version of `FraudShield-XGBoost` in the copy,
promotes v1, promotes v2, rolls back, and checks that `champion` points at v1 again with v2
recorded as the previous version. It refuses to work on the real database or in its folder,
and writes the real database's SHA-256 from before and after into
`reports/rollback_drill.json`. That report is never overwritten, so move the old one away to
run the drill again. Exit code 0 means every step did what it should.

What this proves: the alias logic and its gate work on a copy of the real registry. What it
doesn't: anything about serving, that the real registry has been moved, or anything about a
real second model, since version 2 is synthetic and only exists in the copy.

Nobody has done this on the real registry yet. If you do, dry-run first:

```bash
.venv/bin/python scripts/promote_model.py --rollback            # dry run, changes nothing
.venv/bin/python scripts/promote_model.py --rollback --apply    # actually moves the alias
```

### Serving rollback (written down, never run)

I wrote this from the code and haven't tried it. When the API starts, `backend/app.py`
hashes `artifacts/xgboost_v1/model.joblib` and won't start unless the hash and threshold
match the `xgboost` entry in `reports/mlflow_registry.json`. Both files are mounted
read-only by `compose.yaml`. To go back to an earlier model:

```bash
# 1. Find the earlier receipt and model file in Git history (if they were committed).
git log --oneline -- reports/mlflow_registry.json artifacts/xgboost_v1/model.joblib

# 2. Keep a copy of the current model, then restore the earlier model and its receipt.
cp artifacts/xgboost_v1/model.joblib artifacts/xgboost_v1/model.joblib.current
git show <commit>:artifacts/xgboost_v1/model.joblib > artifacts/xgboost_v1/model.joblib
git show <commit>:reports/mlflow_registry.json > reports/mlflow_registry.json

# 3. Check: these two hashes must be the same.
sha256sum artifacts/xgboost_v1/model.joblib
python3 -c "import json; print(json.load(open('reports/mlflow_registry.json'))['models']['xgboost']['model_sha256'])"

# 4. Restart the API so it loads and checks the model again.
docker compose restart api

# 5. Confirm what's being served: status should be "ready" and model_sha256 should equal
#    the hash from step 3.
curl -fsS --max-time 10 http://127.0.0.1:8000/health
```

So the only things to put back are `artifacts/xgboost_v1/model.joblib` and its receipt
`reports/mlflow_registry.json` (the `xgboost` entry holds the expected hash and threshold);
there's no other setting. After the restart `/health` should say `ready` and show the loaded
`model_sha256`. If the model file and the receipt don't match, the API won't start, so if
`/health` never answers, step 3 went wrong. Don't edit the receipt to match a file you
haven't identified. To go forward again, do the same thing with the `model.joblib.current`
copy from step 2.

Things to watch out for:

- If the earlier model file was never committed, you'll need your own backup of it. Don't
  skip the hash check.
- Restoring `reports/mlflow_registry.json` changes a tracked evidence file, so write down why.
- If the canary stack is running, restart the gateway too so it reads the same receipt:
  `docker compose -f compose.canary.yaml --profile canary restart canary-gateway`.
- This doesn't cover the champion model-server package in
  `runtime/mlflow-serving/identity-v1/`. `scripts/package_identity_models.py` has the
  approved hashes hard-coded and won't overwrite an existing output folder, so as it stands
  it can't rebuild an older champion package.
- I haven't run any of these commands. The service names come from the compose files, and I
  don't know for sure that a restart is enough to pick up the mounted file.

## Security

What's in place and what isn't is listed in
[high_level_design.md](high_level_design.md#reliability-privacy-and-security). In short,
this is a local-only course deployment: every published port is bound to `127.0.0.1` in the
compose files, which is why I didn't add TLS or encryption at rest.
