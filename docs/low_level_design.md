# FraudShield Low-Level Design

## API address and input

Base URL: http://127.0.0.1:8000
Scoring requests use Content-Type: application/json.

POST /predict and POST /explain require exactly these fields:

| Field | Type | Constraint |
|---|---|---|
| type | string | CASH_IN, CASH_OUT, DEBIT, PAYMENT or TRANSFER |
| amount | number | Finite and nonnegative |
| oldbalanceOrg | number | Finite and nonnegative |

Additional fields are forbidden. Amount and balance use the same monetary unit.

Example request:
{"type":"TRANSFER","amount":100000,"oldbalanceOrg":100000}

## Endpoint catalogue

| Method | Path | Purpose | Expected status |
|---|---|---|---|
| POST | /predict | Score one transaction | 200; invalid input 422 |
| POST | /explain | Score and explain one transaction | 200; invalid input 422 |
| GET | /health | Readiness and model identity | 200 ready; 503 not ready |
| GET | /metrics | Prometheus text exposition | 200 |
| GET | /docs | Interactive API documentation | 200 |
| GET | /redoc | Alternative API documentation | 200 |
| GET | /openapi.json | Generated OpenAPI schema | 200 |

Unexpected inference failures can produce HTTP 500.
A startup failure can prevent the server from accepting requests at all.

Batch, feedback, pipeline-management and deployment-admin endpoints
are planned extensions and are not part of this current contract.

## Prediction response

POST /predict returns:

| Field | Type | Meaning |
|---|---|---|
| score | number | Finite model score in [0, 1] |
| threshold | number | Loaded decision threshold |
| predicted_fraud | boolean | score >= threshold |
| model_name | string | Model name from the verified receipt |
| model_version | string | Model version from the verified receipt |

The score is not represented as a calibrated fraud probability.
The decision is a model assessment, not a confirmed fraud label.

## Explanation response

POST /explain returns all prediction fields, plus:

| Field | Type | Meaning |
|---|---|---|
| base_value | number | Explanation baseline in raw model log-odds |
| raw_margin | number | Explained model margin |
| contribution_units | string | raw model log-odds |
| contributions | array | Feature contributions |

Each contribution contains feature (string) and shap_log_odds (number).
Contributions are sorted by descending absolute magnitude.

The base value plus contributions approximately reconstructs the raw margin.
These contributions explain model behavior, not causal effects.

## Health and errors

Ready health response contains:
status="ready", model_name, model_version and model_sha256.

Not-ready response:
HTTP 503 with {"status":"not_ready"}.

Invalid scoring input:
HTTP 422 with {"detail":"Invalid transaction input."}.

Validation errors do not echo transaction values or arbitrary field names.
Clients must not assume a structured JSON contract for unexpected HTTP 500 errors.

## Startup and shutdown

1. Read reports/mlflow_registry.json and its XGBoost receipt.
2. Hash artifacts/xgboost_v1/model.joblib.
3. Reject a checksum mismatch.
4. Load the trusted bundle.
5. Validate that its threshold is finite, in [0, 1], and matches the receipt.
6. Initialize the pipeline, explainer, identity and drift monitor.
7. Set readiness to true.
8. On shutdown, clear readiness and inference references.

The API directly loads the bundle; it currently does not call a separate
MLflow model-serving process.

## Features

Input columns: type, amount, oldbalanceOrg.

Output order:
log_amount, log_sender_balance, amount_balance_fraction,
amount_exceeds_balance, type_CASH_IN, type_CASH_OUT, type_DEBIT,
type_PAYMENT, type_TRANSFER.

The Python feature builder rejects extra or missing columns and invalid values.
Spark feature parity is checked separately.

## Frontend behavior

The frontend currently uses http://127.0.0.1:8000 as a hardcoded base URL.
The browser sends requests directly to the API.

The page checks health when loaded.
Users choose Check transaction or Check + explain.
Form controls are disabled while a scoring request runs.
Requests have a 20-second browser timeout.
Changing inputs clears the previous assessment.

The frontend uses browser validation and finite-number checks.
Backend validation remains authoritative.
Errors are displayed as text, with a further health check after scoring failures.

## CORS

Allowed origins:
- http://127.0.0.1:8080
- http://localhost:8080

Allowed methods: GET and POST.
Allowed request header: Content-Type.
Credentials are disabled.

## Metrics and drift semantics

Scoring metrics distinguish /predict and /explain.
Request counts include invalid inputs and server errors.
Successful decision counts are not ground-truth fraud counts.

Drift is updated from successful completed /predict requests.
Explanations and invalid requests do not enter the drift window.

The window holds 500 observations and requires 100 before publishing scores.
Scores compare type, amount and sender-balance bucket distributions against
the training reference using total variation.

The service uses one worker. Drift state is in memory and resets on restart.

## DVC stage contracts

| Stage | Principal inputs | Output |
|---|---|---|
| spark_prepare | Raw CSV, preparation/feature code, dependency records | data/processed/paysim_spark_dvc_v1 |
| logreg_train | Prepared data, training/feature code, dependency record | artifacts/logreg_dvc_v1 |
| xgboost_train | Prepared data, training/helper/feature code, dependency record | artifacts/xgboost_dvc_v1 |

The exact dependency declarations are in dvc.yaml.
Content identities of the recorded run are in dvc.lock.
Tuning, held-out evaluation and registration are currently separate scripts.

## Source mapping

| Responsibility | Source |
|---|---|
| Routes, schemas and lifecycle | backend/app.py |
| Scoring middleware and metrics | backend/metrics.py |
| Rolling drift | backend/drift.py |
| Shared features | src/fraudshield/features.py |
| Explanation engine | src/fraudshield/explanations.py |
| MLflow wrapper | src/fraudshield/mlflow_model.py |
| Browser UI | frontend/index.html |
| Deployment | compose.yaml |

## Configuration and specification follow-up

The current model paths, CORS origins and browser API URL are defined in code.
Compose defines ports, resource limits, health checks and artifact mounts.
Its Grafana setup requires GRAFANA_ADMIN_PASSWORD from the local environment.

A complete configuration reference, detailed metric catalogue and exported
OpenAPI appendix will be updated as the planned management features are built.
