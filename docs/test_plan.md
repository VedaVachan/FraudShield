# FraudShield Test Plan

## Purpose and scope

Verify data/feature consistency, direct API behavior, pipeline evidence, model gates,
canary recovery, notification delivery, controlled candidate training and local
performance. This plan consolidates the implemented project's checks; it is not a
claim that every requirement in the course rubric has been fulfilled.

## Environment and entry conditions

- Existing Ubuntu VM: 6 vCPUs, approximately 8 GiB RAM, CPU inference/training.
- Project `.venv` and separate DVC/serving environments with recorded dependencies.
- Trusted model artifacts, registry receipt and prepared data available.
- Docker access, main network, existing images and optional stack mounts available.
- Secrets remain in ignored local files; test evidence must not contain key values.
- Use temporary files/mocks for failure unit tests; do not corrupt deployed artifacts.
- Freeze model selection before held-out evaluation; never retune on test outcomes.

## Acceptance criteria

These criteria were written on 2026-11-10, after the benchmarks, drills and tests
already existed. They record what the repository can show; they are not predictions
made beforehand. Results are in the "Acceptance results" table of
[test_report.md](test_report.md).

| ID | Criterion | Basis |
|---|---|---|
| AC-01 | All automated tests pass | Local CI exits 0 with no failures or errors |
| AC-02 | Unit-test coverage of `backend/` and `src/fraudshield/` is at least 80% | Threshold 80%, `coverage` with `unittest` |
| AC-03 | The frozen DVC baseline passes the validation gate floors | Validation average precision >= 0.85, precision >= 0.70, recall >= 0.90 |
| AC-04 | The frozen DVC baseline meets the same floors on the held-out test split | Same floors; test split opened once after selection was frozen |
| AC-05 | The deployed champion and the logistic-regression baseline have held-out results | A recorded held-out evaluation of both artifacts |
| AC-06 | Single-client `/predict` p95 latency is under 50 ms | Threshold 50 ms, closed-loop, 1,000 requests |
| AC-07 | p95 latency at 50 concurrent users is under 50 ms | A recorded 50-client run |
| AC-08 | Each recorded benchmark scenario has fewer than 0.5% failed requests | Per scenario, counted from the report files |
| AC-09 | A failing challenger is removed from traffic automatically after one champion fallback | One recorded drill |
| AC-10 | `dvc repro` reports the pipeline up to date | The `dvc_reproduction` step of local CI |
| AC-11 | Reproduction from a fresh clone gives the same metrics | A recorded fresh-clone run |
| AC-12 | Retraining is candidate-only and never promotes or deploys a model | Drill records and tests |

## Automated test cases

The suite has **239 `unittest` tests in 31 files**. They run in local CI
(`scripts/run_ci.py`) and, since 2026-11-10, also as the DVC stage `unit_tests`, which
records its result in `reports/unit_tests_result.json`. Counts were taken with the
unittest loader. Inputs are test fixtures only: temporary files and databases, mocks,
FastAPI's `TestClient`, and (for A02 and A16) the real deployed model and Spark.

| ID | File (in `tests/`) | Tests | Input | Expected result |
|---|---|---:|---|---|
| A01 | `test_alert_receiver.py` | 8 | POST `/alerts` with firing and resolved notifications, extra fields, bad status, oversized body, unwritable history | Firing/resolved recorded; fields filtered; bad or large input rejected; storage failure gives 503 |
| A02 | `test_api.py` | 6 | Real deployed model; GET `/health`; POST `/predict` and `/explain` with valid and invalid bodies | Ready status; pipeline parity; explanation sums to the raw margin; invalid input gives 422 |
| A03 | `test_canary.py` | 10 | Weights 0 to 100, repeated transaction IDs, failing challenger | Deterministic allocation, valid weights only, one fallback, no retry loop |
| A04 | `test_canary_rollback.py` | 7 | Challenger failure with removal on and off; stale failure after a weight change | Eligible failures remove traffic; stale failures keep a newer configuration |
| A05 | `test_drift_scanner.py` | 6 | Notification history with firing, resolved, synthetic, malformed and stale records | Bounded history, replay protection, bad records skipped |
| A06 | `test_features.py` | 6 | Valid and invalid transaction tables (extra columns, negative or non-finite numbers, unknown type) | Correct order and values; invalid input rejected; input not modified |
| A07 | `test_gateway.py` | 12 | POST `/predict` and `/admin/canary` with and without the key; mock upstreams | Authentication enforced; routing correct; generic 503 on scoring failure |
| A08 | `test_gateway_metrics.py` | 6 | Gateway requests including fallback and removal | Request, result and latency counts; fallback and removal visible; registry isolated |
| A09 | `test_metrics.py` | 3 | Valid, invalid and failing API requests | Invalid input is not a decision; server failure counted; endpoints separate |
| A10 | `test_model_client.py` | 9 | Mock upstream: valid, malformed, oversized, wrong-threshold and unreachable responses | Valid response accepted; the others raise the model-service error |
| A11 | `test_model_gate.py` | 6 | Validation reports and model files: valid, changed model, weak, missing or test-set metrics | Valid report accepted; the others rejected |
| A12 | `test_pipeline.py` | 6 | History files: missing, partial, oversized, invalid lines | Bounded newest-first history; invalid records counted; values sanitized |
| A13 | `test_pipeline_summary.py` | 3 | Summary file present, missing, invalid | Snapshot served; missing or invalid gives 503 |
| A14 | `test_retraining_queue.py` | 9 | Notifications: repeated delivery IDs, cooldown, second request for one scope, other features | Duplicates and cooldown enforced; one active request; scope and feature filtered |
| A15 | `test_retraining_worker.py` | 9 | Mock training and gate runners: success, rejection, failure, timeout, bad receipt, held lock, replay | Correct terminal state; identity checks; replay refused |
| A16 | `test_spark_features.py` | 4 | Spark and Python features on edge values; invalid types and numbers | Parity; invalid input rejected |
| A17 | `test_gateway_identity.py` | 2 | Wrong model identity from the champion or challenger | Generic 503 for the champion; challenger removed with fallback |
| A18 | `test_gateway_server_coverage.py` | 10 | Registry and key files, environment values, invalid settings | Correct wiring and defaults; key enforced; invalid configuration refused |
| A19 | `test_mlflow_identity.py` | 3 | Model bundle with matching and mismatching SHA-256; file replaced after check | Mismatch refused; verified bytes loaded; predictions unchanged |
| A20 | `test_model_client_identity.py` | 6 | Upstream responses with matching, missing, wrong and malformed model hash | Only a matching identity is returned; legacy mode unchanged |
| A21 | `test_model_client_transport.py` | 2 | Bad status line and incomplete body from the upstream | Wrapped in the model-service error; response closed |
| A22 | `test_process_runner.py` | 4 | Dummy subprocesses: exit code, timeout, SIGTERM, killed worker | Output captured; children stopped; lock kept after a killed worker |
| A23 | `test_result_storage.py` | 9 | Result publication with injected sync failures and kills | Complete or no result file; never overwritten; failed publication blocks replay |
| A24 | `test_retraining_recovery.py` | 31 | Interrupted requests with valid, missing and inconsistent evidence | Read-only inspection; recovery only on matching evidence, confirmation and reason; audit row written |
| A25 | `test_worker_interruptions.py` | 2 | SIGTERM and SIGKILL sent to a running worker | SIGTERM records failure after stopping children; SIGKILL leaves `running` with the lock held |
| A26 | `test_logging.py` | 14 | Logging setup, canary failures, gateway and receiver requests, worker failures | One handler per logger; levels; no keys or request values in log lines |
| A27 | `test_run_unit_tests.py` | 4 | A tiny passing and a tiny failing test suite | Result file has correct counts and exit code; CI environment values match `run_ci.py` |
| A28 | `test_frontend_markup.py` | 12 | The two HTML pages and `pipeline_summary.js` read as text | Required ids and messages present; every id used by a script exists; address validation identical on both pages |
| A29 | `test_log_feature_importance.py` | 8 | A tiny model and a throwaway MLflow store | Wrong hash refused before logging; one analysis run with nine importance values; model and registry unchanged |
| A30 | `test_promote_model.py` | 16 | A throwaway registry with versions of different validation quality | Dry run changes nothing; floors and comparison enforced; test-set evidence refused; rollback restores the previous alias |
| A31 | `test_grafana_dashboards.py` | 6 | The two dashboard JSON files | Titles, descriptions, axis labels with units, legends and colour-blind-safe colours present |

The 110 tests of A01 to A16 are the ones counted in the earlier recorded runs; A17 to A31
add 129 tests, giving 239.

## Execution procedure

From the project root, run:

```bash
.venv/bin/python scripts/run_ci.py
```

The wrapper runs automated tests and then DVC reproduction, records actual exits,
saves per-command logs and stops if a stage fails. A successful cached DVC check
is evidence of up-to-date dependencies/outputs, not a new model fit.

The `unit_tests` DVC stage runs the same suite through `scripts/run_unit_tests.py`
when its inputs change, so a changed test run executes the suite twice; when nothing
changed the stage is skipped.

Focused test commands may be used for a bounded code change. Record their scope;
do not describe a focused execution as a new full-suite result.

## Manual and integration cases

| ID | Procedure | Expected behavior / evidence |
|---|---|---|
| I01 | Start main stack and query /health | Ready and exact original registered model checksum |
| I02 | Send valid prediction and explanation | Score/threshold/decision consistent; nine additive raw-log-odds contributions |
| I03 | Inspect console and API history | Configured topology distinguished from recorded commands and saved snapshots |
| I04 | Run gate with normal and deliberately impossible AP floor | Normal PASS/0; impossible policy FAIL/1; no test-set evaluation |
| I05 | Route through each real model server, then restore zero weight | Both paths respond; model/threshold boundaries preserved |
| I06 | Stop challenger after enabling its traffic | One champion fallback, automatic zero weight, next call directly champion |
| I07 | Restart challenger | Challenger health recovers without re-enabling traffic |
| I08 | Verify gateway metrics and Prometheus target | Target up; traffic/latency/routing metrics visible; Grafana panels load |
| I09 | Temporary synthetic alert true, then false with same labels | Firing and resolved delivered; original rules restored |
| I10 | Send shifted direct-API traffic and scan deliveries | Real drift alert delivered; one request queued from eligible notification |
| I11 | Restore prepared marginal-matching traffic | Low input drift returns; resolved notification delivered |
| I12 | Explicitly run queued request | Separate MLflow candidate/receipt; gate result saved; champion unchanged |
| I13 | Replay completed request | Refused as not_queued; training log unchanged |
| I14 | Run fixed concurrent scenarios | Preserve counts, all-attempt and success latencies, failures and exact run context |

Manual drills may change process-local counters and traffic windows. Preserve
before/after identity and restore challenger weight zero and low drift after drills.
Use the controlled drill evidence for completed cases; do not rerun expensive fits
solely to populate documentation.

## Frontend cases

Remain on the frontend at port 8080; change the API field inside the page.

| ID | Action | Expected behavior |
|---|---|---|
| U01 | Predict and explain default transaction | Assessment and nine contribution rows; model identity displayed |
| U02 | Apply http://localhost:8000 | Health reports ready at selected address |
| U03 | Reload after applying localhost | Saved API origin remains selected; readiness is rechecked |
| U04 | Apply unused localhost port 8999 | API unavailable; previous assessment cleared |
| U05 | Restore default | Default origin selected; readiness returns |
| U06 | Apply http://example.com:8000 | Local-origin validation rejects it; active origin unchanged |
| U07 | Clear the amount field and choose Check transaction | "Enter the transaction amount." appears under the field; no request is sent |
| U08 | Enter -5 as the sender balance | "The sender balance cannot be negative." appears under the field and disappears when corrected |
| U09 | Pipeline page: apply `http://localhost:8000`, then reload | The address is remembered and is the one used on the transaction page |
| U10 | Pipeline page with a failed run in the history | The row is marked FAILED and shows the stored reason, or says that no error text is stored |

U03 was confirmed manually later (see test_report.md). U07 to U10 describe behaviour added
on 2026-11-10 and have **not been run in a browser**; only static markup checks (A28)
exist for them.

Original note: U03 reload persistence was not explicitly confirmed in the recorded
conversation; record it separately before marking it passed. Screenshots of other cases support
manual observations, not automated browser-test coverage.

## Performance policy

Closed-loop tests use 1, 4 and 8 clients, five warmups per client, 1,000 measured
requests per full scenario, and the first 1,000 validation input rows. Run endpoint
workloads sequentially. Higher concurrency is skipped after measured failures.
Smoke cases use fewer requests and are not population-level latency estimates.

No predeclared fixed-rate SLA or maximum capacity was demonstrated. Do not invent
a pass threshold afterward. Report throughput, success/failure counts, median,
p95, p99 and maximum, including failed warmup attempts and all-attempt latency.
See [load_test_report.md](load_test_report.md).

## Completion and defect handling

Functional cases pass only with their expected assertions/evidence. Retain failed
reports alongside later successful runs. Gateway warmup/load 503s remain a known
unresolved finding. Record interrupted worker state without automatically resetting
it to queued. Never include raw transaction bodies or admin secrets in evidence.

Documentation completeness, fresh-machine restoration, host/component monitoring,
fixed-rate acceptance targets and course-rubric compliance require separate review;
a passing unit-test result alone does not prove them.

## Static style check

The line-length limit is 100 characters (`setup.cfg`, section `[pycodestyle]`).
`pycodestyle` is pinned in `requirements-dev.txt` and is deliberately not part of
`scripts/run_ci.py`. To run it from the project root:

```bash
python -m venv --system-site-packages /tmp/stylenv
/tmp/stylenv/bin/pip install pycodestyle==2.15.0
/tmp/stylenv/bin/pycodestyle backend src scripts
```

Result on 2026-11-10: `backend` 0, `src` 0, `scripts` 28 findings. All 28 are
E402 (import not at top) in `scripts/train_logreg.py` and `scripts/train_xgboost.py`.
Those two files are inputs of DVC training stages, so they were not edited: any
change would invalidate the stages and force retraining. Other intentional
import-order cases carry `# noqa: E402` with a reason.
