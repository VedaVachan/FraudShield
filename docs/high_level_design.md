# FraudShield High-Level Design

## Problem and intended users

FraudShield helps a reviewer inspect a synthetic payment through a model score,
threshold-based assessment and optional explanation. A project operator can inspect
pipeline records, monitor serving and rehearse controlled candidate operations.
This is a local educational application, not real payment authorization.

## Goals and scope

- Validate data and reproduce baseline preparation/training.
- Separate model development from held-out evaluation.
- Serve validated predictions and explanations through REST.
- Monitor service behavior, input changes and routing failures.
- Preserve inspectable code, data, model and execution provenance.
- Demonstrate drift notification, request queueing and controlled candidate training.

Implemented components include a DVC validation gate, local CI history, historical
and newly tracked MLflow runs, verified MLflow HTTP serving, a read-only pipeline
console, configurable frontend connectivity, deterministic canary routing,
authenticated weight changes, fallback, automatic challenger traffic removal,
local alert delivery and operator-started candidate training with validation.

Batch scoring, feedback labels, UI pipeline-launch controls, continuously supervised
retraining, automatic candidate promotion and artifact rollback are not implemented.

## Design choices

| Choice | Rationale |
|---|---|
| Synthetic PaySim data | Reproducible experiments without real customer transactions |
| Chronological splits | Develop on earlier transactions and evaluate later ones |
| Three pre-transaction inputs | Exclude post-payment balances and customer identifiers |
| Shared feature functions | Reduce training/serving inconsistency |
| Logistic regression and CPU XGBoost | Compare a simpler model with nonlinear interactions within VM limits |
| Validation-selected threshold | Make decision policy explicit without test-set tuning |
| Separate frontend and FastAPI | Separate presentation from inference through REST |
| Docker Compose | Package local services without cloud infrastructure |
| DVC and MLflow | Track pipeline dependencies and distinct model-run histories |
| Bucket-based total variation | Bound drift memory and avoid retaining complete transaction values |
| Separate candidate artifacts | Preserve the champion while validating controlled operations |
| SQLite queue and process lock | Deduplicate requests and prevent overlapping local worker execution |

## Data design

| Split | Steps | Rows | Fraud rows |
|---|---|---:|---:|
| Train | 1–520 | 6,082,007 | 5,781 |
| Validation | 521–631 | 191,147 | 1,180 |
| Test | 632–743 | 89,466 | 1,252 |

Inputs are `type`, `amount` and `oldbalanceOrg`. `isFraud` is the target and `step`
supports chronological splitting. Identifiers, `isFlaggedFraud` and post-transaction
balances are excluded from model inputs. Python/Spark parity is checked.
Synthetic fraud patterns limit generalization to real payment traffic.

## Model selection and provenance

Thresholds maximize validation F1, choosing the higher threshold on exact ties.
Validation supports early stopping and four Ray Tune configurations. The tuned
candidate improved average precision slightly, but its selected-threshold F1
matched the DVC baseline. The DVC XGBoost baseline was frozen before held-out
evaluation; held-out AP was 0.992652 and F1 was 0.978108.

The deployed original registered model is a separate artifact. Its hash and
threshold are verified against a receipt. Held-out results are not transferred to
it or to tracked candidates. Historical MLflow imports explicitly do not establish
an original training-code commit. New tracked training records its actual code/data
provenance and verifies logged metrics and downloaded artifact bytes.

## Serving and presentation

The direct API loads the original bundle in one worker and returns model identity.
The browser calls it through REST. Its API-origin setting accepts local HTTP(S)
origins, clears old assessments on valid changes and persists only the origin.
Browser storage failure permits a page-local setting. Backend validation remains
authoritative; requests have a 20-second browser timeout.

The pipeline console is read-only. It separates configured topology, actual command
execution records and historical evidence snapshots. A DVC cache check is not shown
as newly measured training performance, and saved MLflow receipts are not live queries.

The separate gateway calls champion/challenger MLflow HTTP servers. Weight zero
selects the champion. Transaction-ID hashing controls allocation at positive weight.
Administrative weight changes require the ignored local API key. Response checks
validate consistency, not upstream artifact identity.

## Monitoring and recovery

Prometheus collects API and gateway metrics; Grafana displays service, drift and
routing information. The direct API drift window holds 500 successful predictions
and publishes after 100 samples. Explanations and invalid requests do not enter it.
The alert uses total variation above 0.20 for 30 seconds, with recent prediction
activity and API scrape-health conditions. This is a demonstration policy, not a
calibrated production threshold. Input drift does not prove accuracy degradation.

An eligible challenger serving failure triggers one champion fallback attempt and
removes subsequent challenger traffic. This does not revert model artifacts,
promote a model or cancel in-flight requests. Gateway routing state is process-local
and starts at zero. Restarting a challenger does not re-enable its traffic.

## Alert delivery and controlled retraining

Alertmanager delivers notifications to a local receiver. A host scanner accepts
recent firing input-drift notifications and persists queue requests. Delivery-ID
deduplication, one active request per API instance and a one-hour creation-time
cooldown limit repeats. One global worker lock prevents overlapping worker processes.

An operator starts a queued request. The worker trains a separate MLflow candidate
on approved train/validation splits, checks its receipt and applies the saved-report
gate. Gate floors are AP 0.85, precision 0.70 and recall 0.90. A pass is neither a
champion-comparison test nor automatic promotion. Interrupted workers may leave a
request running for investigation; no automatic resume is implemented.

The verified drill delivered an actual drift notification, queued a request,
completed candidate training and its gate, preserved the deployed API model and
zero challenger weight, delivered resolution and refused replay of the completed
request. It did not establish effectiveness against shifted traffic or new labels.

## Reliability, privacy and security

Published ports bind to localhost. Serving uses resource limits, non-root users,
read-only filesystems and read-only artifact mounts as configured. Model-server
and gateway container UIDs match the local operator where required for bind mounts.

The drift detector retains bucket indices. The receiver persists approved labels,
not full raw notifications. Client diagnostic warnings omit transaction values and
exception-message contents. Gateway admin authentication does not establish
authentication for the direct API, frontend or read-only console. TLS, encrypted
storage and comprehensive audit logging remain outside verified scope.

Checksums detect differing bytes; they do not encrypt artifacts or prove publisher
trust. Only trusted joblib bundles should be loaded.

## Verification and remaining work

The preserved integration execution passed 110 tests. Later diagnostic logging
passed nine focused client tests; the frontend was manually exercised. The preserved
full execution predates those later changes. See [test_report.md](test_report.md).

Concurrent tests recorded successful direct API scenarios, gateway warmup and load
503s, later successful gateway scenarios, and substantial tail-latency variation.
The failure cause is not established and logging is not a functional fix. See
[load_test_report.md](load_test_report.md); sustained capacity and SLA compliance
have not been demonstrated.

Remaining work includes reconciling the final submission with authoritative course
requirements, confirming the unrecorded frontend reload-persistence check, and
demonstrating fresh-machine restoration before claiming portable recovery. Host
CPU/memory/disk dashboards and per-component instrumentation require a separate
requirements review; current verified panels principally cover API and gateway behavior.
