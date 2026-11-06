# FraudShield Concurrent Load-Test Report

## Method

Tests used 1, 4 or 8 closed-loop clients, one transaction per request,
persistent client connections, and five warmup requests per client.
Full scenarios attempted 1,000 measured requests each. Smoke runs used
40 requests per scenario and are excluded from the table below.

Inputs were the first 1,000 validation rows using only type, amount and
oldbalanceOrg. No test-set inputs or labels were used. This fixed sample
does not establish performance across the full transaction population.

Latency includes JSON encoding, local HTTP, server processing and response
decoding. Warmup is excluded. Concurrent clients are not a fixed arrival
rate or an equivalent number of requests per second.

FraudShield monitoring remained enabled. The older Assignment 4 monitoring
stacks were stopped. The VM has 6 vCPUs and approximately 8 GiB RAM.
The API and MLflow model-server containers each have a 2-CPU limit.

## Measured results

| Run | Clients | Successful / attempted | Failures | Successful req/s | Success p95 ms | All-attempt p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| api_predict | 1 | 1000 / 1000 | 0 | 82.54 | 19.94 | 32.03 |
| api_predict | 4 | 1000 / 1000 | 0 | 74.31 | 96.15 | 160.59 |
| api_predict | 8 | 1000 / 1000 | 0 | 52.61 | 252.59 | 319.66 |
| api_explain | 1 | 1000 / 1000 | 0 | 44.85 | 38.18 | 59.31 |
| api_explain | 4 | 1000 / 1000 | 0 | 28.46 | 247.51 | 350.86 |
| api_explain | 8 | 1000 / 1000 | 0 | 28.45 | 379.74 | 561.49 |
| gateway_predict_run2 | 1 | 1000 / 1000 | 0 | 40.06 | 38.51 | 54.32 |
| gateway_predict_run2 | 4 | 984 / 1000 | 16 | 38.53 | 101.28 | 2013.06 |
| gateway_diagnostic | 4 | 1000 / 1000 | 0 | 84.75 | 76.51 | 99.35 |
| gateway_eight_clients | 8 | 1000 / 1000 | 0 | 78.50 | 162.78 | 245.21 |

The source JSON reports retain median, p95, p99, maximum latency, timing,
failure counts, model checks and routing checks. Source hashes and code
commits are listed in reports/load_test_summary.json.

## Reliability findings

- Direct API prediction and explanation scenarios completed without
  measured request failures.
- The first gateway attempt failed during warmup with HTTP 503 and
  produced no measured scenario.
- A later gateway run had 16 HTTP 503 failures among 1,000 attempts at
  four clients: a 1.6% measured failure rate. Eight clients were skipped
  by that run's failure guard.
- After adding diagnostic logging and recreating the gateway, separate
  four-client and eight-client runs completed without measured failures.
  Logging was not a functional fix; the earlier failure cause remains
  unconfirmed. The later runs do not erase those failures.
- Successful-response latency excludes failed requests. All-attempt
  latency includes them; both distributions are retained.

## Performance limitations

Direct API prediction latency increased and throughput decreased as
concurrency rose in its recorded run. Explanation throughput plateaued
near 28.45 successful requests/s at four and eight clients. The slowest
eight-client explanation request took approximately 4.90 seconds.

Gateway measurements varied across separate runs. Their differences from
the direct API cannot be attributed solely to gateway overhead: the
gateway uses a separate MLflow HTTP serving path.

These are short, local observations, not sustained-capacity, fixed-rate,
50-user, remote-network or SLA verification. Resource counters collected
afterward cannot establish resource use at the instant of a failure.

## State preservation

The benchmark checked API model identity and required champion-only
gateway routing. Direct API prediction runs sent 500 prepared synthetic
recovery inputs afterward. These recovery requests were outside the
measured workload and do not demonstrate model adaptation or retraining.

All successful, failed and smoke reports are preserved for review.
