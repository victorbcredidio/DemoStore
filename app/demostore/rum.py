"""Browser RUM delivery: the observer bundle, its page tag and a same-origin OTLP relay.

This replaces the lab's NGINX layer (sub_filter injection plus the /__rum__ location rules):
- HTML pages rendered from base.html load /__rum__/rum-v1.js?v=<content hash> when enabled.
- POST /__rum__/v1/{traces,metrics,logs} is relayed to one fixed Collector URL per signal.
  JSON only, 256 KiB maximum, a per-client token bucket (10/s, burst 30), no browser
  headers forwarded, no redirects followed, a short upstream timeout.
Everything under /__rum__/ is excluded from backend traces and metrics (see telemetry.py).
"""
from collections import OrderedDict
import hashlib
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from flask import Response, request
from werkzeug.exceptions import RequestEntityTooLarge

PREFIX = "/__rum__/"
BUNDLE_PATH = PREFIX + "rum-v1.js"
LICENSES_PATH = PREFIX + "LICENSES.txt"
SIGNALS = ("traces", "metrics", "logs")
MAX_BODY = 256 * 1024
MAX_UPSTREAM_REPLY = 64 * 1024
RATE_PER_SECOND, BURST = 10.0, 30
UPSTREAM_TIMEOUT = 2.0
DEFAULT_DIR = Path(__file__).with_name("rum_dist")


def upstream_endpoints(environ=None):
    """One fixed URL per signal. DEMO_RUM_OTLP_ENDPOINT wins, then the backend's OTLP settings."""
    environ = os.environ if environ is None else environ
    rum_base = environ.get("DEMO_RUM_OTLP_ENDPOINT", "").rstrip("/")
    base = environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    endpoints = {}
    for signal in SIGNALS:
        if rum_base:
            url = f"{rum_base}/v1/{signal}"
        else:
            url = (environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
                   or f"{base or 'http://127.0.0.1:4318'}/v1/{signal}")
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"Invalid OTLP endpoint for RUM {signal}")
        endpoints[signal] = url
    return endpoints


class RateLimiter:
    """Token bucket per client address, bounded in memory. Per Gunicorn worker process."""
    def __init__(self, rate=RATE_PER_SECOND, burst=BURST, max_clients=1024, clock=time.monotonic):
        self.rate, self.burst, self.max_clients, self.clock = rate, burst, max_clients, clock
        self.buckets = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, key):
        with self.lock:
            now = self.clock()
            tokens, last = self.buckets.pop(key, (float(self.burst), now))
            tokens = min(float(self.burst), tokens + (now - last) * self.rate)
            allowed = tokens >= 1
            self.buckets[key] = (tokens - 1 if allowed else tokens, now)
            while len(self.buckets) > self.max_clients:
                self.buckets.popitem(last=False)
            return allowed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # A redirect becomes an HTTPError: the upstream stays fixed.


class Relay:
    def __init__(self, endpoints, timeout=UPSTREAM_TIMEOUT):
        self.endpoints = dict(endpoints)
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        # Keep Gunicorn threads available for shoppers if the Collector is slow.
        self.slots = threading.BoundedSemaphore(1)

    def send(self, signal, body):
        if not self.slots.acquire(timeout=1):
            return 503, b'{"error":"busy"}'
        try:
            upstream = urllib.request.Request(self.endpoints[signal], data=body, method="POST",
                                              headers={"Content-Type": "application/json"})
            try:
                with self.opener.open(upstream, timeout=self.timeout) as reply:
                    return reply.status, reply.read(MAX_UPSTREAM_REPLY)
            except urllib.error.HTTPError as error:
                with error:
                    return (error.code if 400 <= error.code < 600 else 502), error.read(MAX_UPSTREAM_REPLY)
            except (OSError, ValueError):
                return 502, b'{"error":"collector unavailable"}'
        finally:
            self.slots.release()


class Bundle:
    def __init__(self, directory):
        directory = Path(directory)
        self.script = (directory / "rum-v1.js").read_bytes()
        self.version = hashlib.sha256(self.script).hexdigest()[:16]
        licenses = directory / "LICENSES.txt"
        self.licenses = licenses.read_bytes() if licenses.is_file() else None


def plain(status, body=b""):
    return Response(body, status=status, content_type="application/json" if body else "text/plain")


def configure(app):
    """Register the RUM routes. When RUM is off (the default) every /__rum__/ path is 404."""
    state = {"bundle": None, "relay": None, "limiter": RateLimiter()}
    if app.config.get("RUM_ENABLED"):
        try:
            state["bundle"] = Bundle(app.config.get("RUM_DIR") or DEFAULT_DIR)
            state["relay"] = Relay(app.config.get("RUM_ENDPOINTS") or upstream_endpoints())
        except (OSError, ValueError) as error:
            app.logger.error("DEMO_RUM_ENABLED=1 but browser RUM can't start (%s); RUM stays off.",
                             type(error).__name__)
            state["bundle"] = state["relay"] = None
    app.extensions["demostore.rum"] = state

    def enabled():
        return state["bundle"] is not None

    @app.context_processor
    def rum_tag():
        if not enabled():
            return {"rum_script": None}
        return {"rum_script": f"{BUNDLE_PATH}?v={state['bundle'].version}", "release_id": app.config["RELEASE_ID"]}

    @app.get(BUNDLE_PATH)
    def rum_bundle():
        if not enabled():
            return plain(404)
        bundle = state["bundle"]
        response = Response(bundle.script, content_type="application/javascript; charset=utf-8")
        # The page always asks for ?v=<hash>; only that exact URL may be cached for long.
        response.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                             if request.args.get("v") == bundle.version else "no-cache")
        response.set_etag(bundle.version)
        return response.make_conditional(request)

    @app.get(LICENSES_PATH)
    def rum_licenses():
        if not enabled() or state["bundle"].licenses is None:
            return plain(404)
        return Response(state["bundle"].licenses, content_type="text/plain; charset=utf-8")

    @app.route(PREFIX + "v1/<signal>", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def rum_ingress(signal):
        if not enabled() or signal not in SIGNALS:
            return plain(404)
        if request.method != "POST":
            return plain(405)
        if not request.headers.get("Content-Type", "").lower().startswith("application/json"):
            return plain(415)
        if request.content_length is not None and request.content_length > MAX_BODY:
            return plain(413)
        if not state["limiter"].allow(request.remote_addr or "unknown"):
            return plain(429)
        request.max_content_length = MAX_BODY
        try:
            body = request.get_data(cache=False)
        except RequestEntityTooLarge:
            return plain(413)
        status, reply = state["relay"].send(signal, body)
        return Response(reply, status=status, content_type="application/json")

    @app.route(PREFIX + "<path:anything>", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    def rum_unknown(anything):
        return plain(404)
