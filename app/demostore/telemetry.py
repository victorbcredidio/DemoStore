"""Worker-local backend tracing and integration with metrics/structured logs."""
import atexit
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
import re
import socket
import time

from flask import current_app, has_request_context, request, session
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from .observability import BackendSignals
from .contracts import (SCENARIOS, OPERATIONS, RUN_ID, EVENTS, ERROR_MESSAGES, attributes as safe_attributes)

EXTENSION = "demostore.telemetry"
REQUEST_SPAN = "demostore.request_span"
NOOP = trace.NoOpTracerProvider().get_tracer("demostore.business")
BUSINESS_SPAN = ContextVar("demostore.business_span", default=None)
ALLOWED_RESOURCE = {"service.name", "service.namespace", "service.version", "service.instance.id",
                    "deployment.environment.name", "process.pid", "telemetry.sdk.name",
                    "telemetry.sdk.version", "telemetry.sdk.language", "app.component"}


class PrivacyExporter(SpanExporter):
    """Drop URLs, headers, SQL, bodies and exception details before OTLP encoding."""
    def __init__(self, delegate, routes):
        self.delegate = delegate
        self.routes = frozenset(routes)

    def sanitized(self, span):
        attributes = safe_attributes(span.attributes or {}, self.routes)
        route = attributes.get("http.route", "unmatched_route")
        if span.kind in (SpanKind.SERVER, SpanKind.CLIENT):
            method = attributes.get("http.request.method", attributes.get("http.method", "HTTP"))
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
                method = "HTTP"
            name = method + " " + route
        else:
            name = span.name if span.name in OPERATIONS else "backend.operation"
        events = []
        for event in span.events:
            if event.name in EVENTS or event.name == "exception":
                events.append(Event(event.name, safe_attributes(event.attributes or {}, self.routes), event.timestamp))
        resource = Resource({key: value for key, value in span.resource.attributes.items()
                             if key in ALLOWED_RESOURCE})
        return ReadableSpan(name=name, context=span.context, parent=span.parent, resource=resource,
                            attributes=attributes, events=events, links=(), kind=span.kind,
                            status=Status(span.status.status_code, span.status.description if span.status.description in ERROR_MESSAGES.values() else None), start_time=span.start_time,
                            end_time=span.end_time, instrumentation_scope=span.instrumentation_scope)

    def export(self, spans):
        return self.delegate.export(tuple(self.sanitized(span) for span in spans))

    def shutdown(self):
        return self.delegate.shutdown()

    def force_flush(self, timeout_millis=30000):
        return self.delegate.force_flush(timeout_millis)


def common_attributes():
    traffic = "unclassified"
    attributes = {}
    if has_request_context():
        attributes.update(request.environ.get("demostore.business", {}))
        traffic = session.get("traffic_type", request.environ.get("demostore.initial_traffic", "unclassified"))
        run_id = request.headers.get("X-Lab-Run-Id", "")
        if RUN_ID.fullmatch(run_id):
            attributes["lab.validation_id"] = run_id
        mode = request.environ.get("demostore.scenario")
        if mode in SCENARIOS:
            attributes["app.scenario"] = mode
    if traffic not in {"manual", "synthetic"}:
        traffic = "unclassified"
    attributes.update({"lab.traffic_type": traffic, "lab.synthetic": traffic == "synthetic"})
    return attributes


def annotate(attributes):
    current = trace.get_current_span()
    tracked = BUSINESS_SPAN.get()
    if tracked is not None and tracked.get_span_context().span_id == current.get_span_context().span_id:
        tracked.set_attributes(attributes)
    else:
        current.set_attributes(attributes)


def scenario(mode):
    if has_request_context():
        request.environ["demostore.scenario"] = mode
    annotate({"app.scenario": mode})


def error(span, error_type):
    span.set_status(Status(StatusCode.ERROR, ERROR_MESSAGES.get(error_type)))
    span.set_attributes({"error.type": error_type, "business.outcome": "error"})


def event(name, attributes=None):
    extension = current_app.extensions.get(EXTENSION)
    if extension:
        extension["signals"].log(name, {**common_attributes(), **(attributes or {})},
                                 trace.get_current_span())


def shutdown(app):
    extension = app.extensions.get(EXTENSION)
    if extension:
        extension["shutdown"]()
    for child in app.extensions.get("demostore.test_dependencies", {}).values():
        shutdown(child)


def record_checkout(response, status, started, span, exception=False):
    extension = current_app.extensions.get(EXTENSION)
    if not extension:
        return
    attributes = {**common_attributes(), "http.request.method": "POST", "http.route": "/api/checkout",
                  "http.response.status_code": status,
                  "checkout.outcome": "error" if status >= 500 else "rejected" if status >= 400 else "success"}
    if status >= 400:
        attributes.setdefault("error.type", "checkout_exception" if exception else "simulated_checkout_error" if status >= 500 else "checkout_rejected")
    if status == 200:
        body = response.get_json(silent=True)
        attributes["order.id"] = (body or {}).get("order", {}).get("id")
    extension["signals"].checkout(attributes, time.perf_counter() - started, span)


def enrich(values):
    if has_request_context():
        request.environ.setdefault("demostore.business", {}).update(values)
    annotate(values)


class BusinessSpan:
    """Keep business outcomes for metrics even when trace sampling is disabled."""
    def __init__(self, span):
        self.span, self.values = span, {}

    def __getattr__(self, name):
        return getattr(self.span, name)

    def set_attribute(self, key, value):
        self.values[key] = value
        self.span.set_attribute(key, value)

    def set_attributes(self, values):
        self.values.update(values)
        self.span.set_attributes(values)

    def set_status(self, status, description=None):
        if getattr(status, "status_code", status) == StatusCode.ERROR:
            self.values["business.outcome"] = "error"
        self.span.set_status(status, description)


@contextmanager
def operation(name, attributes=None, kind=SpanKind.INTERNAL):
    extension = current_app.extensions.get(EXTENSION)
    tracer = extension["tracer"] if extension else NOOP
    started = time.perf_counter()
    with tracer.start_as_current_span(name, kind=kind, record_exception=False, set_status_on_exception=False) as raw:
        span = BusinessSpan(raw)
        token = BUSINESS_SPAN.set(span)
        span.set_attributes({"app.operation": name, "business.outcome": "success",
                             **common_attributes(), **(attributes or {})})
        try:
            yield span
        except Exception as exc:
            reason = span.values.get("error.type", "checkout_exception")
            error(span, reason)
            span.add_event("exception", {"exception.type": type(exc).__name__,
                **({"exception.message": ERROR_MESSAGES[reason]} if reason in ERROR_MESSAGES else {})})
            raise
        finally:
            span.set_attributes(common_attributes())
            if extension and name in OPERATIONS:
                extension["signals"].business(name, span.values, time.perf_counter() - started)
            BUSINESS_SPAN.reset(token)



def traced(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with operation(name) as span:
                started = time.perf_counter()
                extension = current_app.extensions.get(EXTENSION)
                active = common_attributes()
                if name == "checkout" and extension:
                    extension["signals"].checkout_active(1, active)
                try:
                    response = function(*args, **kwargs)
                except Exception:
                    if name == "checkout":
                        record_checkout(None, 500, started, span, exception=True)
                    raise
                finally:
                    if name == "checkout" and extension:
                        extension["signals"].checkout_active(-1, active)
                status = response[1] if isinstance(response, tuple) else getattr(response, "status_code", 200)
                if isinstance(status, int) and status >= 500:
                    reason = common_attributes().get("failure.type", span.values.get("failure.type"))
                    error(span, reason if reason and reason != "none" else "simulated_checkout_error" if name == "checkout" else "http_server_error")
                if name == "checkout":
                    record_checkout(response[0] if isinstance(response, tuple) else response, status, started, span)
                return response
        return wrapped
    return decorate


def configure(app):
    if not app.config.get("TELEMETRY_ENABLED"):
        return
    if EXTENSION in app.extensions:
        return
    # Gunicorn does not preload the application: create_app runs after the worker fork.
    resource = Resource.create({
        "service.name": app.config.get("SERVICE_NAME", os.environ.get("OTEL_SERVICE_NAME", "demostore")),
        "app.component": app.config.get("COMPONENT", "checkout"),
        "service.namespace": "user-experience-lab",
        "service.version": app.config["RELEASE_ID"],
        "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
        "deployment.environment.name": "lab",
        "process.pid": os.getpid(),
    })
    provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON), shutdown_on_exit=False)
    testing_exporter = app.config.get("TELEMETRY_TEST_EXPORTER") if app.testing else None
    exporter = testing_exporter or OTLPSpanExporter(
        endpoint=os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://127.0.0.1:4318/v1/traces"),
        timeout=float(os.environ.get("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "3")),
    )
    filtered = PrivacyExporter(exporter, (rule.rule for rule in app.url_map.iter_rules()))
    processor = SimpleSpanProcessor(filtered) if testing_exporter else BatchSpanProcessor(
        filtered, max_queue_size=512, max_export_batch_size=128, schedule_delay_millis=500,
        export_timeout_millis=4000,
    )
    provider.add_span_processor(processor)
    signals = BackendSignals(Resource({key: value for key, value in resource.attributes.items()
                                       if key in ALLOWED_RESOURCE}), app.config,
                             (rule.rule for rule in app.url_map.iter_rules()))
    closed = False
    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            signals.shutdown()
        finally:
            provider.shutdown()
    app.extensions[EXTENSION] = {"provider": provider, "tracer": provider.get_tracer("demostore.business"),
                                "signals": signals, "shutdown": close}
    atexit.register(close)

    def request_hook(span, environ):
        environ[REQUEST_SPAN] = span
        environ.setdefault("demostore.initial_traffic", session.get("traffic_type", "unclassified"))
        span.set_attributes(common_attributes())

    FlaskInstrumentor().instrument_app(
        app, tracer_provider=provider, meter_provider=NoOpMeterProvider(), request_hook=request_hook,
        excluded_urls=r"/healthz(?:\?|$)|/static/|/favicon\.ico(?:\?|$)", enable_commenter=False,
    )

    @app.before_request
    def begin_response():
        if request.path != "/healthz" and request.path != "/favicon.ico" and not request.path.startswith("/static/"):
            request.environ["demostore.request_started"] = time.perf_counter()

    @app.after_request
    def enrich_response(response):
        span = request.environ.get(REQUEST_SPAN)
        started = request.environ.get("demostore.request_started")
        if started is not None:
            method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "HTTP"
            attributes = {**common_attributes(), "http.request.method": method,
                          "http.route": request.url_rule.rule if request.url_rule else "unmatched_route",
                          "http.response.status_code": response.status_code}
            duration = time.perf_counter() - started
            signals.response(attributes, duration)
            signals.log("http.request.completed", {**attributes, "http.response.duration_ms": round(duration * 1000, 3)}, span)
        if span is not None and span.is_recording():
            span.set_attributes(common_attributes())
            # Record the actual Flask response, including redirects and error handlers.
            # Keep the legacy key for existing queries while exporting the stable key.
            # These HTTP attributes are separate from the span's UNSET/ERROR status.
            span.set_attribute("http.response.status_code", response.status_code)
            span.set_attribute("http.status_code", response.status_code)
            if response.status_code >= 500:
                error(span, common_attributes().get("failure.type", "http_server_error"))
            response.headers["X-Trace-Id"] = f"{span.get_span_context().trace_id:032x}"
        transaction = common_attributes().get("transaction.id")
        if transaction:
            response.headers["X-Transaction-Id"] = transaction
        return response
