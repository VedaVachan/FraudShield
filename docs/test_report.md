# FraudShield Test Report

## Evidence and scope

This report consolidates recorded local results through 6 November 2026. It does
not represent a new execution of the suite. Source evidence remains in the
repository reports listed below. Results apply to the stated revision and local
VM configuration; they do not establish production readiness or course compliance.

## Automated verification

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

Confirm U03 explicitly, repeat required release gates at the final code revision,
and review fresh-machine setup and course acceptance criteria against the actual
rubric. Neither historical test counts nor later successful load runs resolve the
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
