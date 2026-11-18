# FraudShield

FraudShield scores a payment transaction for fraud risk and explains the score, using the
synthetic PaySim dataset, an XGBoost model with SHAP explanations and a small web page.
It is a local-only AIOps course project (CPU only, no cloud services) with Prometheus and
Grafana monitoring; scores are model outputs, not confirmed fraud.

## Quick start

Needs Docker with the Compose plugin. Written from the repository files and not verified
on a clean machine; training data and the full Python setup are only needed for the
pipeline and tests (see the [operator guide](docs/operator_guide.md)).

```bash
git clone https://github.com/VedaVachan/FraudShield.git
cd FraudShield
cp .env.example .env        
mkdir -p runtime && touch runtime/pipeline_runs.jsonl
docker compose up -d --build
```

| What | Address |
|---|---|
| Transaction page | http://127.0.0.1:8080 |
| API documentation | http://127.0.0.1:8000/docs |
| Pipeline page | http://127.0.0.1:8080/pipeline.html |
| MLflow | not a compose service; run `.venv/bin/mlflow ui --backend-store-uri sqlite:///runtime/mlflow/tracking.db` and open http://127.0.0.1:5000 (needs `runtime/mlflow/tracking.db`, which is not in Git) |
| Prometheus | http://127.0.0.1:9095 |
| Grafana (user `admin`, password from `.env`) | http://127.0.0.1:3010 |

## Run the tests

```bash
.venv/bin/python scripts/run_ci.py
```

It runs the unit tests and then `dvc repro`. The current run has 348 tests and exits 0
(run `a6784025-ae57-4ba8-8915-c1e939fba489`; `dvc repro` skipped every stage as unchanged,
so nothing was retrained).

## Project layout

- `backend/` FastAPI API, canary gateway, alert receiver, metrics, retraining queue and worker
- `frontend/` transaction page and pipeline page (static HTML)
- `scripts/` data preparation, training, evaluation, CI runner, promotion and analysis tools
- `tests/` the `unittest` suite
- `docs/` design, test, user and operator documents and screenshots
- `monitoring/` Prometheus rules and Grafana dashboards
- `artifacts/` model files (the deployed `xgboost_v1` is committed)

## Documentation

- [Architecture](docs/architecture.md), [high-level design](docs/high_level_design.md),
  [low-level design](docs/low_level_design.md)
- [Test plan](docs/test_plan.md), [test report](docs/test_report.md)
- [User manual](docs/user_manual.md), [operator guide](docs/operator_guide.md)
- [Retraining recovery](docs/retraining_recovery.md)
- [AI disclosure](AI_DISCLOSURE.md)

## Known limitations

- No TLS, authentication or rate limiting: the deployment is local-only.
- No 50-user load test was run.
- The deployed model has no recorded training commit (`reports/reproducibility_manifest.json`).
- The canary gateway, alert and resource-monitoring stacks are optional extra compose files.
- Serving rollback is a documented procedure only; it was never drilled.
