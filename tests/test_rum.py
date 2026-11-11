"""Browser RUM delivery: page tag, bundle, same-origin OTLP relay limits and trace join."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from demostore import rum, telemetry
from support import create_app

BUNDLE = b"/* fake observer */ window.DemoStoreRUM = {};"


class FakeCollector:
    """A real HTTP listener, so the relay is exercised over TCP like in production."""
    def __init__(self):
        self.received, self.reply = [], (200, b'{"partialSuccess":{}}', {})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                owner.received.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, payload, headers = owner.reply
                self.send_response(status)
                for key, value in {"Content-Type": "application/json", **headers}.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class RumTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="demostore-rum-test-")
        self.addCleanup(self.temporary.cleanup)
        directory = Path(self.temporary.name)
        (directory / "dist").mkdir()
        (directory / "dist" / "rum-v1.js").write_bytes(BUNDLE)
        (directory / "dist" / "LICENSES.txt").write_text("=== example@1.0.0 ===\nMIT\n")
        self.collector = FakeCollector()
        self.addCleanup(self.collector.close)
        self.config = {"TESTING": True, "SECRET_KEY": "test-only-key", "RELEASE_ID": "rel-123",
                       "DATABASE": str(directory / "lab.sqlite3"),
                       "RUM_ENABLED": True, "RUM_DIR": str(directory / "dist"),
                       "RUM_ENDPOINTS": {s: f"{self.collector.url}/v1/{s}" for s in rum.SIGNALS}}

    def app(self, **overrides):
        app = create_app({**self.config, **overrides})
        self.addCleanup(telemetry.shutdown, app)
        return app

    def login(self, client, traffic="manual"):
        token = client.get("/api/session").json["csrf_token"]
        response = client.post("/api/login", json={"username": "demo_user", "password": "demo_pass",
                               "traffic_type": traffic}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        return response.json["csrf_token"]

    def send(self, client, signal="traces", body=b'{"resourceSpans":[]}', content_type="application/json", **kw):
        return client.post(f"/__rum__/v1/{signal}", data=body, headers={"Content-Type": content_type, **kw})

    # --- page tag ---------------------------------------------------------------------------
    def test_disabled_by_default_no_tag_and_every_rum_path_is_404(self):
        client = self.app(RUM_ENABLED=False).test_client()
        self.assertNotIn(b"__rum__", client.get("/login").data)
        self.assertEqual(client.get("/__rum__/rum-v1.js").status_code, 404)
        self.assertEqual(self.send(client).status_code, 404)
        self.assertEqual(self.collector.received, [])

    def test_html_pages_load_the_versioned_bundle_once_before_store_js(self):
        app = self.app()
        client = app.test_client()
        version = app.extensions["demostore.rum"]["bundle"].version
        for path in ("/login", "/login?traffic=synthetic"):
            html = client.get(path).get_data(as_text=True)
            self.assertEqual(html.count("/__rum__/rum-v1.js"), 1, path)
            self.assertIn(f'src="/__rum__/rum-v1.js?v={version}" defer', html)
            self.assertIn('<meta name="demostore-release" content="rel-123">', html)
            self.assertLess(html.index("/__rum__/rum-v1.js"), html.index("store.js"))
        self.login(client)
        self.assertEqual(client.get("/").get_data(as_text=True).count("/__rum__/rum-v1.js"), 1)

    def test_no_tag_in_json_health_or_static_responses(self):
        client = self.app().test_client()
        self.login(client)
        for path in ("/api/session", "/api/cart", "/healthz", "/static/store.js", "/static/store.css"):
            with client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)
                self.assertNotIn(b"__rum__", response.data, path)

    def test_enabled_without_a_bundle_starts_with_rum_off(self):
        app = self.app(RUM_DIR=str(Path(self.temporary.name) / "missing"))
        client = app.test_client()
        self.assertNotIn(b"__rum__", client.get("/login").data)
        self.assertEqual(self.send(client).status_code, 404)
        self.login(client)
        self.assertFalse(client.get("/api/lab/status").json["rum_enabled"])

    # --- bundle -----------------------------------------------------------------------------
    def test_bundle_is_javascript_cached_only_for_its_own_hash(self):
        app = self.app()
        client = app.test_client()
        version = app.extensions["demostore.rum"]["bundle"].version
        hashed = client.get(f"/__rum__/rum-v1.js?v={version}")
        self.assertEqual(hashed.data, BUNDLE)
        self.assertTrue(hashed.content_type.startswith("application/javascript"))
        self.assertEqual(hashed.headers["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertEqual(hashed.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(client.get("/__rum__/rum-v1.js?v=stale").headers["Cache-Control"], "no-cache")
        revalidated = client.get("/__rum__/rum-v1.js", headers={"If-None-Match": f'"{version}"'})
        self.assertEqual(revalidated.status_code, 304)
        self.assertIn(b"MIT", client.get("/__rum__/LICENSES.txt").data)

    # --- ingress ----------------------------------------------------------------------------
    def test_relay_forwards_body_to_the_fixed_collector_path_without_browser_headers(self):
        client = self.app().test_client()
        self.login(client)
        for signal in rum.SIGNALS:
            body = b'{"signal":"%s"}' % signal.encode()
            response = self.send(client, signal, body, "application/json; charset=utf-8",
                                 **{"X-Forwarded-Host": "evil.example", "Authorization": "Bearer x"})
            self.assertEqual((response.status_code, response.data), (200, b'{"partialSuccess":{}}'))
            received = self.collector.received[-1]
            self.assertEqual((received["path"], received["body"]), (f"/v1/{signal}", body))
            self.assertEqual(received["headers"]["Content-Type"], "application/json")
            for header in ("Cookie", "Authorization", "X-Forwarded-Host", "Referer"):
                self.assertNotIn(header, received["headers"])

    def test_ingress_status_codes(self):
        client = self.app().test_client()
        self.assertEqual(client.get("/__rum__/v1/traces").status_code, 405)
        self.assertEqual(self.send(client, content_type="text/plain").status_code, 415)
        self.assertEqual(self.send(client, content_type="application/x-protobuf").status_code, 415)
        self.assertEqual(self.send(client, body=b"x" * (rum.MAX_BODY + 1)).status_code, 413)
        self.assertEqual(self.send(client, "profiles").status_code, 404)
        self.assertEqual(client.post("/__rum__/v2/traces").status_code, 404)
        self.assertEqual(client.get("/__rum__/anything/else").status_code, 404)
        self.assertEqual(self.collector.received, [])
        self.assertEqual(self.send(client, body=b"x" * rum.MAX_BODY).status_code, 200)  # exactly at the cap

    def test_upstream_errors_are_passed_through_and_redirects_are_not_followed(self):
        client = self.app().test_client()
        self.collector.reply = (400, b'{"error":"bad payload"}', {})
        self.assertEqual(self.send(client).status_code, 400)
        self.collector.reply = (503, b'{}', {})
        self.assertEqual(self.send(client).status_code, 503)
        self.collector.reply = (307, b'', {"Location": "http://elsewhere.example/v1/traces"})
        self.assertEqual(self.send(client).status_code, 502)
        self.assertEqual(len(self.collector.received), 3)

    def test_unreachable_collector_is_502_and_shopping_still_works(self):
        dead = f"http://127.0.0.1:{closed_port()}"
        client = self.app(RUM_ENDPOINTS={s: f"{dead}/v1/{s}" for s in rum.SIGNALS}).test_client()
        self.assertEqual(self.send(client).status_code, 502)
        token = self.login(client)
        self.assertEqual(client.post("/api/cart/items", json={"product_id": "laptop"},
                                     headers={"X-CSRF-Token": token}).status_code, 200)

    def test_rate_limit_returns_429_after_the_burst(self):
        app = self.app()
        app.extensions["demostore.rum"]["limiter"] = rum.RateLimiter(clock=lambda: 0.0)
        client = app.test_client()
        statuses = [self.send(client).status_code for _ in range(rum.BURST + 1)]
        self.assertEqual(statuses[:rum.BURST], [200] * rum.BURST)
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(len(self.collector.received), rum.BURST)

    def test_token_bucket_refills_per_client_and_stays_bounded(self):
        now = [0.0]
        limiter = rum.RateLimiter(rate=10, burst=3, max_clients=2, clock=lambda: now[0])
        self.assertEqual([limiter.allow("a") for _ in range(4)], [True, True, True, False])
        self.assertTrue(limiter.allow("b"))
        now[0] = 0.1
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        limiter.allow("c")
        self.assertEqual(len(limiter.buckets), 2)

    def test_endpoint_derivation(self):
        self.assertEqual(rum.upstream_endpoints({})["traces"], "http://127.0.0.1:4318/v1/traces")
        base = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://host.containers.internal:4318/"}
        self.assertEqual(rum.upstream_endpoints(base)["logs"], "http://host.containers.internal:4318/v1/logs")
        specific = {**base, "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://c:4318/custom/metrics"}
        self.assertEqual(rum.upstream_endpoints(specific)["metrics"], "http://c:4318/custom/metrics")
        own = {**specific, "DEMO_RUM_OTLP_ENDPOINT": "http://rum-collector:4318"}
        self.assertEqual(rum.upstream_endpoints(own)["metrics"], "http://rum-collector:4318/v1/metrics")
        with self.assertRaises(ValueError):
            rum.upstream_endpoints({"DEMO_RUM_OTLP_ENDPOINT": "file:///etc/passwd"})

    # --- telemetry --------------------------------------------------------------------------
    def test_ingress_creates_no_backend_spans_or_request_metrics_and_checkout_joins_the_browser_trace(self):
        exporter = InMemorySpanExporter()
        app = self.app(TELEMETRY_ENABLED=True, TELEMETRY_TEST_EXPORTER=exporter)
        signals = app.extensions[telemetry.EXTENSION]["signals"]
        client = app.test_client()
        token = self.login(client, "synthetic")
        exporter.clear()
        signals.log_exporter.clear()

        self.assertEqual(self.send(client).status_code, 200)
        self.assertEqual(client.get("/__rum__/rum-v1.js").status_code, 200)
        self.assertEqual(exporter.get_finished_spans(), ())
        logged = [item.log_record.attributes.get("http.route") for item in signals.log_exporter.get_finished_logs()]
        self.assertFalse([route for route in logged if route and "__rum__" in route])

        # The browser's fetch wrapper sends a sampled W3C traceparent on /api/checkout.
        trace_id, parent = "4bf92f3577b34da6a3ce929d0e0e4736", "00f067aa0ba902b7"
        client.post("/api/cart/items", json={"product_id": "laptop"}, headers={"X-CSRF-Token": token})
        response = client.post("/api/checkout", json={}, headers={"X-CSRF-Token": token,
                               "traceparent": f"00-{trace_id}-{parent}-01"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Trace-Id"], trace_id)
        server = next(s for s in exporter.get_finished_spans() if s.name == "POST /api/checkout")
        self.assertEqual(f"{server.context.trace_id:032x}", trace_id)
        self.assertEqual(f"{server.parent.span_id:016x}", parent)
        self.assertEqual(server.attributes["lab.traffic_type"], "synthetic")
        services = {s.resource.attributes["service.name"] for s in exporter.get_finished_spans()
                    if f"{s.context.trace_id:032x}" == trace_id}
        self.assertTrue({"demostore", "demostore-payment", "demostore-bank"} <= services, services)


if __name__ == "__main__":
    unittest.main(verbosity=2)
