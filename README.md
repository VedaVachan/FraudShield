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
| Grafana | http://127.0.0.1:3010 |

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

FraudShield is built to run locally, so it has no TLS, authentication or rate limiting, and it should not be exposed to the internet as it is. The deployed model does not have a recorded training commit, which means I cannot trace that exact model back to the code that produced it (details are in reports/reproducibility_manifest.json). The canary gateway, alerting and resource-monitoring stacks are optional extras that run from separate compose files, so they are not part of the default setup. Rolling back the serving model is described in the documentation, but I only practised the registry rollback on a copy and never drilled the live serving rollback. I also did not run a 50-user load test.

## Screen recording

[Watch the video](https://drive.google.com/file/d/134BDrZ83VMFRfTwv-xqS-FnmFgm4SDHf/view?usp=drive_link)
(https://drive.google.com/file/d/134BDrZ83VMFRfTwv-xqS-FnmFgm4SDHf/view?usp=drive_link)
