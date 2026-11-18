# FraudShield high-level design

## The problem and who it's for

A reviewer wants to look at a payment and get a quick, explainable opinion on whether it
looks like fraud. FraudShield gives a model score, a flagged / not flagged decision based
on a threshold, and optionally an explanation. An operator, the person running the
system, can also look at pipeline records, watch the service in Grafana and try out
controlled operations like canary routing and candidate retraining. All of this is a local
course project on synthetic data; it doesn't authorise real payments.

## What I set out to do

- Validate the data and make the preparation and training reproducible.
- Keep model development apart from the final held-out evaluation.
- Serve predictions and explanations over REST.
- Monitor the service, changes in the inputs and routing failures.
- Be able to trace code, data, models and runs back to where they came from.
- Show the drift → alert → queued request → candidate training loop working end to end.

What got built: a DVC pipeline with a validation gate, a local CI history, MLflow runs
(both imported historical ones and newly tracked ones), MLflow HTTP serving, a read-only
pipeline page, a frontend you can point at a different local API, a canary gateway with
deterministic routing, key-protected weight changes, fallback and automatic removal of a
failing challenger, local alert delivery, and candidate retraining with a validation gate.

What didn't: batch scoring, a way to feed back true labels, buttons on the page to start
the pipeline, a supervised always-on retraining service, automatic promotion of a
candidate and automatic rollback of the served model.

## Main design decisions

| Decision | Why |
|---|---|
| Synthetic PaySim data | Repeatable experiments without touching real customer data |
| Splits by time | Train on earlier transactions, evaluate on later ones, like it would be used |
| Only three inputs known before the payment | Leave out balances after the payment and any customer identifiers |
| One shared feature function | Training and serving can't drift apart |
| Logistic regression and CPU XGBoost | A simple baseline next to a model that handles interactions, both fit the VM |
| Threshold picked on validation | The decision rule is explicit and the test set isn't used to tune it |
| Frontend separate from the FastAPI service | The page only talks to the model over REST |
| Docker Compose | Run all the services locally without any cloud |
| DVC and MLflow | Track pipeline dependencies and keep model runs apart |
| Total variation over buckets for drift | Memory stays bounded and full transaction values aren't kept |
| Candidates as separate files | Retraining can't overwrite the champion |
| SQLite queue and a process lock | No duplicate requests and never two workers at once |

## Data

| Split | Steps | Rows | Fraud rows |
|---|---|---:|---:|
| Train | 1–520 | 6,082,007 | 5,781 |
| Validation | 521–631 | 191,147 | 1,180 |
| Test | 632–743 | 89,466 | 1,252 |

The inputs are `type`, `amount` and `oldbalanceOrg`; `isFraud` is the target and `step` is
used for the time split. Identifiers, `isFlaggedFraud` and the balances after the payment
are kept out of the model. Tests check that the Python and Spark feature code agree. Since
the fraud patterns are simulated, the results don't carry over to real payments.

## Choosing and tracking the model

The threshold is the one with the best F1 on validation; on an exact tie the higher
threshold wins. Validation is also used for early stopping and for four Ray Tune
configurations. The tuned model had a slightly better average precision, but at its chosen
threshold its F1 was the same as the DVC baseline. I froze the DVC XGBoost baseline before
running the held-out evaluation, which gave AP 0.992652 and F1 0.978108.

The model the API actually serves is a different file: the originally registered model.
The API checks its hash and threshold against a receipt before starting. The held-out
numbers belong to the DVC baseline and shouldn't be read as results for the served model
or for any retrained candidate. The historical MLflow imports can't say which commit
originally trained them. New tracked training records the real code and data provenance
and checks the logged metrics and the downloaded artifact bytes.

## Serving and the web pages

The API runs one worker, loads the original model bundle and returns the model identity
with every answer. The browser calls it over REST. On the page you can point it at another
local API address; a valid change clears old results, and only that address is saved. If
browser storage isn't available the setting still works for the open page. The backend
still validates everything, and the browser gives up on a request after 20 seconds.

The pipeline page is read-only. It keeps three things apart: how the pipeline is set up,
the commands that actually ran, and saved snapshots of older evidence. A DVC run that found
nothing to do isn't shown as fresh training, and the saved MLflow receipts aren't live
queries.

The canary gateway is a separate path that calls champion and challenger MLflow HTTP
servers. At weight zero everything goes to the champion; above zero, a hash of the
transaction ID decides. Changing the weight needs the local admin key (which isn't in Git).
The gateway checks that responses are consistent, but that doesn't prove which model file
produced them.

## Monitoring and failure handling

Prometheus collects metrics from the API and the gateway, and Grafana shows the service,
drift and routing panels. The API's drift window holds the last 500 successful predictions
and starts reporting after 100. Explanations and invalid requests don't go into it. The
alert fires when total variation stays above 0.20 for 30 seconds, as long as there has been
recent prediction traffic and Prometheus can scrape the API. I picked 0.20 for the demo;
it isn't calibrated for production, and drift in the inputs doesn't prove the model got
worse.

If the challenger fails, the gateway tries the champion once for that request and stops
sending traffic to the challenger. It doesn't change any model files, promote anything or
cancel requests already running. The weight lives only in the gateway process and starts at
zero, and restarting the challenger doesn't turn its traffic back on.

### What is monitored

| Component | How I watch it | Where it's set up |
|---|---|---|
| Direct API | Prometheus job `fraudshield-api`: `fraudshield_requests_total`, `fraudshield_request_duration_seconds`, `fraudshield_decisions_total`, `fraudshield_drift_window_samples`, `fraudshield_input_drift_score`; container health check on `/health` | `monitoring/prometheus.yml`, `backend/metrics.py`, `compose.yaml` |
| Canary gateway | Prometheus job `fraudshield-gateway`: `fraudshield_gateway_requests_total`, `fraudshield_gateway_request_duration_seconds`, `fraudshield_gateway_scoring_results_total`, `fraudshield_gateway_canary_weight_percent`, `fraudshield_gateway_fallback_attempts_total`, `fraudshield_gateway_automatic_removals_total`; container health check | `backend/gateway_metrics.py`, `compose.canary.yaml` |
| MLflow model servers | Not scraped. I see them through the gateway's result, fallback and removal metrics and a health check on `/ping`. I didn't check whether MLflow serving has its own metrics endpoint | `monitoring/prometheus.yml`, `compose.canary.yaml` |
| The VM | Prometheus job `fraudshield-node` (Node Exporter), only while the optional resource stack runs | `compose.resources.yaml` |
| Containers | Prometheus job `fraudshield-containers` (cAdvisor), same optional stack | `compose.resources.yaml` |
| Retraining worker | It's a command-line process, so it isn't scraped. I follow it through the files it writes in `runtime/retraining/runs/<request_id>/` (`result.json`, `training.log`, `gate.json`, `gate.stderr.log`), the queue status and its log lines. The pipeline page shows CI and gate runs, not worker runs | `backend/retraining_worker.py`, `scripts/run_ci.py`, `scripts/run_model_gate.py` |
| Alerts | Rule `FraudShieldInputDrift` → Alertmanager → local receiver, which appends to `runtime/alerts/notifications.jsonl`. It records what happened and triggers nothing else | `monitoring/drift_rules.yml`, `monitoring/alertmanager.yml`, `backend/alert_receiver.py` |
| Frontend, Prometheus, Grafana | Container health checks only | `compose.yaml` |

Grafana loads two dashboards automatically: service monitoring (API, drift, gateway) and VM
and container resources. The services and the worker also log to standard error, at the
level set by `FRAUDSHIELD_LOG_LEVEL` (see [operator_guide.md](operator_guide.md#logging)).

### Rolling back a model

Nothing rolls a model back automatically. `scripts/promote_model.py` moves the MLflow
`champion` alias by hand, behind a validation-only gate, and can move it back with
`--rollback`. I drilled that alias rollback on a temporary copy of the registry. The
served model is a different story: switching it back means restoring the old model file
and receipt and restarting the API. That procedure is written down in
[operator_guide.md](operator_guide.md#rolling-back-a-model) but I've never run it. The
gateway's automatic action only removes challenger traffic.

## Alerts and retraining

Alertmanager sends notifications to a small local receiver. A scanner on the host picks up
recent firing input-drift notifications and saves them as queued requests. Duplicate
deliveries are ignored, each API instance can only have one active request, and there's a
one-hour wait before the next request for the same instance. A single global lock stops two
workers from running at once.

By default an operator starts each queued request. The worker can also poll the queue with
`--watch`, which is off unless you ask for it. Either way it trains a separate MLflow
candidate on the approved training and validation splits, checks the receipt and runs the
gate: AP at least 0.85, precision at least 0.70, recall at least 0.90. Passing the gate
doesn't compare the candidate with the champion and doesn't promote it. If a worker is
interrupted the request can be left `running` for someone to look at; nothing resumes it
automatically ([retraining_recovery.md](retraining_recovery.md)).

In the drill I ran, a real drift notification arrived, a request was queued, candidate
training and its gate completed, the served model and the zero challenger weight stayed
the same, the resolved notification arrived, and replaying the finished request was
refused. It didn't show that retraining helps with shifted traffic or new labels.

## Reliability, privacy and security

All published ports are bound to localhost. The serving containers have resource limits,
run as non-root users with read-only filesystems, and mount model files read-only. Where
bind mounts need it, the model-server and gateway container UIDs match the local operator.

The drift detector keeps bucket numbers, not the actual values. The alert receiver saves
the labels it needs rather than whole notifications. Warning logs from the model client
leave out transaction values and exception messages. The gateway admin key only protects
the gateway; the direct API, the frontend and the pipeline page have no authentication.

What's in place:

- The gateway admin key is at least 32 characters, read from a mounted secret file,
  compared in constant time and never logged (`backend/gateway.py`, `backend/gateway_server.py`).
- SHA-256 checks on model bytes: the API against its receipt, the MLflow wrapper against its
  expected hash, and the gateway client against the hash in the response.
- A strict request schema that rejects unknown fields, a generic 422 from the direct API,
  CORS limited to the local frontend, and a 64 KB limit on model responses and alert bodies.
- Non-root containers with read-only filesystems, CPU and memory limits and read-only model
  mounts; every published port on `127.0.0.1`.
- Secrets stay out of Git (`.env`, `runtime/`); `.env.example` only lists the names.
- Logs contain no keys, request bodies or transaction values (`tests/test_logging.py`).

What isn't: TLS, encryption of data, models or volumes at rest, authentication on the
direct API and the frontend, rate limiting, pseudonymisation of identifiers, and audit
logging beyond the retraining recovery table. I left these out because this is a local
course deployment on one machine with no outside clients. Note that
`compose.resources.yaml` runs cAdvisor with `privileged: true` and `pid: host`.

Checksums only tell you the bytes changed; they don't encrypt anything or prove who made
the file. Only load joblib bundles you trust.

## Testing and what's left

The full suite has 348 tests, and the last local CI run (`a6784025-ae57-4ba8-8915-c1e939fba489`)
passed with exit code 0. The [test report](test_report.md) has the details and the
history.

The load tests showed the direct API working without failures, but the gateway had 503s
during warm-up and under load, then later clean runs, and tail latency varied a lot. I
haven't found the cause, and adding logging didn't fix anything. Sustained capacity and any
kind of SLA weren't tested ([load_test_report.md](load_test_report.md)).

Still to do: check the final submission against the course requirements, record the
frontend reload check that I did by hand but never wrote down, and rebuild the system on a
fresh machine before claiming it can be restored elsewhere.
