"""Actual loopback HTTP boundaries for mock inventory, payment and bank services."""
import hashlib
import hmac
import json
import os
import time

from flask import Flask, current_app, jsonify, request
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
import requests

from . import telemetry
from .contracts import PORTS, SCENARIOS, TRANSACTION_ID, ERROR_MESSAGES, RUN_ID

PRICES = {"laptop": 99900, "headphones": 14900}


class DependencyFailure(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class PaymentApplicationError(Exception):
    """Deliberate lab exception; no request body or arbitrary message is recorded."""


def call(component, path, payload, timeout=3.0):
    """A real HTTP CLIENT span, signed body and propagated W3C context; no arbitrary URLs."""
    port = PORTS[component]
    attrs = {"http.request.method": "POST", "http.route": path,
             "server.address": "127.0.0.1", "server.port": port}
    with telemetry.operation("POST " + path, attrs, kind=SpanKind.CLIENT) as span:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(current_app.config["SECRET_KEY"].encode(), encoded, hashlib.sha256).hexdigest()
        headers = {"Content-Type": "application/json", "X-Demo-Internal-Signature": signature}
        if payload.get("validation_id"):
            headers["X-Lab-Run-Id"] = payload["validation_id"]
        propagate.inject(headers)
        try:
            if current_app.testing and current_app.config.get("DEPENDENCY_TRANSPORT"):
                status, body = current_app.config["DEPENDENCY_TRANSPORT"](component, path, encoded, headers, timeout)
            else:
                with requests.Session() as client:
                    client.trust_env = False
                    response = client.post(f"http://127.0.0.1:{port}{path}", data=encoded, headers=headers, timeout=timeout)
                    status, body = response.status_code, response.json()
            span.set_attributes({"http.response.status_code": status, "http.status_code": status})
            if status >= 400:
                telemetry.error(span, body.get("reason", "provider_unavailable"))
            return status, body
        except requests.Timeout:
            telemetry.error(span, "provider_timeout")
            span.add_event("exception", {"exception.type": "TimeoutError", "exception.message": ERROR_MESSAGES["provider_timeout"]})
            raise DependencyFailure("provider_timeout") from None
        except (requests.RequestException, ValueError):
            reason = {"inventory": "inventory_unavailable", "payment": "payment_unavailable", "bank": "provider_unavailable"}[component]
            telemetry.error(span, reason)
            span.add_event("exception", {"exception.type": "ConnectionError", "exception.message": ERROR_MESSAGES[reason]})
            raise DependencyFailure(reason) from None


def validate_payload(value):
    if not isinstance(value, dict) or not isinstance(value.get("transaction_id"), str) or not TRANSACTION_ID.fullmatch(value["transaction_id"]):
        raise ValueError("invalid_transaction")
    if value.get("scenario") not in SCENARIOS:
        raise ValueError("invalid_scenario")
    if type(value.get("delay_ms")) is not int or not 100 <= value["delay_ms"] <= 10000:
        raise ValueError("invalid_delay")
    items = value.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 2:
        raise ValueError("invalid_items")
    seen, total, quantity = set(), 0, 0
    for item in items:
        if not isinstance(item, dict) or item.get("product_id") not in PRICES or item["product_id"] in seen:
            raise ValueError("invalid_product")
        if type(item.get("quantity")) is not int or not 1 <= item["quantity"] <= 99:
            raise ValueError("invalid_quantity")
        seen.add(item["product_id"])
        total += PRICES[item["product_id"]] * item["quantity"]
        quantity += item["quantity"]
    if type(value.get("total_cents")) is not int or value["total_cents"] != total:
        raise ValueError("invalid_total")
    if value.get("traffic_type") not in {"manual", "synthetic"}:
        raise ValueError("invalid_traffic_type")
    marker = value.get("validation_id", "")
    if marker and (not isinstance(marker, str) or not RUN_ID.fullmatch(marker)):
        raise ValueError("invalid_validation_id")
    if type(value.get("attempt", 1)) is not int or not 1 <= value.get("attempt", 1) <= 2:
        raise ValueError("invalid_attempt")
    return {"transaction.id": value["transaction_id"], "app.scenario": value["scenario"],
            "cart.total_cents": total, "cart.item_count": quantity, "currency": "USD",
            "product.skus": ",".join(product for product in PRICES if product in seen),
            "payment.method": "test_card", "payment.provider": "demostore_mock_bank",
            "payment.attempt": value.get("attempt", 1)}


def create_dependency(component, config=None):
    if component not in PORTS:
        raise ValueError("Unknown mock dependency")
    app = Flask("demostore-" + component)
    app.config.from_mapping(SECRET_KEY=os.environ.get("DEMO_SECRET_KEY"), COMPONENT=component,
        SERVICE_NAME="demostore-" + component, RELEASE_ID=os.environ.get("DEMO_RELEASE_ID", "development"),
        TELEMETRY_ENABLED=os.environ.get("DEMO_TELEMETRY_ENABLED", "0") == "1", MAX_CONTENT_LENGTH=16384)
    if config:
        app.config.update(config)
    app.config.update(COMPONENT=component, SERVICE_NAME="demostore-" + component)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("A persistent application key is required")

    @app.get("/healthz")
    def health():
        return jsonify(status="ok", service=app.config["SERVICE_NAME"], release=app.config["RELEASE_ID"])

    def authenticate():
        if request.path == "/healthz":
            return None
        supplied = request.headers.get("X-Demo-Internal-Signature", "")
        expected = hmac.new(app.config["SECRET_KEY"].encode(), request.get_data(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            return jsonify(ok=False, reason="internal_auth_required"), 403
        try:
            payload = request.get_json(silent=True)
            attrs = validate_payload(payload)
        except (ValueError, TypeError):
            return jsonify(ok=False, reason="invalid_internal_payload"), 400
        request.environ["demostore.payload"] = payload
        request.environ["demostore.initial_traffic"] = payload["traffic_type"]
        request.environ["demostore.scenario"] = payload["scenario"]
        telemetry.enrich({**attrs, "app.component": component})
        return None

    def outcome(event, step, result, reason="none", status=200, **extra):
        values = {"business.step": step, "business.outcome": result, "failure.type": reason,
                  "http.response.status_code": status, **extra}
        telemetry.annotate({key: value for key, value in values.items() if key != "http.response.status_code"})
        trace.get_current_span().add_event(event, values)
        telemetry.event(event, values)

    if component == "inventory":
        @app.post("/reserve")
        @telemetry.traced("inventory.reserve")
        def reserve():
            payload = request.environ["demostore.payload"]
            time.sleep(0.01)
            if payload["scenario"] == "out-of-stock":
                outcome("inventory.rejected", "inventory.reserve", "rejected", "out_of_stock", 409, **{"inventory.available": 0})
                return jsonify(ok=False, reason="out_of_stock"), 409
            reservation = "RSV-" + payload["transaction_id"].split("-", 1)[1]
            available = 2 if payload["scenario"] == "warning" else 100
            warning = available == 2
            outcome("inventory.low_stock" if warning else "inventory.reserved", "inventory.reserve",
                    "warning" if warning else "success", "low_stock" if warning else "none",
                    **{"inventory.reservation_id": reservation, "inventory.available": available, "inventory.low_stock": warning})
            return jsonify(ok=True, reservation_id=reservation, available=available, warning=warning)

        @app.post("/release")
        @telemetry.traced("inventory.release")
        def release():
            outcome("inventory.released", "inventory.release", "released")
            return jsonify(ok=True, released=True)

    if component == "bank":
        @app.post("/charge")
        @telemetry.traced("bank.charge")
        def charge():
            payload = request.environ["demostore.payload"]
            scenario = payload["scenario"]
            outcome("bank.processing", "bank.charge", "started")
            if scenario == "retry" and payload.get("attempt", 1) == 1:
                outcome("bank.unavailable", "bank.charge", "error", "provider_retryable", 503)
                return jsonify(ok=False, reason="provider_retryable"), 503
            delay = 3.0 if scenario == "timeout" else payload["delay_ms"] / 1000 if scenario == "slow" else .65 if scenario == "payment-degraded" else .02
            time.sleep(delay)
            if scenario == "card-declined":
                outcome("bank.declined", "bank.charge", "rejected", "card_declined", 402)
                return jsonify(ok=False, reason="card_declined"), 402
            payment = "PAY-" + payload["transaction_id"].split("-", 1)[1]
            outcome("bank.approved", "bank.charge", "success", **{"payment.transaction_id": payment})
            return jsonify(ok=True, payment_id=payment, simulated=True)

        @app.post("/void")
        @telemetry.traced("bank.void")
        def void_bank():
            outcome("bank.voided", "bank.void", "voided", **{"payment.compensated": True})
            return jsonify(ok=True, voided=True)

    if component == "payment":
        @app.post("/authorize")
        @telemetry.traced("payment.authorize")
        def authorize():
            payload = request.environ["demostore.payload"]
            scenario = payload["scenario"]
            outcome("payment.started", "payment.authorize", "started")
            if scenario == "error":
                try:
                    raise PaymentApplicationError()
                except PaymentApplicationError as error:
                    reason = "payment_application_error"
                    telemetry.error(trace.get_current_span(), reason)
                    trace.get_current_span().add_event("exception", {"exception.type": type(error).__name__, "exception.message": ERROR_MESSAGES[reason]})
                    outcome("payment.failed", "payment.authorize", "error", reason, 500)
                    return jsonify(ok=False, reason=reason), 500
            for attempt in range(1, 3 if scenario == "retry" else 2):
                telemetry.annotate({"payment.attempt": attempt, "payment.max_attempts": 2 if scenario == "retry" else 1})
                timeout = 1.0 if scenario == "timeout" else max(3.5, payload["delay_ms"] / 1000 + 1)
                try:
                    status, body = call("bank", "/charge", {**payload, "attempt": attempt}, timeout)
                except DependencyFailure as failure:
                    telemetry.error(trace.get_current_span(), failure.reason)
                    outcome("payment.failed", "payment.authorize", "error", failure.reason, 503,
                            **{"payment.timeout_ms": int(timeout * 1000)})
                    return jsonify(ok=False, reason=failure.reason), 503
                if status == 503 and scenario == "retry" and attempt == 1:
                    outcome("payment.retrying", "payment.authorize", "warning", "provider_retryable", **{"payment.attempt": attempt})
                    continue
                if status == 402:
                    outcome("payment.declined", "payment.authorize", "rejected", "card_declined", 402)
                    return jsonify(ok=False, reason="card_declined"), 402
                if status != 200:
                    outcome("payment.failed", "payment.authorize", "error", "provider_unavailable", 503)
                    return jsonify(ok=False, reason="provider_unavailable"), 503
                event = "payment.provider.degraded" if scenario == "payment-degraded" else "payment.approved"
                outcome(event, "payment.authorize", "warning" if scenario in {"payment-degraded", "retry"} else "success",
                        "provider_latency" if scenario == "payment-degraded" else "retry_succeeded" if scenario == "retry" else "none",
                        **{"payment.transaction_id": body["payment_id"], "payment.attempt": attempt})
                return jsonify(ok=True, payment_id=body["payment_id"], attempts=attempt)

        @app.post("/void")
        @telemetry.traced("payment.void")
        def void_payment():
            try:
                status, body = call("bank", "/void", request.environ["demostore.payload"])
            except DependencyFailure:
                return jsonify(ok=False, reason="provider_unavailable"), 503
            outcome("payment.voided", "payment.void", "voided", **{"payment.compensated": True})
            return jsonify(body), status

    telemetry.configure(app)
    app.before_request(authenticate)
    return app
