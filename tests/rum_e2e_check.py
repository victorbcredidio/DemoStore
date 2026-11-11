"""CI end-to-end assertions on what a real Collector received from browser RUM and the backend.

Usage: python3 tests/rum_e2e_check.py <collector file exporter JSONL> <browser_acceptance report JSON> <release id>
The Collector's file exporter writes one OTLP/JSON export request per line.
"""
import json
import sys

BACKEND = {"demostore", "demostore-inventory", "demostore-payment", "demostore-bank"}


def values(attributes):
    return {item["key"]: next(iter(item.get("value", {}).values()), None) for item in attributes or []}


def main(export_path, report_path, release):
    raw = open(export_path, encoding="utf-8").read()
    spans, metrics, logs = [], [], []
    for line in raw.splitlines():
        if not line.strip():
            continue
        batch = json.loads(line)
        for resource in batch.get("resourceSpans", []):
            res = values(resource.get("resource", {}).get("attributes"))
            for scope in resource.get("scopeSpans", []):
                spans += [(res, span, values(span.get("attributes"))) for span in scope.get("spans", [])]
        for resource in batch.get("resourceMetrics", []):
            res = values(resource.get("resource", {}).get("attributes"))
            for scope in resource.get("scopeMetrics", []):
                metrics += [(res, metric["name"]) for metric in scope.get("metrics", [])]
        for resource in batch.get("resourceLogs", []):
            res = values(resource.get("resource", {}).get("attributes"))
            for scope in resource.get("scopeLogs", []):
                logs += [(res, record) for record in scope.get("logRecords", [])]

    passed = []
    def check(condition, message):
        if not condition:
            raise SystemExit(f"FAILED: {message}")
        passed.append(message)

    browser = [(res, span, attrs) for res, span, attrs in spans if res.get("service.name") == "demostore-browser"]
    names = {span["name"] for _, span, _ in browser}
    check(browser, "the Collector received demostore-browser spans")
    for name in ("browser.navigation", "browser.web_vital", "browser.checkout", "POST /api/checkout", "browser.error"):
        check(name in names, f"browser span {name} received")
    check(any(res.get("service.name") == "demostore-browser" for res, _ in metrics), "browser metrics received")
    check(any(res.get("service.name") == "demostore-browser" for res, _ in logs), "browser logs received")
    resource = browser[0][0]
    check(resource.get("service.namespace") == "user-experience-lab", "browser service.namespace is user-experience-lab (A3)")
    check(resource.get("service.version") == release, f"browser service.version is the release id {release[:12]} (A4)")

    report = json.load(open(report_path, encoding="utf-8"))
    check(report.get("passed") is True, "browser acceptance run passed")
    checkout_traces = {r["traceparent"].split("-")[1] for r in report.get("requests", []) if r.get("traceparent")}
    check(checkout_traces, "browser sent traceparent on /api/checkout")
    joined = set()
    for trace_id in checkout_traces:
        services = {res.get("service.name") for res, span, _ in spans if span.get("traceId") == trace_id}
        if "demostore-browser" in services and {"demostore", "demostore-payment", "demostore-bank"} <= services:
            joined.add(trace_id)
    check(joined, f"{len(joined)} checkout trace(s) contain browser AND backend spans (one trace end to end)")
    trace_id = sorted(joined)[0]
    server = [attrs for res, span, attrs in spans if span.get("traceId") == trace_id
              and res.get("service.name") == "demostore" and span["name"] == "POST /api/checkout"]
    check(server and server[0].get("lab.traffic_type") == "synthetic", "backend checkout span labelled synthetic")
    check(all(attrs.get("lab.traffic_type") == "synthetic" for _, span, attrs in browser
              if span.get("traceId") == trace_id), "browser spans in that trace labelled synthetic")
    rum_paths = [attrs.get("http.route") for res, _, attrs in spans
                 if res.get("service.name") in BACKEND and "__rum__" in str(attrs.get("http.route", ""))]
    check(not rum_paths, "no backend spans for the /__rum__ relay itself")
    check("demo_pass" not in raw, "no password anywhere in the exported telemetry")

    for message in passed:
        print("PASS", message)
    print(f"Collector received {len(spans)} spans, {len(metrics)} metric series, {len(logs)} log records.")
    print("Joined trace example:", trace_id)


if __name__ == "__main__":
    main(*sys.argv[1:4])
