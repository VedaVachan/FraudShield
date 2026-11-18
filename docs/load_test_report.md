# Load-test report

## How I tested

Each test used 1, 4 or 8 clients in a closed loop: a client sends one transaction, waits
for the answer, then sends the next. Connections stayed open between requests, and each
client sent five warm-up requests that aren't counted. A full scenario tried 1,000
measured requests. I also did smoke runs with 40 requests per scenario; those are not in
the table.

The inputs were the first 1,000 rows of the validation split, using only `type`, `amount`
and `oldbalanceOrg`. No test-set rows or labels were used. That's a fixed sample, so it
doesn't tell you how the system behaves on every kind of transaction.

The latency figures cover encoding the JSON, the local HTTP call, the server's work and
decoding the response. "4 clients" is not the same as a fixed request rate.

FraudShield's own monitoring stayed on during the tests; I stopped the older Assignment 4
monitoring stacks first. The VM has 6 vCPUs and about 8 GiB of RAM, and the API and the
MLflow model-server containers are each limited to 2 CPUs.

## Results

| Run | Clients | Succeeded / tried | Failed | Successful req/s | p95 of successes (ms) | p99 of all attempts (ms) |
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

The JSON reports behind this table also keep the median, p95, p99 and maximum latency,
timings, failure counts and the model and routing checks. `reports/load_test_summary.json`
lists the hash and code commit of each source report.

## Failures

The direct API never failed a measured request, for either `/predict` or `/explain`.

The gateway did. My first gateway attempt failed during warm-up with HTTP 503 and never
got to a measured scenario. A later run had 16 HTTP 503 failures out of 1,000 requests at
four clients, a 1.6% failure rate, and its failure guard then skipped the eight-client
scenario.

After that I added diagnostic logging and recreated the gateway container, and separate
four-client and eight-client runs finished with no failures. Logging doesn't fix anything
by itself, so I can't say what caused the earlier 503s, and the later clean runs don't
cancel out the failures. The "p95 of successes" column leaves failed requests out; the
"p99 of all attempts" column includes them, which is why it jumps to 2013 ms in the
failing run.

## What the numbers show and don't show

For the direct API, latency went up and throughput went down as I added clients.
Explanations levelled off at about 28.45 successful requests per second at both four and
eight clients, and the slowest eight-client explanation took about 4.90 seconds.

The gateway numbers moved around between runs. The gateway calls a separate MLflow HTTP
model server, so the difference from the direct API isn't only gateway overhead.

These were short runs on one local machine. They are not a sustained-capacity test, a
fixed-rate test, a 50-user test, a test over a real network or any kind of SLA. I
collected resource counters afterwards, which can't show what the machine was doing at
the moment a request failed.

## Leaving the system as I found it

The benchmark checked the API's model identity and required the gateway to route
everything to the champion. After the direct API prediction runs I sent 500 prepared
synthetic requests to bring the drift window back to normal. Those weren't part of the
measurements and they don't show the model adapting or retraining.

I kept every report, including the failed and smoke runs.
