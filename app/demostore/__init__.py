"""A deliberately small, deterministic store for a non-production monitoring lab."""
from datetime import timedelta
from contextlib import contextmanager
from functools import wraps
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from . import telemetry
from .contracts import SCENARIOS, TRANSACTION_ID
from .checkout import process as process_checkout

PRODUCTS = (
    {"id": "laptop", "name": "Laptop", "price_cents": 99900, "icon": "laptop"},
    {"id": "headphones", "name": "Headphones", "price_cents": 14900, "icon": "headphones"},
)
PRODUCT_INDEX = {product["id"]: product for product in PRODUCTS}
MODES = SCENARIOS
DEFAULT_DELAY_MS = 3000


def create_app(config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("DEMO_SECRET_KEY"),
        DATABASE=os.environ.get("DEMO_DATABASE", str(Path(app.instance_path) / "lab.sqlite3")),
        DEMO_USERNAME=os.environ.get("DEMO_USERNAME", "demo_user"),
        DEMO_PASSWORD=os.environ.get("DEMO_PASSWORD", "demo_pass"),
        RELEASE_ID=os.environ.get("DEMO_RELEASE_ID", "development"),
        TELEMETRY_ENABLED=os.environ.get("DEMO_TELEMETRY_ENABLED", "0") == "1",
        SESSION_COOKIE_NAME="demostore_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("DEMO_COOKIE_SECURE", "0") == "1",
        # A concurrent read must not reissue a stale cookie over a cart mutation.
        SESSION_REFRESH_EACH_REQUEST=False,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=4),
        MAX_CONTENT_LENGTH=16 * 1024,
    )
    if config:
        app.config.update(config)
    if not app.config["SECRET_KEY"]:
        raise RuntimeError("Set DEMO_SECRET_KEY to a persistent random value before starting DemoStore.")

    database = Path(app.config["DATABASE"])
    database.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect():
        connection = sqlite3.connect(database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS lab_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL,
                delay_ms INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            INSERT OR IGNORE INTO lab_control (id, mode, delay_ms) VALUES (1, 'normal', 3000);
            CREATE TABLE IF NOT EXISTS demo_order (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                item_count INTEGER NOT NULL,
                total_cents INTEGER NOT NULL,
                scenario TEXT NOT NULL,
                traffic_type TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS demo_transaction (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                scenario TEXT NOT NULL, stage TEXT NOT NULL, outcome TEXT NOT NULL,
                reason TEXT NOT NULL, item_count INTEGER NOT NULL, total_cents INTEGER NOT NULL,
                traffic_type TEXT NOT NULL, trace_id TEXT, order_id TEXT,
                warnings TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS demo_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL UNIQUE,
                order_id TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
        """)

    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(24)
        return session["csrf_token"]

    def failure(code, message, status):
        return jsonify(ok=False, error={"code": code, "message": message}), status

    def authenticated(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if session.get("username") != app.config["DEMO_USERNAME"]:
                return failure("LOGIN_REQUIRED", "Sign in to use DemoStore.", 401)
            return view(*args, **kwargs)
        return wrapped

    def csrf_protected(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            supplied = request.headers.get("X-CSRF-Token", "")
            expected = session.get("csrf_token", "")
            if not supplied or not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
                return failure("INVALID_CSRF", "Refresh the page and try again.", 400)
            return view(*args, **kwargs)
        return wrapped

    def body():
        value = request.get_json(silent=True)
        return value if isinstance(value, dict) else None

    def cart():
        stored = session.get("cart", {})
        items = []
        for product in PRODUCTS:
            quantity = stored.get(product["id"], 0)
            if isinstance(quantity, int) and not isinstance(quantity, bool) and 1 <= quantity <= 99:
                items.append({**product, "quantity": quantity,
                              "subtotal_cents": quantity * product["price_cents"]})
        return {"items": items, "item_count": sum(item["quantity"] for item in items),
                "total_cents": sum(item["subtotal_cents"] for item in items)}

    def control():
        with connect() as connection:
            row = connection.execute("SELECT mode, delay_ms, updated_at FROM lab_control WHERE id=1").fetchone()
        return dict(row)

    def set_control(mode, delay_ms):
        with connect() as connection:
            connection.execute("""UPDATE lab_control SET mode=?, delay_ms=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=1""", (mode, delay_ms))
        telemetry.scenario(mode)
        telemetry.event("lab.mode.changed", {"app.scenario": mode, "demo.delay_ms": delay_ms})
        return control()

    @app.context_processor
    def template_context():
        return {"csrf_token": csrf_token(), "demo_username": app.config["DEMO_USERNAME"],
                "default_account": app.config["DEMO_USERNAME"] == "demo_user" and app.config["DEMO_PASSWORD"] == "demo_pass",
                "traffic_type": session.get("traffic_type", "manual")}

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        response.headers["X-Lab-Traffic-Type"] = session.get("traffic_type", "unclassified")
        return response

    @app.get("/healthz")
    def health():
        with connect() as connection:
            connection.execute("SELECT mode FROM lab_control WHERE id=1").fetchone()
        return jsonify(status="ok", service="demostore", release=app.config["RELEASE_ID"])

    @app.get("/")
    def store():
        if session.get("username") != app.config["DEMO_USERNAME"]:
            return redirect(url_for("login"))
        return render_template("store.html", products=PRODUCTS, username=session["username"])

    @app.get("/login")
    def login():
        if session.get("username") == app.config["DEMO_USERNAME"]:
            return redirect(url_for("store"))
        traffic = request.args.get("traffic", "manual")
        return render_template("login.html", selected_traffic="synthetic" if traffic == "synthetic" else "manual")

    @app.get("/api/session")
    def session_info():
        return jsonify(ok=True, authenticated=session.get("username") == app.config["DEMO_USERNAME"],
                       csrf_token=csrf_token(), traffic_type=session.get("traffic_type", "manual"))

    @app.post("/api/login")
    @csrf_protected
    @telemetry.traced("auth.login")
    def sign_in():
        data = body()
        if data is None:
            return failure("INVALID_BODY", "Send a JSON object.", 400)
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return failure("INVALID_LOGIN", "Use the lab account shown on the sign-in page.", 401)
        if not (hmac.compare_digest(username.encode(), app.config["DEMO_USERNAME"].encode())
                and hmac.compare_digest(password.encode(), app.config["DEMO_PASSWORD"].encode())):
            return failure("INVALID_LOGIN", "Username or password is incorrect.", 401)
        session.clear()
        session.permanent = True
        session["username"] = username
        session["traffic_type"] = "synthetic" if data.get("traffic_type") == "synthetic" else "manual"
        session["cart"] = {}
        return jsonify(ok=True, username=username, csrf_token=csrf_token(), traffic_type=session["traffic_type"])

    @app.post("/api/logout")
    @authenticated
    @csrf_protected
    @telemetry.traced("auth.logout")
    def sign_out():
        session.clear()
        return jsonify(ok=True)

    @app.get("/api/products")
    @authenticated
    def products():
        return jsonify(ok=True, products=PRODUCTS)

    @app.get("/api/cart")
    @authenticated
    def get_cart():
        return jsonify(ok=True, cart=cart())

    @app.post("/api/cart/items")
    @authenticated
    @csrf_protected
    @telemetry.traced("cart.add")
    def add_item():
        data = body()
        if data is None or not isinstance(data.get("product_id"), str) or data["product_id"] not in PRODUCT_INDEX:
            return failure("INVALID_PRODUCT", "Choose a listed product.", 400)
        quantity = data.get("quantity", 1)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 99:
            return failure("INVALID_QUANTITY", "Quantity must be an integer from 1 to 99.", 400)
        stored = dict(session.get("cart", {}))
        new_quantity = stored.get(data["product_id"], 0) + quantity
        if new_quantity > 99:
            return failure("INVALID_QUANTITY", "The cart allows at most 99 of each product.", 400)
        stored[data["product_id"]] = new_quantity
        session["cart"] = stored
        return jsonify(ok=True, cart=cart())

    @app.post("/api/cart/clear")
    @authenticated
    @csrf_protected
    @telemetry.traced("cart.clear")
    def clear_cart():
        session["cart"] = {}
        return jsonify(ok=True, cart=cart())

    @app.get("/api/lab/control")
    @authenticated
    def get_control():
        return jsonify(ok=True, control=control())

    @app.post("/api/lab/control")
    @authenticated
    @csrf_protected
    def change_control():
        data = body()
        if data is None or not isinstance(data.get("mode"), str) or data["mode"] not in MODES:
            return failure("INVALID_MODE", "Choose a supported lab scenario.", 400)
        delay = data.get("delay_ms", control()["delay_ms"])
        if not isinstance(delay, int) or isinstance(delay, bool) or not 100 <= delay <= 10000:
            return failure("INVALID_DELAY", "Delay must be an integer from 100 to 10000 ms.", 400)
        return jsonify(ok=True, control=set_control(data["mode"], delay))

    @app.post("/api/lab/reset")
    @authenticated
    @csrf_protected
    def reset():
        session["cart"] = {}
        return jsonify(ok=True, control=set_control("normal", DEFAULT_DELAY_MS), cart=cart())

    @app.post("/api/checkout")
    @authenticated
    @csrf_protected
    @telemetry.traced("checkout")
    def checkout():
        data = body()
        if data is None:
            return failure("INVALID_BODY", "Send a JSON object.", 400)
        selected = control()
        if "scenario" in data:
            if session.get("traffic_type") != "synthetic":
                return failure("SCENARIO_OVERRIDE_FORBIDDEN", "Per-request scenarios require a synthetic lab session.", 403)
            if not isinstance(data["scenario"], str) or data["scenario"] not in MODES:
                return failure("INVALID_MODE", "Choose a supported lab scenario.", 400)
            selected = {**selected, "mode": data["scenario"]}
            delay = data.get("delay_ms", selected["delay_ms"])
            if type(delay) is not int or not 100 <= delay <= 10000:
                return failure("INVALID_DELAY", "Delay must be from 100 to 10000 ms.", 400)
            selected["delay_ms"] = delay
        return process_checkout(connect, cart, selected)

    @app.get("/api/lab/transactions")
    @authenticated
    def transactions():
        try:
            limit = int(request.args.get("limit", "10"))
            if not 1 <= limit <= 100:
                raise ValueError()
        except ValueError:
            return failure("INVALID_LIMIT", "Limit must be from 1 to 100.", 400)
        with connect() as connection:
            rows = connection.execute("SELECT * FROM demo_transaction ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return jsonify(ok=True, transactions=[transaction_row(row) for row in rows])

    @app.get("/api/lab/status")
    @authenticated
    def lab_status():
        import urllib.request
        from .contracts import PORTS
        services = {}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for component, port in PORTS.items():
            try:
                with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=.5) as response:
                    data = json.load(response)
                    services["demostore-" + component] = {"healthy": response.status == 200, "release": data.get("release")}
            except (OSError, ValueError):
                services["demostore-" + component] = {"healthy": False}
        return jsonify(ok=True, service="demostore", release=app.config["RELEASE_ID"], control=control(),
                       scenarios=sorted(MODES), dependencies=services, telemetry_enabled=app.config["TELEMETRY_ENABLED"])

    def transaction_row(row):
        result = dict(row)
        result["id"] = f"TXN-{result['id']:08d}"
        result["currency"] = "USD"
        result["warnings"] = json.loads(result["warnings"])
        return result

    @app.get("/api/lab/transactions/<transaction_id>")
    @authenticated
    def transaction_details(transaction_id):
        if not TRANSACTION_ID.fullmatch(transaction_id):
            return failure("INVALID_TRANSACTION", "Use a TXN transaction identifier.", 400)
        with connect() as connection:
            row = connection.execute("SELECT * FROM demo_transaction WHERE id=?", (int(transaction_id.split("-")[1]),)).fetchone()
        if row is None:
            return failure("TRANSACTION_NOT_FOUND", "Transaction was not found.", 404)
        return jsonify(ok=True, transaction=transaction_row(row))

    @app.errorhandler(HTTPException)
    def http_error(error):
        if request.path.startswith("/api/"):
            return failure(error.name.upper().replace(" ", "_"), error.description, error.code)
        return error

    telemetry.configure(app)
    return app
