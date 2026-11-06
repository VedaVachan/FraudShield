# FraudShield User Manual

## Scope and prerequisites

FraudShield is a local demonstration using synthetic PaySim data. Assessments do
not authorize payments. These instructions resume the existing prepared Ubuntu VM
at `/home/vedavachan/projects/fraudshield`; they are not a complete fresh-machine
restoration procedure. Existing images, model packages, data, runtime directories,
Python environments and ignored credentials must already be present.

## Start or resume after a VM restart

Run commands in the VM terminal:

```bash
cd /home/vedavachan/projects/fraudshield
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

## Check a transaction

1. Open the transaction interface and wait for **API ready**.
2. Select TRANSFER, CASH_OUT, CASH_IN, PAYMENT or DEBIT.
3. Enter a nonnegative finite amount and sender balance in the same monetary unit.
4. Select **Check transaction**, or **Check + explain** for feature contributions.

The model flags an input when its score meets its decision threshold. The score
is not a calibrated fraud probability. An unflagged input is not proof that a
payment is legitimate. The displayed model name and version identify the API's
reported configuration.

The explanation lists nine engineered-feature contributions in log-odds. Positive
values push the score toward fraud and negative values push it away. Contributions
can exist for zero-valued indicators. They explain the model calculation, not
causes or proof of fraud. The base value plus contributions approximately equals
the raw margin. Editing inputs clears the previous assessment.

## Change the API connection

Use **Local API address inside the transaction page**, not the browser address
bar. The default is `http://127.0.0.1:8000`.

- Enter `http://localhost:8000` and select **Apply and check connection** to use
  that equivalent local origin.
- Select **Restore default** to return to the default origin.
- Only HTTP/HTTPS localhost or 127.0.0.1 origins are accepted, with an optional
  port and without credentials, a non-root path, query or fragment.
- An unused local port such as 8999 produces an unavailable message. A nonlocal
  origin is rejected without replacing the active valid origin.

The browser stores the selected origin in localStorage, not transaction inputs.
Explicit reload-persistence verification remains pending in the recorded manual
checks. The pipeline page has its own address setting. The canary gateway is not
a drop-in replacement for this page: it has no explanation endpoint and a
different response contract.

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
