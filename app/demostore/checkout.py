"""A durable lab transaction with real mock-service calls and visible compensation."""
import json
import time

from flask import jsonify, request, session
from opentelemetry import trace

from . import telemetry
from .contracts import ERROR_MESSAGES
from .dependencies import call, DependencyFailure


class OrderPersistenceError(Exception):
    pass


def process(connect, cart, scenario):
    started = time.perf_counter()
    mode, delay = scenario["mode"], scenario["delay_ms"]
    order_cart = cart()
    trace_id = trace.get_current_span().get_span_context().trace_id
    with connect() as connection:
        cursor = connection.execute("""INSERT INTO demo_transaction
            (scenario,stage,outcome,reason,item_count,total_cents,traffic_type,trace_id)
            VALUES (?,'validation','processing','none',?,?,?,?)""",
            (mode, order_cart["item_count"], order_cart["total_cents"], session["traffic_type"],
             f"{trace_id:032x}" if trace_id else None))
        number = cursor.lastrowid
    transaction = f"TXN-{number:08d}"
    attrs = {"transaction.id": transaction, "transaction.stage": "validation", "transaction.outcome": "processing",
             "app.component": "checkout", "currency": "USD", "cart.item_count": order_cart["item_count"],
             "cart.total_cents": order_cart["total_cents"],
             "product.skus": ",".join(item["id"] for item in order_cart["items"]),
             "payment.provider": "demostore_mock_bank", "payment.method": "test_card"}
    telemetry.enrich(attrs)
    telemetry.scenario(mode)
    telemetry.event("checkout.started", {"business.outcome": "started"})
    warnings = []

    def stage(value):
        telemetry.enrich({"transaction.stage": value})
        with connect() as connection:
            connection.execute("UPDATE demo_transaction SET stage=? WHERE id=?", (value, number))

    def finish(outcome, reason, failed_stage, status, code, message, order=None):
        with connect() as connection:
            connection.execute("""UPDATE demo_transaction SET stage=?,outcome=?,reason=?,warnings=?,order_id=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""",
                (failed_stage, outcome, reason, json.dumps(warnings), order["id"] if order else None, number))
        values = {"transaction.stage": failed_stage, "transaction.outcome": outcome, "failure.type": reason,
                  "business.outcome": outcome, **({"order.id": order["id"]} if order else {})}
        if outcome == "error":
            values["error.type"] = reason
        telemetry.enrich(values)
        trace.get_current_span().add_event("checkout.completed" if outcome == "success" else "checkout.rejected" if outcome == "rejected" else "checkout.failed", values)
        summary = {"id": transaction, "outcome": outcome, "reason": reason, "stage": failed_stage,
                   "scenario": mode, "trace_id": f"{trace_id:032x}" if trace_id else None, "warnings": warnings}
        if order:
            return jsonify(ok=True, order=order, transaction=summary, cart=cart())
        return jsonify(ok=False, error={"code": code, "message": message}, transaction=summary), status

    with telemetry.operation("cart.validate") as span:
        span.set_attributes({"cart.item_count": order_cart["item_count"], "cart.total_cents": order_cart["total_cents"]})
        if not order_cart["items"]:
            telemetry.error(span, "empty_cart")
            return finish("rejected", "empty_cart", "validation", 400, "EMPTY_CART", "Add a product before checking out.")

    stage("pricing")
    with telemetry.operation("catalog.lookup") as span:
        span.add_event("catalog.loaded", attrs)
        telemetry.event("catalog.loaded", {"business.step": "catalog.lookup", "business.outcome": "success"})
    with telemetry.operation("pricing.quote", {"pricing.policy": "tax_included"}):
        with telemetry.operation("tax.calculate") as span:
            net = order_cart["total_cents"] * 10000 // 10825
            pricing = {"pricing.net_cents": net, "pricing.tax_cents": order_cart["total_cents"] - net,
                       "pricing.tax_basis_points": 825, "pricing.policy": "tax_included"}
            span.set_attributes(pricing)
            span.add_event("tax.calculated", pricing)
            telemetry.event("tax.calculated", {**pricing, "business.step": "tax.calculate"})
        telemetry.enrich(pricing)
        telemetry.event("pricing.calculated", {"business.step": "pricing.quote", "business.outcome": "success"})

    payload = {"transaction_id": transaction, "scenario": mode, "delay_ms": delay,
               "items": [{"product_id": item["id"], "quantity": item["quantity"]} for item in order_cart["items"]],
               "total_cents": order_cart["total_cents"], "traffic_type": session["traffic_type"],
               "validation_id": telemetry.common_attributes().get("lab.validation_id", "")}

    def release(void=False):
        stage("compensation")
        for component, path in (([("payment", "/void")] if void else []) + [("inventory", "/release")]):
            try:
                code, _ = call(component, path, payload)
                if code == 200 and void and component == "payment":
                    telemetry.enrich({"payment.compensated": True})
            except DependencyFailure:
                # The failure is already a visible CLIENT span. Do not conceal the
                # original transaction failure behind a secondary transport exception.
                pass

    stage("inventory")
    try:
        status, inventory = call("inventory", "/reserve", payload)
    except DependencyFailure:
        return finish("error", "inventory_unavailable", "inventory", 502, "INVENTORY_UNAVAILABLE", "Mock inventory service is unavailable. Retry after recovery.")
    if status == 409:
        return finish("rejected", "out_of_stock", "inventory", 409, "OUT_OF_STOCK", "The simulated inventory has insufficient stock.")
    if status != 200:
        return finish("error", "inventory_unavailable", "inventory", 502, "INVENTORY_UNAVAILABLE", "Mock inventory reservation failed.")
    telemetry.enrich({"inventory.reservation_id": inventory["reservation_id"], "inventory.available": inventory["available"]})
    if inventory.get("warning"):
        warnings.append("low_stock")

    stage("payment")
    try:
        if mode == "slow":
            with telemetry.operation("checkout.simulated_delay", {"demo.delay_ms": delay}):
                status, payment = call("payment", "/authorize", payload, max(5.0, delay / 1000 + 2))
        else:
            status, payment = call("payment", "/authorize", payload, 5.0)
    except DependencyFailure as failure:
        release()
        return finish("error", failure.reason, "payment", 502, "PAYMENT_UNAVAILABLE", "Mock payment service is unavailable.")
    if status != 200:
        reason = payment.get("reason", "payment_unavailable")
        release()
        if status == 402:
            return finish("rejected", "card_declined", "payment", 402, "CARD_DECLINED", "The mock bank declined this test payment.")
        if reason == "payment_application_error":
            return finish("error", reason, "payment", 500, "SIMULATED_CHECKOUT_ERROR", "Intentional payment failure. Switch to recovery and retry.")
        return finish("error", reason, "payment", 502, "PAYMENT_PROVIDER_TIMEOUT" if reason == "provider_timeout" else "PAYMENT_UNAVAILABLE",
                      "Mock bank response timed out." if reason == "provider_timeout" else "Mock payment provider failed.")
    telemetry.enrich({"payment.transaction_id": payment["payment_id"], "payment.attempt": payment.get("attempts", 1)})
    if mode in {"payment-degraded", "retry"}:
        warnings.append("provider_latency" if mode == "payment-degraded" else "retry_succeeded")

    stage("persistence")
    try:
        with telemetry.operation("order.save", {"db.system.name": "sqlite", "db.operation.name": "INSERT"}) as span:
            if mode == "database-error":
                telemetry.error(span, "order_persistence_failed")
                raise OrderPersistenceError()
            with connect() as connection:
                cursor = connection.execute("""INSERT INTO demo_order (item_count,total_cents,scenario,traffic_type)
                    VALUES (?,?,?,?)""", (order_cart["item_count"], order_cart["total_cents"], mode, session["traffic_type"]))
                order_id = f"DEMO-{cursor.lastrowid:06d}"
                connection.execute("INSERT INTO demo_outbox (transaction_id,order_id,state) VALUES (?,?,?)",
                                   (transaction, order_id, "pending_retry" if mode == "notification-warning" else "queued"))
            span.set_attribute("order.id", order_id)
            span.add_event("order.persisted", {"order.id": order_id, "transaction.id": transaction})
            telemetry.event("order.persisted", {"order.id": order_id, "business.step": "order.save", "business.outcome": "success"})
    except OrderPersistenceError:
        release(void=True)
        return finish("error", "order_persistence_failed", "persistence", 503, "ORDER_STORAGE_FAILURE", "Simulated order persistence failed; compensation was requested.")

    stage("notification")
    with telemetry.operation("notification.enqueue") as span:
        deferred = mode == "notification-warning"
        values = {"notification.state": "pending_retry" if deferred else "queued", "order.id": order_id,
                  "business.step": "notification.enqueue", "business.outcome": "pending_retry" if deferred else "success"}
        if deferred:
            values["failure.type"] = "notification_pending_retry"
            warnings.append("notification_pending_retry")
        span.set_attributes(values)
        span.add_event("notification.deferred" if deferred else "notification.queued", values)
        telemetry.event("notification.deferred" if deferred else "notification.queued", values)
    session["cart"] = {}
    order = {"id": order_id, "item_count": order_cart["item_count"], "total_cents": order_cart["total_cents"],
             "scenario": mode, "elapsed_ms": round((time.perf_counter() - started) * 1000)}
    return finish("success", warnings[-1] if warnings else "none", "complete", 200, None, None, order=order)
