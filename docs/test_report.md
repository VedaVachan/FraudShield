# FraudShield Test Report

## Evidence and scope

This report consolidates recorded local results through 10 November 2026. The sections
"Latest verification" and "Acceptance results" come from the run of 10 November 2026;
the sections after them are earlier records and keep their original numbers (110 tests).
Source evidence remains in the repository reports listed below. Results apply to the stated revision and local
VM configuration; they do not establish production readiness or course compliance.

## Latest verification (10 November 2026)

Local CI (`.venv/bin/python scripts/run_ci.py`) run `113fe711-1fff-4c3c-b516-330d92a54548` ended with exit
code 0. The test step ran **265 tests with no failures or errors**. The
`dvc_reproduction` step reported "Data and pipelines are up to date": the data, both
training stages and the validation gate were skipped as unchanged, so nothing was
retrained. The tests also run as the DVC stage `unit_tests`, whose last result is in
`reports/unit_tests_result.json`. The CI logs are under `runtime/ci/113fe711-1fff-4c3c-b516-330d92a54548/`
(local and git-ignored); the pipeline history is `runtime/pipeline_runs.jsonl`.

Tests added since the 110-test run (counted with the unittest loader; the 31 test files
are listed with their cases in [test_plan.md](test_plan.md)):

| Commit | Files added | Tests | Total after |
|---|---|---:|---:|
| 99e7324, 8c2db59, d2fbedc, 6b3bf87 | `test_mlflow_identity`, `test_model_client_transport`, `test_model_client_identity`, `test_gateway_identity` | 3 + 2 + 6 + 2 | 123 |
| 3d3900a | `test_process_runner`, `test_worker_interruptions` | 4 + 2 | 129 |
| f69067d | `test_result_storage` | 9 | 138 |
| b646b1e | `test_retraining_recovery` | 31 | 169 |
| d5c3dc9 | `test_gateway_server_coverage` | 10 | 179 |
| uncommitted at writing | `test_logging`, `test_run_unit_tests`, `test_frontend_markup`, `test_log_feature_importance`, `test_promote_model`, `test_grafana_dashboards` | 14 + 4 + 38 + 8 + 16 + 6 | 265 |

No existing test file changed after the 110-test run. Unit-test coverage of `backend/` and
`src/fraudshield/` was measured at 217 tests: 93% (1,275 statements, 83 missed, lowest
file `backend/drift.py` at 82%); code run only in subprocesses and `scripts/` are not
measured (`reports/coverage_summary.txt`).

One intermittent failure was seen on 10 November 2026: in one CI invocation the
`unit_tests` stage failed with an error in
`test_worker_interruptions.test_sigterm_stops_descendants_before_recording_failure`
(an empty process-ID file was read), although the same suite had just passed in the
preceding step. A repeat run passed. The failure points to a timing race in the test
setup under load, not to a product defect; it has not been fixed or investigated further.

## Acceptance results

The criteria are defined in [test_plan.md](test_plan.md#acceptance-criteria). They were
written on 2026-11-10, after the benchmarks existed, so this table records what is
verified, not predictions.

| ID | Criterion | Basis | Evidence file | Result |
|---|---|---|---|---|
| AC-01 | All automated tests pass | CI exit 0, no failures or errors | `runtime/ci/113fe711-1fff-4c3c-b516-330d92a54548/automated_tests.log` (local); `reports/unit_tests_result.json` | MET: 265 tests |
| AC-02 | Coverage of `backend/` and `src/fraudshield/` at least 80% | 80% | `reports/coverage_summary.txt` | MET: 93% (measured at 217 tests); not measured: code run only in subprocesses, `scripts/` |
| AC-03 | Frozen DVC baseline passes the validation gate floors | AP >= 0.85, precision >= 0.70, recall >= 0.90 | `reports/xgboost_validation_gate.json` | MET: status PASS, AP 0.9885092, precision 0.9284028, recall 1.0 (validation, model `4b1fa5ce`, run of 2026-11-05) |
| AC-04 | Frozen DVC baseline meets the same floors on the held-out split | Same floors | `reports/xgboost_heldout_evaluation.json` | MET for this artifact only: AP 0.9926524, precision 0.9578867, recall 0.9992013 (89,466 test rows) |
| AC-05 | Deployed champion and logistic regression have held-out results | A recorded evaluation of both | `reports/frozen_models_heldout_evaluation.json` does not exist | NOT MEASURED |
| AC-06 | Single-client `/predict` p95 under 50 ms | 50 ms | `reports/concurrent_api_predict.json` | MET: 19.94 ms (1,000 of 1,000 succeeded) |
| AC-07 | p95 at 50 concurrent users under 50 ms | A recorded 50-client run | none | NOT MEASURED: no 50-user run was performed. Measured without a target: `/predict` p95 96.15 ms at 4 clients and 252.59 ms at 8 clients |
| AC-08a | Direct API runs: failed requests under 0.5% per scenario | Per scenario | `reports/concurrent_api_predict.json`, `reports/concurrent_api_explain.json` | MET: 0 of 1,000 failed in each of the six scenarios (1, 4 and 8 clients, `/predict` and `/explain`) |
| AC-08b | Gateway diagnostic and eight-client runs | Per scenario | `reports/concurrent_gateway_diagnostic.json`, `reports/concurrent_gateway_eight_clients.json` | MET: 0 of 1,000 failed at 4 clients and at 8 clients |
| AC-08c | Gateway run 2, four clients | Per scenario | `reports/concurrent_gateway_predict_run2.json` | NOT MET: 16 of 1,000 failed (1.6%, all HTTP 503). The cause is unresolved; later runs do not erase it |
| AC-08d | First gateway attempt | A measured scenario | `reports/concurrent_gateway_predict.json` | NOT MET: it failed during warmup with HTTP 503 and produced no measured scenario; cause unresolved |
| AC-09 | Failing challenger removed automatically after one champion fallback | One recorded drill | `reports/gateway_http_drill.json`; tests A04, A07 | MET, narrowly: automatic weight zero PASS, next request to the champion PASS, detection and fallback in 0.033 s. A single manual observation from shared output, not an automated log |
| AC-10 | `dvc repro` reports the pipeline up to date | CI step | `runtime/ci/113fe711-1fff-4c3c-b516-330d92a54548/dvc_reproduction.log` (local) | MET: all stages skipped as unchanged. This is a cache check, not a retraining |
| AC-11 | Reproduction from a fresh clone gives the same metrics | A recorded fresh-clone run | none | NOT MEASURED |
| AC-12 | Retraining is candidate-only | Drill records and tests | `reports/drift_request_drill.json`, `reports/controlled_retraining_drill/`; tests A15, A24, A25 | MET: queued request carries `automatic_promotion: false`; `result.json` and `training_receipt.json` record `registered_or_promoted: false`. Effectiveness against shifted data was not shown |

## Screenshots

Placeholders only. The images are to be added by the author under `docs/evidence/`; none of
these files exists yet, so nothing in this report depends on them. Each file should show the
state named in the last column.

| File | What it should show |
|---|---|
| `docs/evidence/01_transaction_page_ready.png` | Transaction page just after opening, status chip reading "API ready" |
| `docs/evidence/02_flagged_result_with_explanation.png` | "Full-drain transfer" example checked with "Check + explain": flagged banner, score meter and contribution bars |
| `docs/evidence/03_not_flagged_result.png` | "Normal payment" example: "Not flagged by the model" and the meter below the threshold |
| `docs/evidence/04_inline_validation_error.png` | An empty or negative field with its red border and message under the field |
| `docs/evidence/05_connection_settings_open.png` | "Connection settings" expanded with the API address field and its buttons |
| `docs/evidence/06_pipeline_diagram_and_table.png` | Pipeline page: DVC diagram with key, and the recorded-executions table |
| `docs/evidence/07_pipeline_failed_run_row.png` | A failed run shown with its FAILED pill and reason text, if one is in the history |
| `docs/evidence/08_grafana_service_monitoring.png` | Grafana dashboard "FraudShield — Service Monitoring" |
| `docs/evidence/09_grafana_resources.png` | Grafana dashboard "FraudShield — VM and Container Resources" |
| `docs/evidence/10_prometheus_targets.png` | Prometheus targets page with the scrape jobs and their state |
| `docs/evidence/11_ci_run_output.png` | Terminal output of `scripts/run_ci.py` ending in "PASS: local CI completed" |
| `docs/evidence/12_coverage_report.png` | The per-file coverage table from `reports/coverage_summary.txt` |

## Earlier recorded verification (110 tests)

The recorded full CI run executed **110 tests successfully**, followed by a
successful DVC reproduction command. The test phase took 87.739 seconds. Run ID:
`4856995d-5df5-4c06-8937-f43f2427bcfa`, at revision `c3c9d08`.
Evidence: `reports/retraining_ci/execution.json`, `automated_tests.log` and
`dvc_reproduction.log` in that directory.

DVC skipped unchanged stages: this verifies the command outcome and cached
pipeline state, not a fresh training run. Subsequent upstream-diagnostic logging
was checked with the nine focused model-client tests. Frontend changes received
manual checks rather than another recorded full-suite execution.

| Suite | Passing tests |
|---|---:|
| Alert receiver | 8 |
| API | 6 |
| Canary router | 10 |
| Canary rollback | 7 |
| Drift scanner | 6 |
| Shared features | 6 |
| Gateway | 12 |
| Gateway metrics | 6 |
| API metrics | 3 |
| Model client | 9 |
| Model gate | 6 |
| Pipeline history | 6 |
| Pipeline summary | 3 |
| Retraining queue | 9 |
| Retraining worker | 9 |
| Spark features | 4 |
| **Total** | **110** |

Deprecation and resource warnings appeared in recorded output despite successful
assertions. Their presence must not be described as a warning-free run.

## Operational drills

| Check | Recorded outcome | Evidence |
|---|---|---|
| HTTP model serving | Three checked inputs agreed with the expected scoring path | `reports/mlflow_http_verification.json` |
| Local gateway failure | Failed challenger fell back once; weight became zero; next request used champion | `reports/gateway_http_drill.json` |
| Container canary | Both routes scored; stopped challenger triggered removal; challenger was restarted with weight remaining zero | `reports/compose_canary_drill.json` |
| Prometheus integration | Gateway target was up and exported prediction/routing metrics | `reports/gateway_metrics_ci/scrape_verification.json` and `metrics_snapshot.txt` |
| Alert delivery | Firing and resolved notifications reached the local receiver; original drift rules restored | `reports/alert_delivery_verification.json` |
| Real drift request | Shifted traffic delivered a drift notification and queued one request; recovery inputs restored low marginal drift | `reports/drift_request_drill.json` |
| Controlled training | Candidate training and validation gate succeeded; candidate checksum and terminal queue state verified | `reports/controlled_retraining_drill/` |
| Recovery and replay | Resolved notification received; completed request refused replay without changing its training log | `reports/controlled_retraining_drill/recovery_and_replay.json` |

The first synthetic resolution attempt timed out after removing its rule. The
successful repeat retained the rule and changed its condition to false. This
later success does not make the first attempt successful.

## Controlled candidate identity

Request: `418aa34f-fc6b-4228-9a26-f59be1879c26`.
MLflow run: `6354dedb1a8f4360b22553f9e147fa06`.
Candidate SHA256:
`cb69091fce02570dd1fe85add2dd67caee400cc81633e3d62ac9d4ffddd27f66`.
Training and gate exit codes were both zero; elapsed worker time was approximately
202 seconds. The deployed API model remained unchanged and challenger weight
remained zero. The candidate was neither registered nor promoted. This exercise
reused existing training/validation data; it does not demonstrate that retraining
improved accuracy or repaired drift.

## Manual frontend observations

| Case | Result |
|---|---|
| U01 default connection and assessment | Observed ready status and successful prediction/explanation |
| U02 localhost alternative | Applying `http://localhost:8000` showed API ready |
| U03 persistence after reload | **Not explicitly confirmed** |
| U04 unused local port | Applying port 8999 showed unavailable and cleared assessment |
| U05 default recovery | Restoring the default returned to a ready API |
| U06 nonlocal origin | `http://example.com:8000` was rejected; previous valid origin remained active |

Screenshots were shown during manual verification; they are not automated browser
coverage or an archived screenshot suite. The backend root returning
`{"detail":"Not Found"}` is expected: the frontend is served on port 8080.

## Concurrent load observations

Each row below represents 1,000 measured attempts, excluding warmup. The final
column includes failed attempts. These were separate local runs, not controlled
comparisons of isolated gateway overhead.

| Path/run | Clients | Successes | Failures | Successful req/s | Success p95 ms | All-attempt p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| API predict | 1 | 1000 | 0 | 82.54 | 19.94 | 32.03 |
| API predict | 4 | 1000 | 0 | 74.31 | 96.15 | 160.59 |
| API predict | 8 | 1000 | 0 | 52.61 | 252.59 | 319.66 |
| API explain | 1 | 1000 | 0 | 44.85 | 38.18 | 59.31 |
| API explain | 4 | 1000 | 0 | 28.46 | 247.51 | 350.86 |
| API explain | 8 | 1000 | 0 | 28.45 | 379.74 | 561.49 |
| Gateway run 2 | 1 | 1000 | 0 | 40.06 | 38.51 | 54.32 |
| Gateway run 2 | 4 | 984 | 16 | 38.53 | 101.28 | 2013.06 |
| Gateway diagnostic | 4 | 1000 | 0 | 84.75 | 76.51 | 99.35 |
| Gateway later run | 8 | 1000 | 0 | 78.50 | 162.78 | 245.21 |

An earlier gateway run failed during warmup with HTTP 503 and recorded no measured
scenario. Run 2 stopped before eight clients after 16 failures at four clients
(1.6%). Later diagnostic and eight-client runs succeeded without upstream failure
log entries. The cause of earlier 503s remains unresolved; adding diagnostics
and recreating the gateway is not proof of a functional fix. No timeout increase
was used to obtain these later results. The eight-client explanation run also
contained a 4,904 ms maximum latency despite zero failed requests.

Full sources and checksum summary: [load_test_report.md](load_test_report.md) and
`reports/load_test_summary.json`. Closed-loop observations do not demonstrate a
sustained fixed arrival rate, 50 simultaneous users, a capacity limit or an SLA.

## Remaining verification

Not measured: fresh-clone reproduction (AC-11), a 50-user load run (AC-07) and held-out
results for the deployed model and logistic regression (AC-05). The frontend changes of
10 November 2026 (manual cases U07 to U10) have not been run in a browser. The earlier
request to confirm U03 was met (see below). Review fresh-machine setup and course
acceptance criteria against the actual rubric. Neither historical test counts nor later successful load runs resolve the
earlier gateway failure finding. Container readiness and threshold consistency
also do not establish per-response model artifact identity.

## Final verification update

At revision `187fb3a`, local CI run
`bb6af575-7e9a-48a7-8046-aab57336728f` passed all 110 tests
in 33.637 seconds. DVC reproduction succeeded with unchanged stages skipped.
Records and logs are preserved in `reports/final_ci/`.

Manual case U03 passed: after applying `http://localhost:8000` inside
the transaction page and reloading, the address remained selected and
the header showed API ready. The `/health` and `/docs` pages also loaded.
This supersedes the earlier pending U03 status.

## MLproject environment verification

MLflow created the declared Conda environment and successfully ran the gate
and all 110 tests (28.363 seconds, zero failures). Run records and configuration
checksums are in `reports/mlproject_environment_verification.json`.
The configuration was subsequently committed as `e789920`.
CI and DVC retain their separate existing environments; host Java remains
a prerequisite. This verifies the test/gate environment, not complete
fresh-machine or deployment-environment parity.
