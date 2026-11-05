# FraudShield High-Level Design

## Problem and intended users

FraudShield helps a payment reviewer inspect a synthetic transaction through
a fraud score, threshold-based assessment and optional explanation.

It is a local educational application, not a real payment authorization system.

## Goals

- Validate and prepare data reproducibly.
- Compare a simple baseline with a nonlinear model.
- Separate model development from held-out evaluation.
- Serve validated predictions and explanations through REST.
- Monitor service behavior and input distribution changes.
- Maintain inspectable code, data and model provenance.

## Current scope

Implemented: single-transaction scoring, explanations, baseline training,
validation-only tuning, DVC preparation/training, original-model MLflow
registration, Docker serving, service metrics and input drift alerts.

Not yet implemented: batch scoring, pipeline management UI, automated traffic
splitting, automated deployment rollback and alert-triggered retraining.

## Design choices

| Choice | Rationale |
|---|---|
| Synthetic PaySim data | Reproducible experiments without real customer transactions |
| Chronological splits | Train on earlier transactions and evaluate later transactions |
| Three pre-transaction inputs | Avoid post-transaction balances and customer identifiers |
| Shared feature functions | Reduce training/serving inconsistency |
| Logistic regression | Establish a simpler comparison model |
| CPU XGBoost | Capture nonlinear interactions within VM resources |
| Validation-selected threshold | Make the decision policy explicit without test-set tuning |
| FastAPI and separate frontend | Separate inference and presentation through REST |
| Docker Compose | Package local services without cloud infrastructure |
| DVC | Track raw data and baseline pipeline dependencies and outputs |
| MLflow | Track and package original models with prediction-parity checks |
| Bucket-based drift | Bound memory and avoid retaining full transaction values |

The implementation uses pure functions for transformations and classes for
validated schemas and stateful components such as drift monitoring.

## Data design

The raw dataset contains 6,362,620 synthetic transactions.

| Split | Steps | Rows | Fraud rows |
|---|---|---:|---:|
| Training | 1-520 | 6,082,007 | 5,781 |
| Validation | 521-631 | 191,147 | 1,180 |
| Test | 632-743 | 89,466 | 1,252 |

Inference inputs are type, amount and oldbalanceOrg.
The label is isFraud; step is used for chronological splitting.

Identifiers, the legacy isFlaggedFraud rule and post-transaction balances are
not model inputs. Spark and online feature implementations have parity checks.

Synthetic fraud patterns limit generalization to real payment traffic.

## Model selection

Training uses class-weighted logistic regression and CPU XGBoost.
Thresholds maximize validation F1, choosing the higher threshold on exact ties.

Four Ray Tune configurations were evaluated on validation data.
The tuned candidate improved validation average precision slightly, but had
the same selected-threshold F1 as the baseline.

The DVC XGBoost baseline was selected and frozen before held-out evaluation.
Its held-out average precision was 0.992652 and F1 was 0.978108.

The currently deployed original registered model is a separate artifact.
Held-out results must not be attributed to it without corresponding evidence.

## Serving design

The API verifies its model checksum and threshold against a registry receipt
before becoming ready. It loads the model directly in a single API worker.

The browser calls REST endpoints and does not load model files.
Its API address is currently hardcoded and requires a configuration improvement.

Invalid inputs return a generic validation error.
The frontend handles unavailable services, timeouts and request failures.

## Monitoring design

Prometheus collects scoring requests, latency histograms, decisions and drift.
Grafana displays service and input drift information.

Drift uses total variation between training reference buckets and the last
500 successful prediction requests, publishing scores after 100 samples.
Invalid requests and explanations do not enter the drift window.

The 0.20 alert threshold is a demonstration policy, not a calibrated
production threshold. Input drift does not establish accuracy degradation.

## Reliability, privacy and security

Published ports bind to localhost. The API container uses a non-root user,
resource limits, a read-only filesystem and read-only artifact mounts.

Validation errors do not echo submitted transaction values.
The drift detector retains bucket indices rather than full transactions.

Checksum validation detects artifact differences; it does not provide
encryption or prove that an artifact comes from a trusted publisher.
Only trusted joblib artifacts should be loaded.

Authentication, TLS, encrypted storage and comprehensive structured audit
logging are not established by the current verified implementation.

## Verification and limitations

The latest complete suite passed 19 automated tests.
Separate live checks verified container serving, explanations, metrics,
drift alert firing/recovery and a test-client routing rehearsal.

The instrumented sequential HTTP benchmark completed 1,000 measured requests
per endpoint without failures. Prediction p95 was 25.56 ms; explanation p95
was 24.95 ms. These results do not establish concurrent-load capacity.

Current inference and drift state use one process.
Horizontal scaling would require deliberate metrics and drift-state design.

## Next engineering work

- Add real pipeline run records and automated quality gates.
- Audit Git, data-version and MLflow provenance for newer experiments.
- Add MLproject entry points and verify MLflow serving.
- Build pipeline visualization and configurable frontend connectivity.
- Implement and test controlled canary routing and automatic rollback.
- Connect alert delivery and controlled retraining.
- Complete test documentation, user manual and representative load testing.
