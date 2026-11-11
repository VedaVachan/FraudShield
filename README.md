# FraudShield

FraudShield scores a payment transaction for fraud risk and explains the score. It uses
the synthetic PaySim dataset, an XGBoost model with SHAP explanations, a small web page,
a canary gateway for trying a second model, and Prometheus/Grafana monitoring. Everything
runs locally on one machine (CPU only, no cloud services). It is a course project, not a
real payment system: scores are model outputs, not confirmed fraud.

## Prerequisites

- Ubuntu VM (developed on Ubuntu 24.04 with 6 CPUs and about 8 GB RAM).
- Docker with the Compose plugin (developed with Docker 29.9.3 and Compose v5.7.0).
- Python 3.11.17 (pinned in `conda-project.yaml`; the commands below use conda).
- Java for Spark (the VM has OpenJDK 17.0.20.1; other versions are not verified).

## Fresh clone setup

Commands marked "not verified" were written from the repository files but not run on a
clean machine.

1. Clone (replace the placeholder with your repository address):

   ```bash
   git clone <repository-url> fraudshield
   cd fraudshield
   ```

2. Create the Python environments. `conda-project.yaml` asks for Python 3.11.17, pip 24.0
   and `requirements-project.lock.txt`. `scripts/run_ci.py` and `dvc.yaml` expect the
   interpreter at `.venv` and DVC at `runtime/dvc-venv` (not verified):

   ```bash
   conda create -n fraudshield python=3.11.17 pip=24.0
   conda activate fraudshield
   python -m venv .venv
   .venv/bin/pip install -r requirements-project.lock.txt
   .venv/bin/python -m venv runtime/dvc-venv
   runtime/dvc-venv/bin/pip install -r requirements-dvc.lock.txt
   ```

3. Get the data. PaySim is not in Git. Download "Synthetic Financial Datasets For Fraud
   Detection" (ealaxi) from https://www.kaggle.com/datasets/ealaxi/paysim1 (a Kaggle
   account is needed), then place the CSV at `data/raw/paysim.csv`:

   ```bash
   mkdir -p data/raw
   mv ~/Downloads/PS_20174392719_1491204439457_log.csv data/raw/paysim.csv
   md5sum data/raw/paysim.csv
   ```

   The `md5sum` must print `e92a5f7447f43712f1dca473d0b0fa85`, the hash in `paysim.dvc`.
   The SHA-256 in `docs/dataset.md` can be checked with `sha256sum`.
   `dvc status` in a fresh clone reports the CSV as "not in cache" even when it is
   present (observed), so use `md5sum` for the check.

4. DVC remote. `.dvc/config` defines a remote named `localstore`: the folder
   `../fraudshield-dvc-remote` next to the repository, outside Git. It is empty in a
   fresh clone. Only someone who owns a filled copy of that folder can `dvc pull` (not
   verified; `dvc push` and `dvc pull` were not run). The owner fills it with:

   ```bash
   export DVC_NO_ANALYTICS=1
   runtime/dvc-venv/bin/dvc status -c
   runtime/dvc-venv/bin/dvc push
   ```

   Anyone else restores the outputs by reproducing the pipeline (next step). The remote
   holds no credentials.

5. Reproduce the pipeline (Spark preparation, both trainings, validation gate). This
   retrains the models and takes a while (not verified from a fresh clone):

   ```bash
   export DVC_NO_ANALYTICS=1
   runtime/dvc-venv/bin/dvc repro
   ```

6. Create the settings file and the file the API container mounts, then start the stack.
   `artifacts/xgboost_v1/model.joblib` (the deployed model) is committed to Git; the
   other models (`artifacts/xgboost_dvc_v1`, `artifacts/logreg_dvc_v1`) are produced by
   `dvc repro`:

   ```bash
   cp .env.example .env
   # edit .env and replace the placeholder password (never commit .env)
   mkdir -p runtime && touch runtime/pipeline_runs.jsonl
   docker compose build api frontend
   docker compose pull prometheus grafana
   docker compose up -d --no-build --wait --wait-timeout 180
   docker compose ps
   ```

## Run the tests

The supported way runs the unit tests and then `dvc repro`, and records both in
`runtime/pipeline_runs.jsonl`:

```bash
.venv/bin/python scripts/run_ci.py
```

Running the tests alone needs the Spark settings from `scripts/run_ci.py`:

```bash
export PYSPARK_PYTHON="$PWD/.venv/bin/python"
export PYSPARK_DRIVER_PYTHON="$PYSPARK_PYTHON"
export SPARK_LOCAL_IP=127.0.0.1
export PYSPARK_SUBMIT_ARGS="--driver-memory 1g pyspark-shell"
PYTHONPATH=src:. .venv/bin/python -m unittest discover -s tests -v
```

Without the Spark variables the tests in `tests/test_spark_features.py` fail with a Python
version mismatch (observed). Prefer `scripts/run_ci.py`.

## Open the services

| What | Address |
|---|---|
| Transaction page (frontend) | http://127.0.0.1:8080 |
| Pipeline page | http://127.0.0.1:8080/pipeline.html |
| API documentation / health | http://127.0.0.1:8000/docs, http://127.0.0.1:8000/health |
| Prometheus | http://127.0.0.1:9095 |
| Grafana (user `admin`, password from `.env`) | http://127.0.0.1:3010 |
| Canary gateway state (optional stack) | http://127.0.0.1:8020/routing |

MLflow has no service in the compose files. To browse the experiment history of a
machine that has `runtime/mlflow/tracking.db` (it is not in Git, so a fresh clone has
none), run the MLflow UI and open http://127.0.0.1:5000:

```bash
.venv/bin/mlflow ui --backend-store-uri sqlite:///runtime/mlflow/tracking.db --host 127.0.0.1 --port 5000
```

Optional stacks (start the main stack first). The canary stack also needs the MLflow
packages under `runtime/mlflow-serving`, built by `scripts/package_identity_models.py`
(it needs `artifacts/xgboost_tuned_v1/model.joblib`, which is not committed), and a
`runtime/gateway-admin.key` file you create yourself (at least 32 characters; never
commit it):

```bash
export GATEWAY_UID="$(id -u)"
export GATEWAY_GID="$(id -g)"
docker compose -f compose.canary.yaml --profile canary up -d --no-build --wait --wait-timeout 180

mkdir -p runtime/alerts
export LOCAL_UID="$(id -u)"
export LOCAL_GID="$(id -g)"
docker compose -f compose.alerts.yaml up -d --no-build --wait --wait-timeout 180

docker compose -f compose.resources.yaml up -d
```

More detail, troubleshooting and shutdown: [docs/operator_guide.md](docs/operator_guide.md).

## Retraining workflow

What is automatic and what is not:

| Step | Automatic? |
|---|---|
| Drift detection (Prometheus rule) and queueing a request (alert receiver) | **Automatic** |
| Executing a queued request (candidate training and validation gate) | **Manual by default; automatic only with `--watch`** |
| Registering, promoting or deploying a model | **Never automatic.** Promotion is always a manual, gated step (`scripts/promote_model.py`) |

Execution is candidate-only either way: it never registers, promotes or deploys a model.

```bash
PYTHONPATH=src:. .venv/bin/python scripts/enqueue_drift_requests.py
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --list
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --request-id REQUEST_ID
```

Opt-in automatic execution (off unless you start it; stops cleanly on Ctrl+C or SIGTERM;
the interval must be positive, default 30 seconds):

```bash
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --watch --interval 60
```

`--watch` polls the queue and runs each queued request through the same path as
`--request-id`: the same worker lock, the same committed-source check and the same
candidate-only training and gate. It was tested with fake training commands only; it has
not been run against real training.

If a worker was killed after writing its result but before updating the queue, inspect
and then explicitly recover (see [docs/retraining_recovery.md](docs/retraining_recovery.md)):

```bash
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --inspect-recovery REQUEST_ID
PYTHONPATH=src:. .venv/bin/python scripts/run_retraining_worker.py --recover REQUEST_ID --confirm-result-sha256 HASH --reason "why"
```

The worker, `--watch` and `--recover` only run when the worker sources are committed and unchanged.

## Other tools

```bash
# Registry aliases: dry run by default; --apply writes; --rollback restores the previous one
.venv/bin/python scripts/promote_model.py --candidate-version 1
.venv/bin/python scripts/promote_model.py --rollback

# Log gain-based feature importance of the deployed model as a new MLflow run
PYTHONPATH=src:. .venv/bin/python scripts/log_feature_importance.py
```

`promote_model.py` moves MLflow registry aliases only; the API and gateway do not read
them. It was tested on a throwaway database and not run against the real registry.
`log_feature_importance.py` refuses to run if the model hash differs.

## Current results

- Tests: 265 tests pass in local CI run `113fe711-1fff-4c3c-b516-330d92a54548` (exit 0);
  `dvc repro` reported the pipeline up to date.
- Coverage: 93% of `backend/` and `src/fraudshield/` (1,275 statements, lowest file 82%),
  measured at 217 tests; see [reports/coverage_summary.txt](reports/coverage_summary.txt).
  The 48 tests added since (265 minus 217) exercise only `scripts/`, the dashboard JSON and
  the frontend pages, so this figure was not re-measured. `scripts/` is not measured.
- Model gate floors on validation: average precision 0.85, precision 0.70, recall 0.90.
  The frozen DVC baseline measured 0.9885 / 0.9284 / 1.0
  (`reports/xgboost_validation_gate.json`).
- Held-out test split, frozen DVC baseline only: average precision 0.9927, precision
  0.9579, recall 0.9992 (`reports/xgboost_heldout_evaluation.json`). The deployed model
  (`artifacts/xgboost_v1`) has no held-out result.
- Latency, direct API `/predict`, 1,000 requests per run, success p95
  (`reports/concurrent_api_predict.json`): 19.94 ms with 1 client, 96.15 ms with 4,
  252.59 ms with 8. No 50-user run was made. One gateway run had 16 failures in 1,000
  requests at 4 clients (`reports/concurrent_gateway_predict_run2.json`).

## Repository layout

| Folder or file | Purpose |
|---|---|
| `backend/` | FastAPI API, canary gateway, alert receiver, metrics, retraining queue and worker |
| `src/fraudshield/` | Shared features, SHAP explainer, MLflow model wrapper, Spark features |
| `frontend/` | Static transaction page and pipeline page |
| `scripts/` | Data preparation, training, evaluation, CI runner, retraining worker, promotion and analysis tools |
| `tests/` | Unit and component tests (`unittest`) |
| `docs/` | Design documents, manuals, test plan and report |
| `reports/` | Recorded evidence: validation, benchmarks, drills, CI logs, coverage |
| `monitoring/` | Prometheus, Alertmanager and Grafana configuration and dashboards |
| `docker/` | Dockerfiles for the model server, gateway and alert receiver |
| `compose*.yaml`, `Dockerfile` | Main, canary, alert and resource stacks; API image |
| `dvc.yaml`, `dvc.lock`, `paysim.dvc`, `.dvc/` | DVC pipeline, data pointer and remote |
| `requirements*.txt`, `conda-project.yaml`, `MLproject` | Pinned dependencies and environment entry points |
| `artifacts/`, `data/`, `runtime/` | Models, datasets and local state; not in Git (except `artifacts/xgboost_v1/model.joblib`) |

## Documentation

| File | Contents |
|---|---|
| [docs/user_manual.md](docs/user_manual.md) | Plain-language guide to using the web page |
| [docs/operator_guide.md](docs/operator_guide.md) | Running, monitoring, logging, rollback and security notes |
| [docs/architecture.md](docs/architecture.md) | Architecture diagrams and block descriptions |
| [docs/high_level_design.md](docs/high_level_design.md) | Goals, design choices, monitoring coverage, limits |
| [docs/low_level_design.md](docs/low_level_design.md) | Endpoint contracts with example requests and responses |
| [docs/openapi.json](docs/openapi.json) | OpenAPI description of the direct API |
| [docs/test_plan.md](docs/test_plan.md) | Test cases and acceptance criteria |
| [docs/test_report.md](docs/test_report.md) | Recorded results and acceptance results |
| [docs/load_test_report.md](docs/load_test_report.md) | Concurrent load measurements |
| [docs/retraining_recovery.md](docs/retraining_recovery.md) | Recovering an interrupted retraining request |
| [docs/dataset.md](docs/dataset.md) | PaySim source, hashes and validation |
| [AI_DISCLOSURE.md](AI_DISCLOSURE.md) | Use of AI tools in this project |

## Known limitations

- No automatic model promotion or rollback. The registry-alias rollback was drilled on a
  copy of the registry (`scripts/rollback_drill.py`); the serving rollback (model file and
  API restart) is a documented procedure that was not drilled. The canary gateway only
  removes challenger traffic.
- No TLS or encryption at rest, no authentication on the direct API or the web page, and
  no rate limiting: the deployment is local-only.
- No feedback endpoint and no batch scoring.
- The DVC remote is a local folder, so a fresh clone cannot download the data or models.
- No fresh-clone reproduction and no 50-user load test has been recorded.
- Input drift is a distribution signal, not measured accuracy loss.
