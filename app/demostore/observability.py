"""Worker-local metrics and fixed, structured application log events."""
import logging
import math
import os
import re
import threading
import time

from opentelemetry import trace
from opentelemetry._logs import SeverityNumber
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import NoOpMeterProvider
from opentelemetry.sdk._logs import LoggerProvider, LogRecordLimits
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor, InMemoryLogRecordExporter, SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import AlwaysOffExemplarFilter, Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import (
    AggregationTemporality, InMemoryMetricReader, PeriodicExportingMetricReader,
)
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from .contracts import (METRIC_KEYS, EVENTS, WARNING_EVENTS, attributes as safe_attributes, message)

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15)


class BackendSignals:
    """No global providers or root logging handler; every worker owns its exporters."""
    def __init__(self, resource, config, routes):
        self.routes = frozenset(routes)
        self._closed = False
        self._close_lock = threading.Lock()
        temporality = {Counter: AggregationTemporality.CUMULATIVE,
                       Histogram: AggregationTemporality.CUMULATIVE}
        reader = config.get("TELEMETRY_TEST_METRIC_READER") if config.get("TESTING") else None
        if reader is None:
            if config.get("TESTING"):
                reader = InMemoryMetricReader(preferred_temporality=temporality)
            else:
                reader = PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=os.environ.get("OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "http://127.0.0.1:4318/v1/metrics"),
                        timeout=float(os.environ.get("OTEL_EXPORTER_OTLP_METRICS_TIMEOUT", "3")),
                        preferred_temporality=temporality, meter_provider=NoOpMeterProvider(),
                    ), export_interval_millis=float(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "15000")),
                    export_timeout_millis=4000,
                )
        self.metric_reader = reader
        names = ("demostore.http.server.requests", "http.server.request.duration",
                 "demostore.checkout.count", "demostore.checkout.duration",
                 "demostore.business.operations", "demostore.business.operation.duration",
                 "demostore.business.warnings", "demostore.checkout.active")
        views = [View(instrument_name=name, attribute_keys=METRIC_KEYS,
                      aggregation_cardinality_limit=512,
                      **({"aggregation": ExplicitBucketHistogramAggregation(BUCKETS)}
                         if name.endswith("duration") else {})) for name in names]
        self.meter_provider = MeterProvider(resource=resource, metric_readers=[reader], views=views,
                                            exemplar_filter=AlwaysOffExemplarFilter(), shutdown_on_exit=False)
        meter = self.meter_provider.get_meter("demostore.backend")
        self.requests = meter.create_counter(names[0], unit="{request}", description="Completed Flask responses")
        self.request_duration = meter.create_histogram(names[1], unit="s", description="Flask request duration")
        self.checkouts = meter.create_counter(names[2], unit="{checkout}", description="Checkout business attempts by outcome")
        self.checkout_duration = meter.create_histogram(names[3], unit="s", description="Checkout business duration")
        self.operations = meter.create_counter("demostore.business.operations", unit="{operation}", description="Business steps by outcome")
        self.operation_duration = meter.create_histogram("demostore.business.operation.duration", unit="s", description="Business step duration")
        self.warnings = meter.create_counter("demostore.business.warnings", unit="{warning}", description="Business warnings")
        self.active = meter.create_up_down_counter("demostore.checkout.active", unit="{checkout}", description="Active checkout business attempts")

        exporter = config.get("TELEMETRY_TEST_LOG_EXPORTER") if config.get("TESTING") else None
        if exporter is None:
            exporter = InMemoryLogRecordExporter() if config.get("TESTING") else OTLPLogExporter(
                endpoint=os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://127.0.0.1:4318/v1/logs"),
                timeout=float(os.environ.get("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "3")),
                meter_provider=NoOpMeterProvider(),
            )
        self.log_exporter = exporter
        self.logger_provider = LoggerProvider(resource=resource, shutdown_on_exit=False,
            meter_provider=NoOpMeterProvider(), log_record_limits=LogRecordLimits(max_attributes=48, max_attribute_length=160))
        processor = SimpleLogRecordProcessor(exporter) if config.get("TESTING") else BatchLogRecordProcessor(
            exporter, max_queue_size=512, max_export_batch_size=128, schedule_delay_millis=500,
            export_timeout_millis=4000, meter_provider=NoOpMeterProvider(),
        )
        self.logger_provider.add_log_record_processor(processor)
        self.logger = self.logger_provider.get_logger("demostore.application")
        self.component = resource.attributes.get("app.component", "checkout")
        self.log("application.started", {"app.component": self.component})

    def attributes(self, values):
        """Accept known keys AND bounded values; never accept a free-form message/body."""
        values = safe_attributes(values, self.routes)
        traffic = values.get("lab.traffic_type", "unclassified")
        values["lab.traffic_type"] = traffic
        values["lab.synthetic"] = traffic == "synthetic"
        values["lab.validation"] = "lab.validation_id" in values
        return values

    def log(self, event, attributes=None, span=None):
        if event not in EVENTS:
            return
        attrs = self.attributes(attributes or {})
        status = attrs.get("http.response.status_code", 200)
        severity = SeverityNumber.ERROR if status >= 500 else SeverityNumber.WARN if status >= 400 or event in WARNING_EVENTS else SeverityNumber.INFO
        try:
            # Native Logger.emit leaves event time unset unless supplied. An OTLP zero
            # timestamp would put the record outside the lab's retention window.
            timestamp = time.time_ns()
            self.logger.emit(body=message(event, attrs), event_name=event, attributes=attrs,
                             timestamp=timestamp, observed_timestamp=timestamp,
                             severity_number=severity, severity_text=severity.name,
                             context=trace.set_span_in_context(getattr(span, "span", span)) if span is not None else None)
        except Exception:
            logging.getLogger(__name__).warning("DemoStore log recording failed")
        if event in WARNING_EVENTS:
            self.warnings.add(1, {key: value for key, value in attrs.items() if key in METRIC_KEYS})

    def business(self, name, attributes, duration):
        attrs = self.attributes({**attributes, "business.step": name})
        labels = {key: value for key, value in attrs.items() if key in METRIC_KEYS}
        self.operations.add(1, labels)
        self.operation_duration.record(duration, labels)

    def checkout_active(self, delta, attributes):
        attrs = self.attributes(attributes)
        self.active.add(delta, {key: value for key, value in attrs.items()
                                if key in {"lab.traffic_type", "lab.synthetic", "lab.validation"}})

    def response(self, attributes, duration):
        attrs = self.attributes(attributes)
        labels = {key: value for key, value in attrs.items() if key in METRIC_KEYS}
        try:
            self.requests.add(1, labels)
            self.request_duration.record(duration, labels)
        except Exception:
            logging.getLogger(__name__).warning("DemoStore metric recording failed")

    def checkout(self, attributes, duration, span):
        attrs = self.attributes(attributes)
        labels = {key: value for key, value in attrs.items() if key in METRIC_KEYS}
        try:
            self.checkouts.add(1, labels)
            self.checkout_duration.record(duration, labels)
        except Exception:
            logging.getLogger(__name__).warning("DemoStore metric recording failed")
        outcome = attrs["checkout.outcome"]
        event = {"success": "checkout.completed", "error": "checkout.failed", "rejected": "checkout.rejected"}[outcome]
        self.log(event, {**attributes, "checkout.duration_ms": round(duration * 1000, 3)}, span)

    def shutdown(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self.log("application.stopping", {"app.component": self.component})
        try:
            self.meter_provider.shutdown(timeout_millis=5000)
        finally:
            self.logger_provider.shutdown()
