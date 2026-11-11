"""Validate traces, metrics, structured logs, privacy and faults without a network backend."""
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from demostore import telemetry
from support import create_app
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.exporter.otlp.proto.common.metrics_encoder import encode_metrics
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, LogRecordExportResult
from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult, PeriodicExportingMetricReader
from opentelemetry.sdk.trace.export import SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="demostore-apm-test-")
        self.addCleanup(self.temporary.cleanup)
        self.exporter = InMemorySpanExporter()
        self.config = {"TESTING": True, "SECRET_KEY": "test-only-key",
                       "DATABASE": str(Path(self.temporary.name) / "lab.sqlite3"),
                       "RELEASE_ID": "stage4-test", "TELEMETRY_ENABLED": True,
                       "TELEMETRY_TEST_EXPORTER": self.exporter}
        self.app = create_app(self.config)
        self.addCleanup(telemetry.shutdown, self.app)
        self.client = self.app.test_client()
        self.login("synthetic")
        self.exporter.clear()
        self.signals = self.app.extensions[telemetry.EXTENSION]["signals"]
        self.signals.log_exporter.clear()

    def login(self, traffic):
        self.token = self.client.get("/api/session").json["csrf_token"]
        response = self.post("/api/login", {"username": "demo_user", "password": "demo_pass", "traffic_type": traffic})
        self.assertEqual(response.status_code, 200)
        self.token = response.json["csrf_token"]

    def post(self, path, body=None, headers=None):
        return self.client.post(path, json=body or {}, headers={"X-CSRF-Token": self.token, **(headers or {})})

    def prepare(self, mode="normal", delay=150):
        self.post("/api/lab/control", {"mode": mode, "delay_ms": delay})
        self.post("/api/cart/items", {"product_id": "laptop"})
        self.exporter.clear()

    def spans(self):
        return list(self.exporter.get_finished_spans())

    def named(self, name):
        return next(span for span in self.spans() if span.name == name)

    def logs(self):
        return [item.log_record for item in self.signals.log_exporter.get_finished_logs()]

    def metrics(self):
        data = self.signals.metric_reader.get_metrics_data()
        return [metric for resource in data.resource_metrics for scope in resource.scope_metrics
                for metric in scope.metrics] if data else []

    def checkout_points(self, name):
        return next(metric for metric in self.metrics() if metric.name == name).data.data_points

    def test_checkout_metrics_and_logs_match_trace_and_response(self):
        self.prepare()
        response = self.post("/api/checkout")
        checkout, server = self.named("checkout"), self.named("POST /api/checkout")
        log = next(item for item in self.logs() if item.event_name == "checkout.completed")
        self.assertEqual(log.trace_id, checkout.context.trace_id)
        self.assertEqual(log.span_id, checkout.context.span_id)
        self.assertEqual(log.attributes["order.id"], response.json["order"]["id"])
        self.assertEqual(log.attributes["http.response.status_code"], 200)
        self.assertEqual(log.severity_text, "INFO")
        self.assertGreaterEqual(log.timestamp, checkout.start_time)
        self.assertLessEqual(log.timestamp, checkout.end_time)
        encoded = encode_logs(self.signals.log_exporter.get_finished_logs())
        for resource in encoded.resource_logs:
            for scope in resource.scope_logs:
                for item in scope.log_records:
                    self.assertGreater(item.time_unix_nano, 1_700_000_000_000_000_000)
                    self.assertEqual(item.time_unix_nano, item.observed_time_unix_nano)
        request_log = next(item for item in self.logs() if item.event_name == "http.request.completed"
                           and item.attributes["http.route"] == "/api/checkout")
        self.assertEqual(request_log.span_id, server.context.span_id)
        for name, unit in (("demostore.checkout.count", "{checkout}"),
                           ("demostore.checkout.duration", "s")):
            metric = next(item for item in self.metrics() if item.name == name)
            self.assertEqual(metric.unit, unit)
            self.assertEqual(int(metric.data.aggregation_temporality), 2)
            point = metric.data.data_points[0]
            self.assertEqual(point.attributes["checkout.outcome"], "success")
            self.assertEqual(point.attributes["lab.traffic_type"], "synthetic")
            self.assertEqual(getattr(point, "value", getattr(point, "count", None)), 1)
        requests = next(item for item in self.metrics() if item.name == "demostore.http.server.requests")
        points = [point for point in requests.data.data_points if point.attributes["http.route"] == "/api/checkout"]
        self.assertEqual(sum(point.value for point in points), 1)

    def test_slow_checkout_is_measured_in_explicit_histogram_buckets(self):
        self.prepare("slow")
        self.post("/api/checkout")
        point = self.checkout_points("demostore.checkout.duration")[0]
        self.assertGreaterEqual(point.sum, 0.14)
        self.assertEqual(point.count, 1)
        self.assertEqual(sum(point.bucket_counts), point.count)
        self.assertGreaterEqual(point.explicit_bounds[-1], 10)
        self.assertEqual(len(point.bucket_counts), len(point.explicit_bounds) + 1)
        self.assertEqual(point.attributes["app.scenario"], "slow")

    def test_error_recovery_and_rejection_have_distinct_metrics_and_log_severity(self):
        self.prepare("error")
        self.assertEqual(self.post("/api/checkout").status_code, 500)
        self.post("/api/lab/control", {"mode": "recovery"})
        self.assertEqual(self.post("/api/checkout").status_code, 200)
        self.assertEqual(self.post("/api/checkout").status_code, 400)
        points = self.checkout_points("demostore.checkout.count")
        self.assertEqual({point.attributes["checkout.outcome"]: point.value for point in points},
                         {"success": 1, "error": 1, "rejected": 1})
        logs = {item.event_name: item for item in self.logs() if item.event_name.startswith("checkout.")}
        self.assertEqual(logs["checkout.failed"].severity_text, "ERROR")
        self.assertNotIn("order.id", logs["checkout.failed"].attributes)
        self.assertEqual(logs["checkout.completed"].severity_text, "INFO")
        self.assertEqual(logs["checkout.rejected"].severity_text, "WARN")
        self.assertTrue(any(item.event_name == "lab.mode.changed" and item.attributes["app.scenario"] == "recovery"
                            for item in self.logs()))

    def test_logs_and_metrics_protobuf_exclude_private_data_and_unbounded_labels(self):
        marker = "PRIVATE-LOG-METRIC-7e3ac9"
        self.client.set_cookie("private-cookie", marker)
        for index in range(12):
            self.client.get("/" + marker + str(index) + "?password=" + marker,
                            headers={"Authorization": "Bearer " + marker,
                                     "X-Lab-Run-Id": "stage4-" + f"{index:016x}"})
        self.signals.log("checkout.completed", {"order.id": marker, "http.route": marker,
            "lab.validation_id": marker, "error.type": marker, "password": marker, "http.url": marker})
        self.signals.log(marker, {"password": marker})
        logs = encode_logs(self.signals.log_exporter.get_finished_logs()).SerializeToString()
        data = self.signals.metric_reader.get_metrics_data()
        metrics = encode_metrics(data).SerializeToString()
        for encoded in (logs, metrics):
            self.assertNotIn(marker.encode(), encoded)
            self.assertNotIn(b"demo_pass", encoded)
            self.assertNotIn(b"demostore_session", encoded)
        requests = next(metric for resource in data.resource_metrics for scope in resource.scope_metrics
                        for metric in scope.metrics if metric.name == "demostore.http.server.requests")
        missing = [point for point in requests.data.data_points if point.attributes["http.route"] == "unmatched_route"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].value, 12)
        self.assertTrue(missing[0].attributes["lab.validation"])
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        self.assertFalse({"lab.validation_id", "order.id", "trace_id", "span_id"}.intersection(point.attributes))
                        self.assertFalse(point.exemplars)

    def test_unsampled_request_still_records_metrics_and_correlated_log(self):
        trace_id = "a" * 32
        response = self.client.get("/api/products", headers={"traceparent": f"00-{trace_id}-{'b' * 16}-00"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.spans(), [])
        log = next(item for item in self.logs() if item.event_name == "http.request.completed")
        self.assertEqual(f"{log.trace_id:032x}", trace_id)
        self.assertNotEqual(log.span_id, 0)
        requests = next(item for item in self.metrics() if item.name == "demostore.http.server.requests")
        self.assertEqual(sum(point.value for point in requests.data.data_points
                             if point.attributes["http.route"] == "/api/products"), 1)

    def test_health_and_static_do_not_generate_metrics_or_application_logs(self):
        before = self.signals.metric_reader.get_metrics_data()
        self.client.get("/healthz")
        self.client.get("/favicon.ico")
        response = self.client.get("/static/store.css")
        response.close()
        self.assertEqual(self.logs(), [])
        after = self.signals.metric_reader.get_metrics_data()
        def counts(data):
            return sorted((metric.name, tuple(sorted(point.attributes.items())), getattr(point, "value", None),
                           getattr(point, "count", None), getattr(point, "sum", None))
                          for resource in data.resource_metrics for scope in resource.scope_metrics
                          for metric in scope.metrics for point in metric.data.data_points)
        # Compare values, since collection timestamps naturally change.
        self.assertEqual(counts(before), counts(after))
        self.assertNotIn(b"/healthz", encode_metrics(after).SerializeToString())

    def test_providers_are_isolated_and_shutdown_emits_one_lifecycle_event(self):
        other = create_app(self.config)
        self.addCleanup(telemetry.shutdown, other)
        signals = other.extensions[telemetry.EXTENSION]["signals"]
        self.assertIsNot(signals.meter_provider, self.signals.meter_provider)
        self.assertIsNot(signals.logger_provider, self.signals.logger_provider)
        other.test_client().get("/api/session")
        self.assertEqual(len(signals.log_exporter.get_finished_logs()), 2)
        self.assertEqual(self.logs(), [])
        telemetry.shutdown(other)
        telemetry.shutdown(other)
        events = [item.log_record.event_name for item in signals.log_exporter.get_finished_logs()]
        self.assertEqual(events.count("application.started"), 1)
        self.assertEqual(events.count("application.stopping"), 1)

    def test_metric_and_log_export_failures_do_not_fail_checkout(self):
        class FailedMetrics(MetricExporter):
            calls = 0
            def export(self, metrics_data, timeout_millis=10000, **kwargs):
                self.calls += 1
                return MetricExportResult.FAILURE
            def shutdown(self, timeout_millis=30000, **kwargs):
                pass
            def force_flush(self, timeout_millis=10000):
                return False
        class FailedLogs(InMemoryLogRecordExporter):
            calls = 0
            def export(self, batch):
                self.calls += 1
                return LogRecordExportResult.FAILURE
        metrics, logs = FailedMetrics(), FailedLogs()
        reader = PeriodicExportingMetricReader(metrics, export_interval_millis=math.inf)
        other = create_app({**self.config, "TELEMETRY_TEST_METRIC_READER": reader,
                            "TELEMETRY_TEST_LOG_EXPORTER": logs})
        self.addCleanup(telemetry.shutdown, other)
        self.client = other.test_client()
        self.login("synthetic")
        self.post("/api/cart/items", {"product_id": "laptop"})
        self.assertEqual(self.post("/api/checkout").status_code, 200)
        reader.collect()
        self.assertGreater(metrics.calls, 0)
        self.assertGreater(logs.calls, 0)

    def test_unexpected_checkout_exception_log_does_not_export_exception_message(self):
        marker = "PRIVATE-EXCEPTION-MESSAGE"
        self.prepare("slow")
        with patch("demostore.time.sleep", side_effect=RuntimeError(marker)):
            with self.assertRaises(RuntimeError):
                self.post("/api/checkout")
        log = next(item for item in self.logs() if item.event_name == "checkout.failed")
        self.assertEqual(log.attributes["error.type"], "checkout_exception")
        self.assertEqual(log.severity_text, "ERROR")
        self.assertNotIn(marker.encode(), encode_logs(self.signals.log_exporter.get_finished_logs()).SerializeToString())

    def assert_http_status(self, response, expected, expected_span_status):
        self.assertEqual(response.status_code, expected)
        server = next(span for span in self.spans() if span.kind == SpanKind.SERVER
                      and span.resource.attributes["service.name"] == "demostore")
        self.assertEqual(server.status.status_code, expected_span_status)
        self.assertEqual(response.headers["X-Trace-Id"], f"{server.context.trace_id:032x}")
        # Verify numeric OTLP values after privacy filtering and protobuf encoding.
        message = encode_spans([server])
        encoded = message.resource_spans[0].scope_spans[0].spans[0]
        attributes = {item.key: item.value for item in encoded.attributes}
        for key in ("http.response.status_code", "http.status_code"):
            self.assertEqual(server.attributes[key], expected)
            self.assertEqual(attributes[key].WhichOneof("value"), "int_value")
            self.assertEqual(attributes[key].int_value, expected)

    def test_http_response_codes_are_exported_for_success_redirect_and_client_errors(self):
        anonymous = self.app.test_client()
        for method, path, expected in (("GET", "/api/session", 200), ("GET", "/", 302),
                                       ("GET", "/api/cart", 401), ("GET", "/missing-route", 404),
                                       ("DELETE", "/api/cart", 405)):
            with self.subTest(status=expected):
                self.exporter.clear()
                response = anonymous.open(path, method=method, follow_redirects=False)
                self.assert_http_status(response, expected, StatusCode.UNSET)

    def test_http_500_code_is_exported_separately_from_error_span_status(self):
        self.prepare("error")
        self.assert_http_status(self.post("/api/checkout"), 500, StatusCode.ERROR)
        for span in self.spans():
            if span.kind == SpanKind.INTERNAL:
                self.assertNotIn("http.response.status_code", span.attributes)
                self.assertNotIn("http.status_code", span.attributes)

    def test_normal_checkout_has_parent_child_tree_and_response_trace_id(self):
        self.prepare()
        response = self.post("/api/checkout")
        self.assertEqual(response.status_code, 200)
        server, checkout = self.named("POST /api/checkout"), self.named("checkout")
        self.assertEqual(server.kind, SpanKind.SERVER)
        self.assertEqual(response.headers["X-Trace-Id"], f"{server.context.trace_id:032x}")
        self.assertEqual(checkout.parent.span_id, server.context.span_id)
        for name in ("cart.validate", "order.save"):
            child = self.named(name)
            self.assertEqual(child.parent.span_id, checkout.context.span_id)
            self.assertEqual(child.context.trace_id, server.context.trace_id)
        self.assertEqual(checkout.attributes["cart.total_cents"], 99900)
        self.assertEqual(checkout.attributes["order.id"], response.json["order"]["id"])
        for span in self.spans():
            self.assertTrue(span.resource.attributes["service.name"].startswith("demostore"))
            self.assertEqual(span.attributes["lab.traffic_type"], "synthetic")

    def test_slow_mode_contains_measured_delay_span(self):
        self.prepare("slow")
        self.assertEqual(self.post("/api/checkout").status_code, 200)
        delay = self.named("checkout.simulated_delay")
        self.assertGreaterEqual((delay.end_time - delay.start_time) / 1e6, 140)
        self.assertEqual(delay.attributes["demo.delay_ms"], 150)
        self.assertEqual(delay.attributes["app.scenario"], "slow")

    def test_error_mode_marks_server_and_checkout_without_saving_order(self):
        self.prepare("error")
        self.assertEqual(self.post("/api/checkout").status_code, 500)
        self.assertEqual(self.named("POST /api/checkout").status.status_code, StatusCode.ERROR)
        self.assertEqual(self.named("checkout").status.status_code, StatusCode.ERROR)
        self.assertNotIn("order.save", [span.name for span in self.spans()])
        self.assertEqual(self.named("checkout").attributes["app.scenario"], "error")

    def test_recovery_records_success_using_retained_cart(self):
        self.prepare("error")
        self.post("/api/checkout")
        self.post("/api/lab/control", {"mode": "recovery"})
        self.exporter.clear()
        self.assertEqual(self.post("/api/checkout").status_code, 200)
        self.assertEqual(self.named("checkout").attributes["app.scenario"], "recovery")
        self.assertNotEqual(self.named("checkout").status.status_code, StatusCode.ERROR)
        self.named("order.save")

    def test_manual_and_logout_requests_preserve_classification(self):
        self.post("/api/logout")
        self.login("manual")
        self.exporter.clear()
        self.post("/api/logout")
        self.assertEqual(self.named("auth.logout").attributes["lab.traffic_type"], "manual")
        self.assertEqual(self.named("POST /api/logout").attributes["lab.traffic_type"], "manual")

    def test_w3c_traceparent_is_extracted(self):
        trace_id, parent_id = "1234567890abcdef1234567890abcdef", "1234567890abcdef"
        response = self.client.get("/api/cart", headers={"traceparent": f"00-{trace_id}-{parent_id}-01"})
        server = self.named("GET /api/cart")
        self.assertEqual(f"{server.context.trace_id:032x}", trace_id)
        self.assertEqual(f"{server.parent.span_id:016x}", parent_id)
        self.assertTrue(server.parent.is_remote)
        self.assertEqual(response.headers["X-Trace-Id"], trace_id)

    def test_health_and_static_requests_do_not_export_spans(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        response = self.client.get("/static/store.css")
        response.close()
        self.assertEqual(self.spans(), [])

    def test_exported_protobuf_omits_credentials_headers_queries_sql_and_exception_details(self):
        marker = "PRIVATE-DO-NOT-EXPORT-7e3ac9"
        self.client.set_cookie("private-cookie", marker)
        self.client.get("/api/products?password=" + marker, headers={"Authorization": "Bearer " + marker})
        with self.app.test_request_context("/api/products?token=" + marker):
            with telemetry.operation(marker) as span:
                span.set_attribute("http.url", "http://localhost/?password=" + marker)
                span.set_attribute("db.statement", "SELECT '" + marker + "'")
                span.set_attribute("http.request.header.authorization", marker)
                span.add_event("exception", {"exception.type": "ValueError", "exception.message": marker,
                                              "exception.stacktrace": marker})
                span.set_status(Status(StatusCode.ERROR, marker))
        encoded = encode_spans(self.spans()).SerializeToString()
        self.assertNotIn(marker.encode(), encoded)
        self.assertNotIn(b"demo_pass", encoded)
        self.assertNotIn(b"demostore_session", encoded)
        self.assertIn("backend.operation", [span.name for span in self.spans()])

    def test_unmatched_route_does_not_export_sensitive_path_in_span_name(self):
        marker = "PRIVATE-MISSING-ROUTE"
        self.client.get("/" + marker + "?token=" + marker)
        self.assertEqual(self.spans()[0].name, "GET unmatched_route")
        self.assertNotIn(marker.encode(), encode_spans(self.spans()).SerializeToString())

    def test_validation_marker_accepts_only_bounded_hex_run_id(self):
        run_id = "stage4-0123456789abcdef"
        self.client.get("/api/cart", headers={"X-Lab-Run-Id": run_id})
        self.assertEqual(self.named("GET /api/cart").attributes["lab.validation_id"], run_id)
        self.exporter.clear()
        self.client.get("/api/cart", headers={"X-Lab-Run-Id": "private-user-data"})
        self.assertNotIn("lab.validation_id", self.named("GET /api/cart").attributes)

    def test_telemetry_can_be_disabled(self):
        other = create_app({**self.config, "TELEMETRY_ENABLED": False})
        self.assertNotIn(telemetry.EXTENSION, other.extensions)
        self.assertNotIn("X-Trace-Id", other.test_client().get("/api/session").headers)
        self.assertEqual(self.spans(), [])

    def test_configure_twice_does_not_duplicate_request_spans(self):
        telemetry.configure(self.app)
        self.client.get("/api/cart")
        self.assertEqual([span.name for span in self.spans()], ["GET /api/cart"])

    def test_export_failure_does_not_fail_checkout(self):
        class FailedExporter(InMemorySpanExporter):
            def export(self, spans):
                return SpanExportResult.FAILURE
        other = create_app({**self.config, "TELEMETRY_TEST_EXPORTER": FailedExporter()})
        self.addCleanup(telemetry.shutdown, other)
        self.client = other.test_client()
        self.login("synthetic")
        self.post("/api/cart/items", {"product_id": "laptop"})
        self.assertEqual(self.post("/api/checkout").status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
