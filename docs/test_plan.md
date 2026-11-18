# FraudShield test plan

## What this covers

This plan lists how I check the project: that the data and features are consistent, that
the API behaves, that the pipeline evidence is honest, that the model gates work, that the
canary gateway recovers from a failing challenger, that alerts get delivered, that candidate
retraining stays candidate-only, and how fast it runs locally. It collects the checks the
project actually has. It doesn't claim every point of the course rubric is covered.

## Setup needed

- The existing Ubuntu VM: 6 vCPUs, about 8 GiB RAM, everything on CPU.
- The project `.venv`, plus the separate DVC and serving environments with their pinned
  dependencies.
- The trusted model files, the registry receipt and the prepared data.
- Docker access, the main network, the existing images and the mounts the optional stacks need.
- Secrets stay in local files that Git ignores, and no test evidence may contain a key.
- Failure tests use temporary files and mocks, never the deployed model files.
- Model selection is frozen before the held-out evaluation, and nothing is retuned on test results.

## Acceptance criteria

I wrote these on 2026-11-10, after the benchmarks, drills and tests already existed, so they
describe what the repository can show rather than predictions made in advance. The results
are in the "Acceptance results" table of [test_report.md](test_report.md).

| ID | Criterion | How it's judged |
|---|---|---|
| AC-01 | All automated tests pass | Local CI exits 0 with no failures or errors |
| AC-02 | Unit-test coverage of `backend/` and `src/fraudshield/` is at least 80% | `coverage` with `unittest`, threshold 80% |
| AC-03 | The frozen DVC baseline passes the validation gate | Validation average precision ≥ 0.85, precision ≥ 0.70, recall ≥ 0.90 |
| AC-04 | The frozen DVC baseline meets the same floors on the held-out test split | Same floors; test split opened once, after selection was frozen |
| AC-05 | The served champion and the logistic-regression baseline have held-out results | A recorded held-out evaluation of both |
| AC-06 | Single-client `/predict` p95 latency under 50 ms | Closed loop, 1,000 requests |
| AC-07 | p95 latency under 50 ms with 50 concurrent users | A recorded 50-client run |
| AC-08 | Fewer than 0.5% failed requests in each recorded benchmark scenario | Counted per scenario from the report files |
| AC-09 | A failing challenger is taken out of traffic automatically after one champion fallback | One recorded drill |
| AC-10 | `dvc repro` reports the pipeline up to date | The `dvc_reproduction` step of local CI |
| AC-11 | A fresh clone reproduces the same metrics | A recorded fresh-clone run |
| AC-12 | Retraining only ever produces a candidate; it never promotes or deploys | Drill records and tests |

## Automated tests

There are 348 `unittest` tests in 37 files. They run in local CI (`scripts/run_ci.py`) and,
since 2026-11-10, also as the DVC stage `unit_tests`, which saves its result in
`reports/unit_tests_result.json`. I counted them with the unittest loader. The tests only use
fixtures: temporary files and databases, mocks and FastAPI's `TestClient`. A02, A16 and the
integration tests A33 and A34 also load the real served model, and A16 runs Spark.

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
| A28 | `test_frontend_markup.py` | 38 | The two HTML pages and `pipeline_summary.js` read as text | Required ids and messages present; every id used by a script exists; address validation identical on both pages; top bar, hero band, collapsed connection settings, status chip states, meter, bars, loading state and example buttons present; no external resources |
| A29 | `test_log_feature_importance.py` | 8 | A tiny model and a throwaway MLflow store | Wrong hash refused before logging; one analysis run with nine importance values; model and registry unchanged |
| A30 | `test_promote_model.py` | 16 | A throwaway registry with versions of different validation quality | Dry run changes nothing; floors and comparison enforced; test-set evidence refused; rollback restores the previous alias |
| A31 | `test_grafana_dashboards.py` | 6 | The two dashboard JSON files | Titles, descriptions, axis labels with units, legends and colour-blind-safe colours present |
| A32 | `test_build_reproducibility_manifest.py` | 18 | A throwaway MLflow database and git repository | Model hash and registry links recorded; absent commit tags written as `missing`, never invented; database unchanged; manifest create-only |
| A33 | `test_integration_api.py` | 14 | The real API app and model, real metrics registry, temporary history and summary files | One request updates request, decision, latency and drift metrics; 422 changes nothing else; no transaction values in the scrape; history newest first, damaged lines counted |
| A34 | `test_integration_gateway.py` | 9 | Real gateway, canary router and HTTP model client; only two fake model servers on 127.0.0.1 | Gateway score equals the direct API score; one champion fallback and automatic removal with matching metrics; wrong upstream identity treated as failure; 503 when the champion fails |
| A35 | `test_e2e_smoke_guard.py` | 6 | No running stack (nothing listens on the addresses used) | `scripts/e2e_smoke.py` exits 1 and records every failure when the stack is down; result file replaced atomically; CI and DVC do not call the script |
| A36 | `test_rollback_drill.py` | 10 | A throwaway MLflow database standing in for the real one | Champion moves v1, v2, back to v1 on a copy; source database byte-identical and hashes recorded; real or existing locations refused; report create-only; a failing step gives exit 1 |
| A37 | `test_retraining_watch.py` | 26 | A private SQLite queue, fake training commands, real worker lock, a temporary git repository | Watch is off by default; oldest first, each request once; busy lock retried; uncommitted sources stop it; non-positive interval refused; SIGINT and SIGTERM stop it cleanly; candidate-only result; script has no registry, promotion or docker calls |

A01 to A16 are the 110 tests counted in the earlier recorded runs; A17 to A37 add 238 more,
for 348 in total.

## Running the tests

From the project root:

```bash
.venv/bin/python scripts/run_ci.py
```

This runs the tests and then `dvc repro`, records the real exit codes, saves a log for each
command and stops at the first failure. When DVC finds nothing changed, that only shows the
outputs are up to date; it isn't a new training run.

The `unit_tests` DVC stage runs the same suite through `scripts/run_unit_tests.py` whenever
its inputs change, so after you change a test the suite runs twice in one CI run. If nothing
changed, the stage is skipped.

For a small change it's fine to run just the related tests, as long as you say so. A focused
run isn't a new full-suite result.

## End-to-end smoke check (not part of CI)

`scripts/e2e_smoke.py` checks the running stack. It isn't a unit test, `unittest discover`
doesn't find it, and neither `scripts/run_ci.py` nor any DVC stage calls it, because it needs
the Docker stack up. Start the main stack (see the operator guide) and run:

```bash
.venv/bin/python scripts/e2e_smoke.py
```

It checks that the frontend on port 8080 serves both pages; that the API answers `/health`,
`/predict`, `/explain` and `/pipeline/summary`; that bad input gets a 422; that Prometheus on
port 9095 shows the API target up and has scraped the request counter after this run's
requests (it waits up to 45 seconds, and Prometheus scrapes every 15); and that Grafana on
port 3010 reports `/api/health` as ok. The result goes to `reports/e2e_smoke_result.json`,
replaced on every run, and the exit code is 0 only if every check passed. You can point it
elsewhere with `--frontend`, `--api`, `--prometheus` and `--grafana`.

It sends one valid `/predict`, one valid `/explain` and one invalid `/predict`, so those show
up in the API metrics and the drift window. It doesn't touch the canary gateway. A pass means
the stack answered at that moment; it's not a performance or load test.

## Manual and integration checks

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

Manual drills change in-memory counters and the traffic window. Record the model identity
before and after, and put the challenger weight back to zero and the drift back to low
afterwards. For cases already done, use the drill evidence rather than re-running expensive
training just to fill in a document.

## Frontend checks

Stay on the frontend at port 8080 and change the API address inside the page.

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

I confirmed U03 by hand later (see test_report.md). U07 to U10 cover behaviour added on
2026-11-10. I checked the "cannot be negative" message by hand for a negative amount
(screenshot `evidence/12_ui_invalid.png`); otherwise these four only have the static markup
tests in A28 and haven't been run in a browser. Screenshots back up what I saw by hand, but
they aren't automated browser tests.

## Load tests

The load tests use 1, 4 and 8 closed-loop clients, five warm-up requests per client, 1,000
measured requests per full scenario and the first 1,000 validation rows as input. Each
endpoint's scenarios run one after another, and higher concurrency is skipped once a scenario
has failures. Smoke runs use fewer requests and aren't meant as latency estimates.

I never set a fixed-rate target or measured maximum capacity in advance, so I'm not going to
invent a pass mark afterwards. Report throughput, successes and failures, median, p95, p99 and
maximum, including failed warm-up attempts and the latency over all attempts. See
[load_test_report.md](load_test_report.md).

## When is something done

A case only passes when its expected assertions or evidence are there. Failed reports are kept
next to later successful ones. The gateway 503s during warm-up and under load are still an open
finding. If a worker is interrupted, record its state; don't reset it to queued. Never put raw
transaction bodies or admin keys into evidence.

Some things need a separate review and aren't proved by the tests passing: whether the
documentation is complete, restoring on a fresh machine, host and per-component monitoring,
fixed-rate targets and the course rubric.

## Code style check

Lines can be up to 100 characters (`setup.cfg`, section `[pycodestyle]`). `pycodestyle` is
pinned in `requirements-dev.txt` and is deliberately left out of `scripts/run_ci.py`. To run it
from the project root:

```bash
python -m venv --system-site-packages /tmp/stylenv
/tmp/stylenv/bin/pip install pycodestyle==2.15.0
/tmp/stylenv/bin/pycodestyle backend src scripts
```

On 2026-11-10 it found nothing in `backend` or `src` and 28 findings in `scripts`, all E402
(import not at the top) in `scripts/train_logreg.py` and `scripts/train_xgboost.py`. I left those
two alone because they are inputs to the DVC training stages, and any edit would force a
retrain. The other places where imports are deliberately out of order have a `# noqa: E402`
comment explaining why.
