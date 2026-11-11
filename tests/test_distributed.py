"""Transaction, service-boundary, compensation and enriched telemetry acceptance."""
import hashlib
from contextlib import closing
import hmac
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from support import create_app
from demostore import telemetry
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.exporter.otlp.proto.common._log_encoder import encode_logs
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
import requests

EXPECTED = {"normal": 200, "recovery": 200, "warning": 200, "slow": 200, "payment-degraded": 200,
            "retry": 200, "notification-warning": 200, "error": 500, "timeout": 502,
            "out-of-stock": 409, "card-declined": 402, "database-error": 503}


class DistributedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="demostore-distributed-")
        self.addCleanup(self.temporary.cleanup)
        self.exporter = InMemorySpanExporter()
        self.database = Path(self.temporary.name) / "lab.sqlite3"
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-only-key", "DATABASE": str(self.database),
            "TELEMETRY_ENABLED": True, "TELEMETRY_TEST_EXPORTER": self.exporter, "RELEASE_ID": "enriched-test"})
        self.addCleanup(telemetry.shutdown, self.app)
        self.client = self.app.test_client()
        self.csrf = self.client.get("/api/session").json["csrf_token"]
        response = self.post("/api/login", {"username": "demo_user", "password": "demo_pass", "traffic_type": "synthetic"})
        self.csrf = response.json["csrf_token"]
        self.marker = "stage4-0123456789abcdef"
        self.exporter.clear()

    def post(self, path, data=None):
        return self.client.post(path, json=data or {}, headers={"X-CSRF-Token": self.csrf,
            "X-Lab-Run-Id": getattr(self, "marker", "stage4-0123456789abcdef")})

    def checkout(self, scenario):
        self.post("/api/cart/clear")
        self.post("/api/cart/items", {"product_id": "laptop"})
        self.exporter.clear()
        return self.post("/api/checkout", {"scenario": scenario, "delay_ms": 150})

    def spans(self):
        return self.exporter.get_finished_spans()

    def logs(self):
        apps = [self.app, *self.app.extensions["demostore.test_dependencies"].values()]
        return [record for app in apps for record in app.extensions[telemetry.EXTENSION]["signals"].log_exporter.get_finished_logs()]

    def test_all_scenarios_keep_durable_ids_correct_orders_and_shared_control(self):
        before = self.client.get("/api/lab/control").json["control"]
        ids = set()
        for scenario, status in EXPECTED.items():
            with self.subTest(scenario=scenario):
                response = self.checkout(scenario)
                self.assertEqual(response.status_code, status)
                item = response.json["transaction"]
                self.assertRegex(item["id"], r"^TXN-\d{8}$")
                self.assertNotIn(item["id"], ids)
                ids.add(item["id"])
                self.assertEqual(response.headers["X-Transaction-Id"], item["id"])
                self.assertEqual(response.headers["X-Trace-Id"], item["trace_id"])
                stored = self.client.get("/api/lab/transactions/" + item["id"]).json["transaction"]
                self.assertEqual(stored["outcome"], "success" if status == 200 else "rejected" if status < 500 else "error")
                self.assertEqual(stored["scenario"], scenario)
                cart = self.client.get("/api/cart").json["cart"]
                self.assertEqual(cart["item_count"], 0 if status == 200 else 1)
                self.assertEqual(stored["total_cents"], 99900)
                self.assertEqual(bool(stored["order_id"]), status == 200)
        self.assertEqual(self.client.get("/api/lab/control").json["control"], before)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_transaction").fetchone()[0], 12)
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_order").fetchone()[0], 7)
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_outbox").fetchone()[0], 7)
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_outbox WHERE state='pending_retry'").fetchone()[0], 1)

    def test_trace_has_four_services_real_http_boundaries_and_retained_business_events(self):
        response = self.checkout("normal")
        spans = self.spans()
        by_id = {span.context.span_id: span for span in spans}
        services = {span.resource.attributes["service.name"] for span in spans}
        self.assertEqual(services, {"demostore", "demostore-inventory", "demostore-payment", "demostore-bank"})
        for span in spans:
            self.assertEqual(f"{span.context.trace_id:032x}", response.headers["X-Trace-Id"])
            self.assertEqual(span.attributes["transaction.id"], response.json["transaction"]["id"])
            self.assertEqual(span.attributes["lab.traffic_type"], "synthetic")
            self.assertEqual(span.attributes["lab.validation_id"], self.marker)
            if span.kind == SpanKind.SERVER and span.resource.attributes["service.name"] != "demostore":
                self.assertEqual(by_id[span.parent.span_id].kind, SpanKind.CLIENT)
                self.assertTrue(span.parent.is_remote)
        self.assertTrue({"catalog.lookup", "pricing.quote", "tax.calculate", "inventory.reserve", "payment.authorize",
                         "bank.charge", "order.save", "notification.enqueue"}.issubset({span.name for span in spans}))
        events = {event.name for span in spans for event in span.events}
        self.assertTrue({"catalog.loaded", "tax.calculated", "inventory.reserved", "bank.approved", "order.persisted",
                         "notification.queued", "checkout.completed"}.issubset(events))

    def test_transaction_logs_are_readable_enriched_and_join_actual_span_ids(self):
        response = self.checkout("normal")
        transaction = response.json["transaction"]["id"]
        span_ids = {span.context.span_id for span in self.spans()}
        logs = [record.log_record for record in self.logs() if record.log_record.attributes.get("transaction.id") == transaction]
        self.assertGreaterEqual(len(logs), 10)
        for record in logs:
            self.assertIn(record.span_id, span_ids)
            self.assertEqual(f"{record.trace_id:032x}", response.headers["X-Trace-Id"])
            self.assertIn(transaction, record.body)
            self.assertGreater(record.timestamp, 0)
        summary = next(item for item in logs if item.event_name == "checkout.completed")
        self.assertIn("999.00 USD", summary.body)
        self.assertIn("products=laptop", summary.body)
        self.assertIn(response.json["order"]["id"], summary.body)

    def test_rejection_skips_payment_while_technical_error_releases_inventory(self):
        response = self.checkout("out-of-stock")
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("payment.authorize", {span.name for span in self.spans()})
        root = next(span for span in self.spans() if span.resource.attributes["service.name"] == "demostore" and span.kind == SpanKind.SERVER)
        self.assertEqual(root.status.status_code, StatusCode.UNSET)
        response = self.checkout("error")
        self.assertEqual(response.status_code, 500)
        self.assertIn("inventory.release", {span.name for span in self.spans()})
        self.assertNotIn("order.save", {span.name for span in self.spans()})
        self.assertTrue(any(event.name == "exception" and event.attributes.get("exception.message") == "Simulated payment application failure"
                            for span in self.spans() for event in span.events))

    def test_retry_retains_failed_attempt_and_success_without_duplicate_order(self):
        response = self.checkout("retry")
        self.assertEqual(response.status_code, 200)
        banks = [span for span in self.spans() if span.name == "bank.charge"]
        self.assertEqual(len(banks), 2)
        self.assertEqual({span.attributes["payment.attempt"] for span in banks}, {1, 2})
        self.assertEqual(sum(span.status.status_code == StatusCode.ERROR for span in banks), 1)
        self.assertEqual(len([span for span in self.spans() if span.name == "order.save"]), 1)
        self.assertIn("retry_succeeded", response.json["transaction"]["warnings"])

    def test_persistence_failure_voids_payment_releases_inventory_and_keeps_no_order(self):
        response = self.checkout("database-error")
        self.assertEqual(response.status_code, 503)
        self.assertTrue({"payment.void", "bank.void", "inventory.release"}.issubset({span.name for span in self.spans()}))
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_order").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_outbox").fetchone()[0], 0)

    def test_dependency_unavailable_is_controlled_and_recovery_succeeds(self):
        previous = self.app.config["DEPENDENCY_TRANSPORT"]
        def unavailable(*_):
            raise requests.ConnectionError("PRIVATE-NETWORK-DETAIL")
        self.app.config["DEPENDENCY_TRANSPORT"] = unavailable
        response = self.checkout("normal")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json["transaction"]["reason"], "inventory_unavailable")
        self.assertNotIn(b"PRIVATE-NETWORK-DETAIL", encode_spans(self.spans()).SerializeToString())
        self.app.config["DEPENDENCY_TRANSPORT"] = previous
        self.assertEqual(self.post("/api/checkout", {"scenario": "recovery"}).status_code, 200)

    def test_internal_signature_and_payload_validation(self):
        child = self.app.extensions["demostore.test_dependencies"]["inventory"].test_client()
        self.assertEqual(child.post("/reserve", json={}).status_code, 403)
        encoded = json.dumps({"transaction_id": "PRIVATE", "scenario": "normal"}).encode()
        signature = hmac.new(b"test-only-key", encoded, hashlib.sha256).hexdigest()
        self.assertEqual(child.post("/reserve", data=encoded, content_type="application/json",
                                   headers={"X-Demo-Internal-Signature": signature}).status_code, 400)

    def test_private_fields_are_not_smuggled_through_business_events_or_log_bodies(self):
        marker = "PRIVATE-CUSTOMER-CARD-DATA"
        self.post("/api/cart/items", {"product_id": "laptop"})
        self.client.post("/api/checkout?token=" + marker,
            json={"scenario": "warning", "customer": marker, "card": marker},
            headers={"X-CSRF-Token": self.csrf, "Authorization": marker})
        self.assertNotIn(marker.encode(), encode_spans(self.spans()).SerializeToString())
        self.assertNotIn(marker.encode(), encode_logs(self.logs()).SerializeToString())

    def test_manual_sessions_cannot_override_scenario_and_history_inputs_are_bounded(self):
        self.post("/api/logout")
        self.csrf = self.client.get("/api/session").json["csrf_token"]
        response = self.post("/api/login", {"username": "demo_user", "password": "demo_pass", "traffic_type": "manual"})
        self.csrf = response.json["csrf_token"]
        self.assertEqual(self.post("/api/checkout", {"scenario": "error"}).status_code, 403)
        self.assertEqual(self.client.get("/api/lab/transactions?limit=10000").status_code, 400)
        self.assertEqual(self.client.get("/api/lab/transactions/PRIVATE").status_code, 400)

    def test_business_metrics_are_useful_bounded_and_active_checkout_returns_to_zero(self):
        self.checkout("retry")
        root = self.app.extensions[telemetry.EXTENSION]["signals"]
        data = root.metric_reader.get_metrics_data()
        metrics = [metric for resource in data.resource_metrics for scope in resource.scope_metrics for metric in scope.metrics]
        self.assertTrue({"demostore.business.operations", "demostore.business.operation.duration", "demostore.checkout.active"}.issubset({m.name for m in metrics}))
        active = next(m for m in metrics if m.name == "demostore.checkout.active")
        self.assertFalse(active.data.is_monotonic)
        self.assertTrue(all(point.value == 0 for point in active.data.data_points))
        for metric in metrics:
            for point in metric.data.data_points:
                self.assertFalse({"transaction.id", "order.id", "lab.validation_id", "trace_id", "span_id"}.intersection(point.attributes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
