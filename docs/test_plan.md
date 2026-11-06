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

## Automated coverage and expected behavior

Counts below describe the preserved 110-test integration execution, not newly
executed tests from installing this documentation.

| ID | Area / source | Tests | Expected behavior |
|---|---|---:|---|
| A01 | test_alert_receiver.py | 8 | Record firing/resolved; filter fields; reject bad/large input; storage failure 503 |
| A02 | test_api.py | 6 | Readiness, pipeline parity, explanation consistency, finite/valid inputs |
| A03 | test_canary.py | 10 | Deterministic allocation, valid weights, one fallback, no retry loop |
| A04 | test_canary_rollback.py | 7 | Eligible failures remove traffic; stale failures preserve newer configuration |
| A05 | test_drift_scanner.py | 6 | Bounded recent history, replay protection, skip malformed/stale/synthetic input |
| A06 | test_features.py | 6 | Correct order/values, input preservation, finite/schema/leakage rejection |
| A07 | test_gateway.py | 12 | Authentication, routing, validation, generic scoring failures |
| A08 | test_gateway_metrics.py | 6 | Request/result/latency counts, fallback/removal visibility, registry isolation |
| A09 | test_metrics.py | 3 | Invalid input not a decision; server failure counted; endpoint separation |
| A10 | test_model_client.py | 9 | Valid response; reject malformed, oversized, inconsistent or unavailable upstream |
| A11 | test_model_gate.py | 6 | Valid report accepted; changed model, weak/missing/nonfinite/test metrics rejected |
| A12 | test_pipeline.py | 6 | Bounded newest-first history, missing/partial input, finite values, sanitization |
| A13 | test_pipeline_summary.py | 3 | Saved snapshot served; missing/invalid snapshot 503 |
| A14 | test_retraining_queue.py | 9 | Delivery deduplication, cooldown, one active request, scope/feature filtering |
| A15 | test_retraining_worker.py | 9 | Success/rejection/failure/timeout, identity checks, lock and replay refusal |
| A16 | test_spark_features.py | 4 | Python/Spark parity and invalid schema/value/type rejection |
| | Total | 110 | See execution logs for actual assertions and outcomes |

## Execution procedure

From the project root, run:

```bash
.venv/bin/python scripts/run_ci.py
```

The wrapper runs automated tests and then DVC reproduction, records actual exits,
saves per-command logs and stops if a stage fails. A successful cached DVC check
is evidence of up-to-date dependencies/outputs, not a new model fit.

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

U03 reload persistence was not explicitly confirmed in the recorded conversation;
record it separately before marking it passed. Screenshots of other cases support
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
the 110-test result alone does not prove them.
