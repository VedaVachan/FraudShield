"""End-to-end smoke check against the RUNNING FraudShield stack.

This is NOT part of the unit tests or of scripts/run_ci.py: it needs the Docker stack
(frontend, API, Prometheus, Grafana) to be up. It uses only the Python standard library.

It sends four scoring requests to the API (two /predict, one /explain, one invalid
/predict), so those appear in the API metrics and in the drift window. It changes
nothing else. Exit code 0 means every check passed, 1 means at least one failed.
The result is written to reports/e2e_smoke_result.json (replaced on every run).
"""

import argparse
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / "reports/e2e_smoke_result.json"
PAYMENT = {"type": "PAYMENT", "amount": 1000.0, "oldbalanceOrg": 5000.0}
COUNTER = 'fraudshield_requests_total{endpoint="/predict",status="200"}'
COUNTER_LINE = re.compile(
    r'^fraudshield_requests_total\{endpoint="/predict",status="200"\} ([0-9.eE+-]+)$',
    re.MULTILINE,
)
OPENER = build_opener(ProxyHandler({}))


class StackUnavailable(Exception):
    """A service could not be reached at all (not an HTTP error status)."""


def fetch(url, method="GET", body=None, timeout=10.0):
    """Return (status, text); HTTP error statuses are returned, not raised."""
    data = None if body is None else json.dumps(body).encode()
    request = Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read(2_000_000).decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read(2_000_000).decode("utf-8", "replace")
    except (URLError, OSError) as exc:
        raise StackUnavailable(f"{url}: {getattr(exc, 'reason', exc)}") from exc


def fetch_json(url, **kwargs):
    """Fetch and parse a JSON response; returns (status, parsed or None)."""
    status, text = fetch(url, **kwargs)
    try:
        return status, json.loads(text)
    except ValueError:
        return status, None


class Smoke:
    """Run named checks, keep their outcome and never stop at the first failure."""

    def __init__(self, urls, timeout, scrape_wait):
        """Remember the service addresses and the waiting limits."""
        self.urls = urls
        self.timeout = timeout
        self.scrape_wait = scrape_wait
        self.checks = []

    def check(self, name, function):
        """Run one check; any exception becomes a failed check with its message."""
        started = time.perf_counter()
        try:
            detail = function()
            passed = True
        except AssertionError as exc:
            passed, detail = False, str(exc) or "assertion failed"
        except StackUnavailable as exc:
            passed, detail = False, f"not reachable: {exc}"
        except Exception as exc:  # a smoke check must report, never crash the run
            passed, detail = False, f"{type(exc).__name__}: {exc}"
        self.checks.append({
            "name": name, "passed": passed, "detail": detail,
            "seconds": round(time.perf_counter() - started, 3),
        })
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {detail}", flush=True)

    def api(self, path, **kwargs):
        """Fetch JSON from the API."""
        return fetch_json(self.urls["api"] + path, timeout=self.timeout, **kwargs)

    def frontend_pages(self):
        """Both frontend pages and the script the pipeline page loads."""
        for path, markers in (
            ("/", ("Transaction Review", 'id="connection-details"')),
            ("/pipeline.html", ("Pipeline", "Refresh history")),
            ("/pipeline_summary.js", ()),
        ):
            status, text = fetch(self.urls["frontend"] + path, timeout=self.timeout)
            assert status == 200, f"{path} returned HTTP {status}"
            for marker in markers:
                assert marker in text, f"{path} does not contain {marker!r}"
        return "index, pipeline page and pipeline_summary.js served"

    def api_health(self):
        """Readiness and a well-formed model identity."""
        status, body = self.api("/health")
        assert status == 200, f"HTTP {status}"
        assert body and body.get("status") == "ready", f"status is {body and body.get('status')}"
        assert re.fullmatch(r"[0-9a-f]{64}", body.get("model_sha256", "")), "no model hash"
        return f"ready, {body['model_name']} v{body['model_version']}"

    def api_predict(self):
        """A valid scoring request returns a consistent score and decision."""
        status, body = self.api("/predict", method="POST", body=PAYMENT)
        assert status == 200, f"HTTP {status}"
        assert 0 <= body["score"] <= 1, "score outside [0, 1]"
        assert body["predicted_fraud"] == (body["score"] >= body["threshold"]), \
            "decision does not follow score and threshold"
        return f"score {body['score']:.6f}, predicted_fraud={body['predicted_fraud']}"

    def api_explain(self):
        """The explanation has nine contributions that add up to the raw margin."""
        status, body = self.api("/explain", method="POST", body=PAYMENT)
        assert status == 200, f"HTTP {status}"
        assert len(body["contributions"]) == 9, "expected nine contributions"
        total = body["base_value"] + sum(c["shap_log_odds"] for c in body["contributions"])
        assert abs(total - body["raw_margin"]) < 1e-3, "contributions do not add up"
        return "nine contributions, additive to the raw margin"

    def api_rejects_bad_input(self):
        """Invalid input gets the generic 422 and nothing is echoed back."""
        status, text = fetch(
            self.urls["api"] + "/predict", method="POST",
            body={**PAYMENT, "amount": -1}, timeout=self.timeout,
        )
        assert status == 422, f"expected 422, got {status}"
        assert json.loads(text) == {"detail": "Invalid transaction input."}, "unexpected body"
        return "HTTP 422 with the generic message"

    def pipeline_summary(self):
        """The saved pipeline summary and the run history are served."""
        status, body = self.api("/pipeline/summary")
        assert status == 200 and body and body.get("schema_version") == 1, \
            f"summary: HTTP {status}"
        status, runs = self.api("/pipeline/runs?limit=5")
        assert status == 200 and isinstance(runs, dict) and "runs" in runs, \
            f"runs: HTTP {status}"
        return f"summary schema 1; history_available={runs.get('history_available')}"

    def prometheus_target(self):
        """Prometheus reports the API scrape target as up."""
        status, body = fetch_json(self.urls["prometheus"] + "/api/v1/targets",
                                  timeout=self.timeout)
        assert status == 200 and body, f"HTTP {status}"
        jobs = {
            t["labels"].get("job"): t["health"] for t in body["data"]["activeTargets"]
        }
        assert jobs.get("fraudshield-api") == "up", \
            f"fraudshield-api target is {jobs.get('fraudshield-api', 'absent')}"
        return "fraudshield-api target is up"

    def prometheus_counter(self):
        """Prometheus has scraped the request counter after this run's requests."""
        status, text = fetch(self.urls["api"] + "/metrics", timeout=self.timeout)
        assert status == 200, f"API /metrics returned HTTP {status}"
        match = COUNTER_LINE.search(text)
        assert match, f"API /metrics has no {COUNTER} line"
        api_value = float(match.group(1))
        assert api_value >= 1, "API counter is 0 after successful predictions"

        query = self.urls["prometheus"] + "/api/v1/query?query=" + quote(COUNTER)
        deadline = time.monotonic() + self.scrape_wait
        seen = None
        while True:
            status, body = fetch_json(query, timeout=self.timeout)
            results = body["data"]["result"] if status == 200 and body else []
            if results:
                seen = float(results[0]["value"][1])
                if seen >= api_value:
                    return (f"Prometheus counter {seen:g} reached the API's {api_value:g}")
            if time.monotonic() >= deadline:
                raise AssertionError(
                    f"after {self.scrape_wait:g} s Prometheus shows "
                    f"{'no series' if seen is None else seen} for {COUNTER}; "
                    f"the API reports {api_value:g}"
                )
            time.sleep(2)

    def grafana_health(self):
        """Grafana reports a healthy database."""
        status, body = fetch_json(self.urls["grafana"] + "/api/health", timeout=self.timeout)
        assert status == 200 and body, f"HTTP {status}"
        assert body.get("database") == "ok", f"database is {body.get('database')}"
        return f"database ok, version {body.get('version', 'unknown')}"

    def run(self):
        """Run every check in a fixed order and return the result record."""
        started = datetime.now(timezone.utc)
        self.check("frontend serves both pages", self.frontend_pages)
        self.check("API /health is ready", self.api_health)
        self.check("API /predict returns a consistent score", self.api_predict)
        self.check("API /explain returns additive contributions", self.api_explain)
        self.check("API rejects bad input with 422", self.api_rejects_bad_input)
        self.check("API /pipeline/summary and /pipeline/runs", self.pipeline_summary)
        self.check("Prometheus reports the API target up", self.prometheus_target)
        self.check("Prometheus has scraped the request counter", self.prometheus_counter)
        self.check("Grafana /api/health is ok", self.grafana_health)
        failed = [c["name"] for c in self.checks if not c["passed"]]
        return {
            "schema_version": 1,
            "scope": "running stack only; not part of unit tests or scripts/run_ci.py",
            "started_utc": started.isoformat(),
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "addresses": self.urls,
            "passed": not failed,
            "checks_run": len(self.checks),
            "checks_failed": len(failed),
            "failed_checks": failed,
            "checks": self.checks,
            "requests_sent_to_api": {
                "predict_valid": 1, "explain_valid": 1, "predict_invalid": 1,
                "note": "the counter check reads /metrics only",
            },
        }


def write_result(record, path):
    """Write the result file atomically so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv=None):
    """Parse addresses, run the checks, write the result and return an exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frontend", default="http://127.0.0.1:8080")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--prometheus", default="http://127.0.0.1:9095")
    parser.add_argument("--grafana", default="http://127.0.0.1:3010")
    parser.add_argument("--output", type=Path, default=RESULT)
    parser.add_argument("--timeout", type=float, default=10.0, help="seconds per request")
    parser.add_argument("--scrape-wait", type=float, default=45.0,
                        help="seconds to wait for Prometheus to scrape (interval is 15 s)")
    args = parser.parse_args(argv)

    urls = {
        name: getattr(args, name).rstrip("/")
        for name in ("frontend", "api", "prometheus", "grafana")
    }
    record = Smoke(urls, args.timeout, args.scrape_wait).run()
    write_result(record, args.output)
    print(f"{'PASS' if record['passed'] else 'FAIL'}: {record['checks_run'] - record['checks_failed']}"
          f"/{record['checks_run']} checks passed; result in {args.output}")
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
