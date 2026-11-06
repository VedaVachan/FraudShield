# FraudShield

A local, CPU-based fraud scoring and explanation service for digital payments,
using the synthetic PaySim dataset.

## What is implemented

- Chronological data splits, shared features, logistic regression and CPU XGBoost.
- Spark validation, feature parity checks and DVC-managed baseline pipelines.
- Validation-only Ray Tune experiments and a tested DVC validation gate.
- Historical MLflow imports distinguished from newly tracked training runs.
- Registered original models and verified standalone MLflow HTTP serving.
- FastAPI prediction, SHAP explanation, readiness and metrics endpoints.
- Separate frontend with a configurable local API address.
- Read-only pipeline console with execution history and historical evidence.
- Deterministic canary gateway with authenticated traffic-weight changes.
- Champion fallback and automatic challenger traffic removal on serving failure.
- Separate Docker Compose deployments for the application, canary and alerts.
- Prometheus/Grafana API, drift and gateway monitoring.
- Local Alertmanager delivery with firing and resolved notification evidence.
- Persistent drift-request queue, cooldown, worker lock and replay protection.
- Operator-started candidate retraining with MLflow provenance and validation.
- Recorded concurrent-load results, including observed gateway failures.

Continuous retraining services, automatic candidate promotion and artifact
deployment rollback are not implemented. The pipeline console is read-only.

## Environment

Development used an Ubuntu VM with 6 CPUs and 8 GB RAM.
Training and inference use CPUs.

The project `.venv` uses Python 3.11 and depends on the existing Conda
`daai` installation. DVC and Ray use separate environments under `runtime/`.
Dependency records are included in the repository.

## Data and features

Raw data: `data/raw/paysim.csv`.

Model inputs are restricted to `type`, `amount`, and `oldbalanceOrg`.
Nine features encode transaction type, log amounts, log sender balance,
amount/balance fraction, and whether the amount exceeds the balance.

| Split | Steps | Rows | Fraud rows |
|---|---|---:|---:|
| Train | 1–520 | 6,082,007 | 5,781 |
| Validation | 521–631 | 191,147 | 1,180 |
| Test | 632–743 | 89,466 | 1,252 |

Validation data was used for tuning, early stopping, and threshold selection.
The baseline selection was frozen before held-out test evaluation.

See `docs/dataset.md` and the reports under `reports/`.

## Start the local services

From the project directory, with Docker running, the model artifacts present,
and `GRAFANA_ADMIN_PASSWORD` configured in the ignored `.env` file:

    docker compose up -d --no-build --wait --wait-timeout 180
    docker compose ps

For an initial build, run:

    docker compose build api frontend
    docker compose pull prometheus grafana

| Service | Local URL |
|---|---|
| Frontend | http://127.0.0.1:8080 |
| Pipeline console | http://127.0.0.1:8080/pipeline.html |
| API documentation | http://127.0.0.1:8000/docs |
| API health | http://127.0.0.1:8000/health |
| API metrics | http://127.0.0.1:8000/metrics |
| Prometheus | http://127.0.0.1:9095 |
| Grafana | http://127.0.0.1:3010 |

Grafana dashboard UID: `fraudshield-overview`.
Grafana uses `http://prometheus:9090` inside the Compose network.

Grafana stores credentials in its persistent volume. Changing the initial
admin password environment variable does not reset an existing account.

## Frontend connection

Open http://127.0.0.1:8080. Change the Local API address field inside the page
and select Apply and check connection. The browser address stays on port 8080.

The default is http://127.0.0.1:8000. HTTP/HTTPS origins with hostname localhost
or 127.0.0.1 are accepted; credentials, paths, queries and fragments are rejected.
Only the API origin is stored in browser localStorage. Transaction inputs are
not saved by the page. Changing the active origin clears the old assessment.

Use the full prediction/explanation API for this page. The separate canary
gateway has a different response contract and is not a drop-in UI backend.
Opening the API root directly returns 404 because it has no homepage;
use /health or /docs.

## Optional canary and alert stacks

On the existing verified VM, start the main stack first so its Docker network
exists. The canary stack also requires the previously exported MLflow packages
under runtime/mlflow-serving and the ignored runtime/gateway-admin.key file.

    export GATEWAY_UID="$(id -u)"
    export GATEWAY_GID="$(id -g)"
    docker compose -f compose.canary.yaml --profile canary up -d --no-build --wait --wait-timeout 180

The gateway binds to http://127.0.0.1:8020. Its upstream model servers are
internal Docker services. Gateway startup initializes challenger weight to zero.
A healthy gateway container indicates configuration readiness, not verified
upstream prediction availability.

The alert stack requires the writable runtime/alerts directory:

    mkdir -p runtime/alerts
    export LOCAL_UID="$(id -u)"
    export LOCAL_GID="$(id -g)"
    docker compose -f compose.alerts.yaml up -d --no-build --wait --wait-timeout 180

Alertmanager and its receiver use the main stack's network and publish no host
ports. Notifications are appended to runtime/alerts/notifications.jsonl.

These commands reuse existing images and artifacts. A fresh-machine restoration
of all model packages, environments and runtime state has not been demonstrated.

## API

- `POST /predict`: fraud score, threshold, decision, and model identity.
- `POST /explain`: prediction plus feature contributions in raw model log-odds.
- `GET /health`: readiness and loaded model checksum.
- `GET /metrics`: aggregate service and input drift metrics.
- `GET /pipeline/runs`: bounded, sanitized execution history.
- `GET /pipeline/summary`: saved historical pipeline evidence.

Example:

    curl -fsS http://127.0.0.1:8000/predict \
      -H 'Content-Type: application/json' \
      -d '{"type":"TRANSFER","amount":100000,"oldbalanceOrg":100000}'

Scores and decisions are model outputs, not confirmed fraud labels.

## Model identity and evaluation

The deployed API serves the original registered v1 artifact:

    artifacts/xgboost_v1/model.joblib
    SHA256: 1d2df49373788c5b1e554ec9ab9cfc346cc0feb1e13406bc0cd93b1594518bcb

The frozen model evaluated on the held-out test split is a separate artifact:

    artifacts/xgboost_dvc_v1/model.joblib
    SHA256: 4b1fa5cead6f0d8dedcab0d99ed66adfdfd9f5b023adba41c567ca3c0c77d106

Its test results at the frozen threshold 0.9105643033981323 were:

| Metric | Value |
|---|---:|
| Average precision | 0.992652 |
| Precision | 0.957887 |
| Recall | 0.999201 |
| F1 | 0.978108 |
| True negatives | 88,159 |
| False positives | 55 |
| False negatives | 1 |
| True positives | 1,251 |

These results apply to the frozen DVC artifact on synthetic PaySim data.
They are not a held-out evaluation of the deployed artifact or tuned candidate.

Evidence:
- `reports/xgboost_model_selection.json`
- `reports/xgboost_heldout_evaluation.json`
- `reports/xgboost_ray_tune_validation.json`

## Monitoring and drift

Prometheus collects scoring request counts, latency histograms, decision counts,
and input drift metrics. Grafana displays these metrics.

Input drift compares the last 500 successful `/predict` requests against
training-only reference distributions. Scores become available after 100
samples. Invalid requests and `/explain` requests do not enter the window.
The detector retains bucket indices rather than transaction values.

The demo alert uses a drift threshold of 0.20 with a sustained condition.
Shifted traffic triggered alerts for all three inputs; reference-like traffic
replaced the window and returned the alert to inactive.

The threshold demonstrates detection mechanics and is not a calibrated
production policy. Input drift does not establish degraded model accuracy.
Prometheus now sends alerts to a local Alertmanager, which delivers firing
and resolved notifications to a separate receiver. End-to-end synthetic delivery
and actual drift recovery were verified.

The gateway exposes its own request, latency, routing, fallback and automatic
traffic-removal metrics. Grafana includes six gateway panels. These metrics
are separate from the direct API's input-drift window.

Evidence:
- reports/alert_delivery_verification.json
- reports/drift_request_drill.json
- reports/controlled_retraining_drill/recovery_and_replay.json
- reports/gateway_metrics_ci/


## Canary routing and failure recovery

The original test-client rehearsal is preserved in
reports/xgboost_canary_drill.json. Later work added a separate HTTP gateway and
containerized champion/challenger model servers.

The gateway selects routes deterministically from transaction IDs and configured
weight. Administrative weight changes require the local API key. A challenger
serving failure triggers one champion fallback attempt and, with the configured
policy enabled, sets subsequent challenger traffic to zero.

This is automatic traffic removal, not artifact rollback or model promotion.
Already in-flight requests can still finish. Routing state and counters are
process-local; restarting the challenger does not re-enable traffic.

Evidence:
- reports/compose_canary_drill.json
- reports/gateway_http_drill.json
- reports/canary_automatic_removal.json

## Controlled retraining

The host-side scanner queues recent firing FraudShieldInputDrift notifications:

    PYTHONPATH=src:. .venv/bin/python scripts/enqueue_drift_requests.py
    PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --list

The scanner can poll with --watch, but no continuously running scanner or worker
service has been installed and verified. It reads the latest 1 MiB of notification
history, accepts records within a 15-minute window, and deduplicates deliveries.
The queue permits one active request per API instance and uses a one-hour
cooldown measured from request creation.

An operator starts one queued request explicitly:

    PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --request-id REQUEST_ID

The worker uses a global process lock, records logs and outcomes, trains into
a separate MLflow run/artifact directory, and gates the candidate validation
report. The gate floors are AP >= 0.85, precision >= 0.70 and recall >= 0.90.
A passed gate is not a comparison against the champion or permission to promote.

The live drill completed request 418aa34f-fc6b-4228-9a26-f59be1879c26,
MLflow run 6354dedb1a8f4360b22553f9e147fa06. Its candidate passed the gate.
The deployed API artifact and zero challenger weight remained unchanged.
A repeated execution was refused, and drift resolution was delivered.

Training reused the approved train/validation splits. No new traffic labels
were collected, the test split was not opened by the worker, and effectiveness
against the shifted distribution was not established.

Evidence: reports/controlled_retraining_drill/ and reports/drift_request_drill.json.

## API latency

The instrumented Docker API benchmark used 100 warmup requests and 1,000
measured requests per endpoint, concurrency 1, batch size 1, and connection reuse.

| Endpoint | Median | p95 | p99 |
|---|---:|---:|---:|
| `/predict` | 11.57 ms | 25.56 ms | 33.70 ms |
| `/explain` | 13.92 ms | 24.95 ms | 31.92 ms |

All measured requests succeeded. Measurements include client JSON processing
and local HTTP. They exclude cold starts, concurrent load, browser rendering,
and remote networking.

The earlier run was faster. These separate runs do not isolate monitoring
overhead. Later concurrent measurements are documented below.

Evidence: `reports/api_latency_instrumented.json`.
The earlier report is preserved as `reports/api_latency.json`.

## Concurrent load results

Bounded closed-loop tests used 1, 4 and 8 clients with 1,000 measured requests
per full scenario. Direct API prediction and explanation runs had no measured
request failures. Latency increased under concurrency; the slowest eight-client
explanation request took approximately 4.90 seconds.

The gateway had an initial warmup HTTP 503 failure and a later four-client run
with 16 HTTP 503 failures among 1,000 attempts. Separate subsequent four-client
and eight-client runs completed without measured failures. Their cause remains
unconfirmed; diagnostic logging was not a functional fix.

These results do not establish sustained capacity or an SLA. Successful-response
latency excludes failures; the reports also retain all-attempt latency.

See docs/load_test_report.md and reports/load_test_summary.json.

## Tests

Run from the project directory:

    export PYSPARK_PYTHON="$PWD/.venv/bin/python"
    export PYSPARK_DRIVER_PYTHON="$PYSPARK_PYTHON"
    export SPARK_LOCAL_IP=127.0.0.1
    export PYSPARK_SUBMIT_ARGS="--driver-memory 1g pyspark-shell"
    PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v

The preserved integration run passed 110 tests. Its logs and execution
records are under reports/retraining_ci/. DVC reproduction used unchanged
cached stages; that execution did not retrain the baseline models.

Run the local CI wrapper to record tests and DVC command outcomes:

    .venv/bin/python scripts/run_ci.py

Later client diagnostic logging passed its nine focused client tests.
Frontend connection behavior was checked manually in the browser.
The preserved 110-test run predates those later changes.

Spark workers and the driver must use the same Python minor version.

## DVC pipeline

    export DVC_NO_ANALYTICS=1
    runtime/dvc-venv/bin/dvc dag
    runtime/dvc-venv/bin/dvc status
    runtime/dvc-venv/bin/dvc repro

The pipeline connects the raw PaySim pointer to Spark preparation and separate
logistic regression and XGBoost training stages, followed by the XGBoost
validation gate. Local CI and its execution history are separate from
automatic deployment; successful checks do not change serving artifacts.

DVC reproduction requires the raw data or its cached objects and the appropriate
Python environments. A Git clone alone does not contain datasets or models.
Local DVC tracking does not provide an off-machine backup.

## Data policy and limitations

Datasets, model binaries, runtime environments, and secrets are excluded from
ordinary Git history. Git tracks code, dependency records, reports, and DVC
metadata.

This project is a local demonstration using synthetic data. Real payment
deployment would require independent validation, calibrated operational
thresholds, security review, and evaluation under representative traffic.
