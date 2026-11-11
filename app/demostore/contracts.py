"""Finite lab scenarios, business telemetry schema and readable log templates."""
import math
import re

SCENARIOS = frozenset({"normal", "slow", "error", "recovery", "warning", "timeout",
    "out-of-stock", "card-declined", "payment-degraded", "retry", "database-error", "notification-warning"})
COMPONENTS = frozenset({"checkout", "inventory", "payment", "bank"})
PORTS = {"inventory": 8101, "payment": 8102, "bank": 8103}
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "HTTP"})
INTERNAL_ROUTES = frozenset({"/reserve", "/release", "/authorize", "/void", "/charge"})
OPERATIONS = frozenset({"auth.login", "auth.logout", "cart.add", "cart.clear", "checkout", "cart.validate",
    "checkout.simulated_delay", "catalog.lookup", "pricing.quote", "tax.calculate", "inventory.reserve",
    "inventory.release", "payment.authorize", "payment.void", "bank.charge", "bank.void",
    "payment.retry", "order.save", "notification.enqueue"})
RUN_ID = re.compile(r"^stage4-[0-9a-f]{12,32}$")
TRANSACTION_ID = re.compile(r"^TXN-\d{8,20}$")
REASONS = frozenset({"none", "low_stock", "out_of_stock", "card_declined", "provider_timeout",
    "provider_unavailable", "provider_retryable", "provider_latency", "retry_succeeded", "payment_application_error",
    "order_persistence_failed", "notification_pending_retry", "inventory_unavailable", "payment_unavailable",
    "empty_cart", "http_server_error", "simulated_checkout_error", "checkout_exception", "checkout_rejected"})
ERROR_MESSAGES = {
    "payment_application_error": "Simulated payment application failure",
    "order_persistence_failed": "Simulated order persistence failure",
    "provider_timeout": "Mock bank response exceeded payment deadline",
    "inventory_unavailable": "Mock inventory service was unreachable",
    "payment_unavailable": "Mock payment service was unreachable",
    "provider_unavailable": "Mock bank service was unreachable",
}
EVENTS = frozenset({"application.started", "application.stopping", "http.request.completed",
    "checkout.started", "checkout.completed", "checkout.failed", "checkout.rejected", "lab.mode.changed",
    "catalog.loaded", "pricing.calculated", "tax.calculated", "inventory.reserved", "inventory.low_stock",
    "inventory.rejected", "inventory.released", "payment.started", "payment.approved", "payment.declined",
    "payment.failed", "payment.retrying", "payment.provider.degraded", "payment.voided",
    "bank.processing", "bank.approved", "bank.declined", "bank.unavailable", "bank.voided",
    "order.persisted", "notification.queued", "notification.deferred"})
WARNING_EVENTS = frozenset({"inventory.low_stock", "inventory.rejected", "payment.declined", "payment.retrying",
    "payment.provider.degraded", "bank.declined", "bank.unavailable", "notification.deferred", "checkout.rejected"})
METRIC_KEYS = frozenset({"http.request.method", "http.route", "http.response.status_code", "app.scenario",
    "checkout.outcome", "lab.traffic_type", "lab.synthetic", "lab.validation", "business.step",
    "business.outcome", "failure.type", "app.component"})


def attributes(values, routes=()):
    """Allow useful business detail without accepting arbitrary input/message strings."""
    result = {}
    choices = {
        "http.method": METHODS, "http.request.method": METHODS,
        "app.scenario": SCENARIOS, "app.component": COMPONENTS, "app.operation": OPERATIONS,
        "business.step": OPERATIONS,
        "checkout.outcome": {"success", "error", "rejected"},
        "business.outcome": {"started", "success", "error", "rejected", "warning", "released", "voided", "pending_retry"},
        "transaction.stage": {"validation", "pricing", "inventory", "payment", "persistence", "notification", "complete", "compensation"},
        "transaction.outcome": {"processing", "success", "error", "rejected"},
        "lab.traffic_type": {"manual", "synthetic", "unclassified"},
        "failure.type": REASONS, "error.type": REASONS,
        "payment.provider": {"demostore_mock_bank"}, "payment.method": {"test_card"},
        "currency": {"USD"}, "pricing.policy": {"tax_included"},
        "product.skus": {"laptop", "headphones", "laptop,headphones"},
        "db.system.name": {"sqlite"}, "db.operation.name": {"INSERT", "UPDATE", "SELECT"},
        "network.protocol.version": {"1.0", "1.1", "2", "3"}, "server.address": {"127.0.0.1"},
        "notification.state": {"queued", "pending_retry"},
    }
    for key, allowed in choices.items():
        value = values.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    for key in ("lab.synthetic", "lab.validation", "inventory.low_stock", "payment.compensated"):
        if type(values.get(key)) is bool:
            result[key] = values[key]
    route = values.get("http.route")
    if isinstance(route, str) and route in set(routes) | INTERNAL_ROUTES | {"unmatched_route"}:
        result["http.route"] = route
    for key in ("http.status_code", "http.response.status_code"):
        value = values.get(key)
        if type(value) is int and 100 <= value <= 599:
            result[key] = value
    if values.get("server.port") in PORTS.values():
        result["server.port"] = values["server.port"]
    for key, pattern in {"transaction.id": TRANSACTION_ID, "lab.validation_id": RUN_ID,
                         "order.id": re.compile(r"^DEMO-\d{6,20}$"),
                         "inventory.reservation_id": re.compile(r"^RSV-\d{8,20}$"),
                         "payment.transaction_id": re.compile(r"^PAY-\d{8,20}$")}.items():
        value = values.get(key)
        if isinstance(value, str) and pattern.fullmatch(value):
            result[key] = value
    for key in ("cart.item_count", "cart.total_cents", "product.quantity", "pricing.net_cents", "pricing.tax_cents",
                "pricing.tax_basis_points", "inventory.available", "demo.delay_ms", "http.response.duration_ms",
                "checkout.duration_ms", "business.duration_ms", "payment.attempt", "payment.max_attempts", "payment.timeout_ms"):
        value = values.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12:
            result[key] = value
    exception_type = values.get("exception.type")
    if isinstance(exception_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", exception_type):
        result["exception.type"] = exception_type
    for key in ("error.message", "exception.message"):
        value = values.get(key)
        if isinstance(value, str) and value in ERROR_MESSAGES.values():
            result[key] = value
    return result


def message(event, attrs):
    """Human-readable bodies use sanitized fields and fixed templates, never raw payloads."""
    transaction = attrs.get("transaction.id", "none")
    amount = attrs.get("cart.total_cents", 0) / 100
    reason = attrs.get("failure.type", "none")
    step = attrs.get("business.step", attrs.get("transaction.stage", "checkout"))
    if event.startswith("application."):
        return f"Application worker {event.split('.')[1]} component={attrs.get('app.component', 'checkout')}"
    if event == "http.request.completed":
        return (f"HTTP {attrs.get('http.request.method', 'HTTP')} {attrs.get('http.route', 'unmatched_route')} "
                f"status={attrs.get('http.response.status_code', 0)} transaction={transaction} "
                f"duration_ms={attrs.get('http.response.duration_ms', 0)}")
    if event == "lab.mode.changed":
        return f"Lab scenario changed mode={attrs.get('app.scenario', 'normal')} delay_ms={attrs.get('demo.delay_ms', 0)}"
    return (f"{event.replace('.', ' ').capitalize()} transaction={transaction} "
            f"stage={step} outcome={attrs.get('business.outcome', attrs.get('checkout.outcome', 'started'))} "
            f"amount={amount:.2f} USD products={attrs.get('product.skus', 'none')} "
            f"quantity={attrs.get('cart.item_count', 0)} reason={reason} "
            f"attempt={attrs.get('payment.attempt', 0)} order={attrs.get('order.id', 'none')}")
