# FraudShield

A local, CPU-based fraud scoring and explanation service for digital payments,
using the synthetic PaySim dataset.

## What is implemented

- Chronological training, validation, and test splits.
- Logistic regression and XGBoost pipelines.
- Spark validation and Parquet feature preparation.
- DVC stages for Spark preparation and model training.
- Validation-only Ray Tune experiments.
- MLflow model registration and checksum-verified API startup.
- FastAPI prediction, explanation, health, and Prometheus metrics endpoints.
- Local frontend and Docker Compose deployment.
- Grafana service and input drift dashboards.
- Input drift alert firing and recovery verification.
- A separate candidate-serving and test-client rollback rehearsal.

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
| API documentation | http://127.0.0.1:8000/docs |
| API health | http://127.0.0.1:8000/health |
| API metrics | http://127.0.0.1:8000/metrics |
| Prometheus | http://127.0.0.1:9095 |
| Grafana | http://127.0.0.1:3010 |

Grafana dashboard UID: `fraudshield-overview`.
Grafana uses `http://prometheus:9090` inside the Compose network.

Grafana stores credentials in its persistent volume. Changing the initial
admin password environment variable does not reset an existing account.

## API

- `POST /predict`: fraud score, threshold, decision, and model identity.
- `POST /explain`: prediction plus feature contributions in raw model log-odds.
- `GET /health`: readiness and loaded model checksum.
- `GET /metrics`: aggregate service and input drift metrics.

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
No Alertmanager notification receiver is configured.

## Local candidate and rollback drill

The tuned candidate was served separately on localhost port 8001.
Five synthetic requests were compared with v1, with zero decision disagreements.
Directing the test client back to v1 reproduced its scores and decisions.
The candidate container was then removed and v1 readiness verified.

This was a test-client routing rehearsal. The live service remained on v1;
there was no production traffic split or candidate promotion.

Evidence: `reports/xgboost_canary_drill.json`.

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
overhead or establish concurrent-load performance.

Evidence: `reports/api_latency_instrumented.json`.
The earlier report is preserved as `reports/api_latency.json`.

## Tests

Run from the project directory:

    export PYSPARK_PYTHON="$PWD/.venv/bin/python"
    export PYSPARK_DRIVER_PYTHON="$PYSPARK_PYTHON"
    export SPARK_LOCAL_IP=127.0.0.1
    export PYSPARK_SUBMIT_ARGS="--driver-memory 1g pyspark-shell"
    PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v

The latest full run passed all 19 tests. Spark workers and the driver must
use the same Python minor version.

## DVC pipeline

    export DVC_NO_ANALYTICS=1
    runtime/dvc-venv/bin/dvc dag
    runtime/dvc-venv/bin/dvc status
    runtime/dvc-venv/bin/dvc repro

The pipeline connects the raw PaySim pointer to Spark preparation and separate
logistic regression and XGBoost training stages.

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
