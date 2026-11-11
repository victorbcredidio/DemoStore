import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from support import create_app


class DemoStoreTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()).resolve()
        self.temporary = tempfile.TemporaryDirectory(prefix="demostore-test-", dir=base)
        target = Path(self.temporary.name).resolve()
        if target.parent != base or not target.name.startswith("demostore-test-"):
            raise RuntimeError("Unexpected test cleanup path")
        self.addCleanup(self.temporary.cleanup)
        self.config = {"TESTING": True, "SECRET_KEY": "test-only-session-key",
                       "DATABASE": str(target / "test.sqlite3"),
                       "DEMO_USERNAME": "demo_user", "DEMO_PASSWORD": "demo_pass"}
        self.app = create_app(self.config)
        self.client, self.token = self.sign_in(self.app)

    def sign_in(self, app):
        client = app.test_client()
        token = client.get("/api/session").json["csrf_token"]
        response = client.post("/api/login", json={"username": "demo_user", "password": "demo_pass", "traffic_type": "synthetic"}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 200)
        return client, response.json["csrf_token"]

    def post(self, path, data=None, client=None, token=None):
        return (client or self.client).post(path, json={} if data is None else data,
            headers={"X-CSRF-Token": token or self.token})

    def add(self, product="laptop", quantity=1, **kwargs):
        return self.post("/api/cart/items", {"product_id": product, "quantity": quantity}, **kwargs)

    def mode(self, mode, delay=200):
        return self.post("/api/lab/control", {"mode": mode, "delay_ms": delay})

    def test_protected_routes_require_login(self):
        guest = self.app.test_client()
        self.assertEqual(guest.get("/").status_code, 302)
        self.assertEqual(guest.get("/api/cart").status_code, 401)
        self.assertEqual(guest.post("/api/lab/control", json={"mode": "error"}).status_code, 401)

    def test_login_rejects_invalid_unicode_credentials_without_crashing(self):
        guest = self.app.test_client()
        token = guest.get("/api/session").json["csrf_token"]
        response = guest.post("/api/login", json={"username": "不正", "password": "wrong"}, headers={"X-CSRF-Token": token})
        self.assertEqual(response.status_code, 401)

    def test_csrf_required_for_login_and_mutation(self):
        guest = self.app.test_client()
        self.assertEqual(guest.post("/api/login", json={"username": "demo_user", "password": "demo_pass"}).status_code, 400)
        self.assertEqual(self.client.post("/api/cart/items", json={"product_id": "laptop"}).status_code, 400)

    def test_login_rotates_csrf_and_labels_session(self):
        guest = self.app.test_client()
        before = guest.get("/api/session").json["csrf_token"]
        result = guest.post("/api/login", json={"username": "demo_user", "password": "demo_pass", "traffic_type": "synthetic"}, headers={"X-CSRF-Token": before})
        self.assertNotEqual(result.json["csrf_token"], before)
        self.assertEqual(guest.get("/api/cart").headers["X-Lab-Traffic-Type"], "synthetic")

    def test_catalog_and_server_computed_cart_prices(self):
        self.assertEqual(len(self.client.get("/api/products").json["products"]), 2)
        response = self.post("/api/cart/items", {"product_id": "laptop", "quantity": 2, "price_cents": 1})
        self.assertEqual(response.json["cart"]["total_cents"], 199800)
        self.add("headphones")
        cart = self.client.get("/api/cart").json["cart"]
        self.assertEqual((cart["item_count"], cart["total_cents"]), (3, 214700))

    def test_read_responses_cannot_overwrite_a_newer_cart_cookie(self):
        for path in ("/", "/api/session", "/api/products", "/api/cart", "/api/lab/control"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Set-Cookie", response.headers, path)
        changed = self.add()
        self.assertIn("Set-Cookie", changed.headers)
        self.assertEqual(self.client.get("/api/cart").json["cart"]["item_count"], 1)

    def test_invalid_product_and_unhashable_input_are_rejected(self):
        for product in ("unknown", ["laptop"], {"id": "laptop"}, None):
            self.assertEqual(self.post("/api/cart/items", {"product_id": product}).status_code, 400)

    def test_quantity_is_bounded_integer(self):
        for quantity in (-1, 0, 100, True, 1.5, "2"):
            self.assertEqual(self.add(quantity=quantity).status_code, 400)
        self.assertEqual(self.add(quantity=99).status_code, 200)
        self.assertEqual(self.add().status_code, 400)

    def test_empty_checkout_is_rejected(self):
        self.assertEqual(self.post("/api/checkout").json["error"]["code"], "EMPTY_CART")

    def test_normal_checkout_creates_order_and_clears_cart(self):
        self.add()
        response = self.post("/api/checkout")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response.json["order"]["id"], r"^DEMO-\d{6,}$")
        self.assertEqual(response.json["order"]["total_cents"], 99900)
        self.assertEqual(response.json["cart"]["item_count"], 0)
        self.assertEqual(self.post("/api/checkout").status_code, 400)

    def test_error_checkout_preserves_cart_and_health(self):
        self.add()
        self.mode("error")
        response = self.post("/api/checkout")
        self.assertEqual((response.status_code, response.json["error"]["code"]), (500, "SIMULATED_CHECKOUT_ERROR"))
        self.assertEqual(self.client.get("/api/cart").json["cart"]["item_count"], 1)
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_recovery_succeeds_using_the_preserved_cart(self):
        self.add()
        self.mode("error")
        self.post("/api/checkout")
        self.mode("recovery")
        response = self.post("/api/checkout")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["order"]["scenario"], "recovery")

    def test_slow_checkout_has_measured_delay(self):
        self.add()
        self.mode("slow", 200)
        start = time.monotonic()
        response = self.post("/api/checkout")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(time.monotonic() - start, .18)
        self.assertGreaterEqual(response.json["order"]["elapsed_ms"], 180)

    def test_mode_and_delay_validation(self):
        for mode in ("bad", ["error"], None):
            self.assertEqual(self.post("/api/lab/control", {"mode": mode}).status_code, 400)
        for delay in (0, 99, 10001, True, "500"):
            self.assertEqual(self.mode("slow", delay).status_code, 400)

    def test_two_app_instances_share_fault_state(self):
        second = create_app(self.config)
        other, token = self.sign_in(second)
        self.mode("error")
        self.add(client=other, token=token)
        self.assertEqual(self.post("/api/checkout", client=other, token=token).status_code, 500)
        self.mode("recovery")
        self.assertEqual(self.post("/api/checkout", client=other, token=token).status_code, 200)

    def test_carts_are_independent_between_browser_sessions(self):
        other, _ = self.sign_in(self.app)
        self.add()
        self.assertEqual(other.get("/api/cart").json["cart"]["item_count"], 0)

    def test_restart_preserves_mode_and_orders(self):
        self.add()
        self.post("/api/checkout")
        self.mode("slow", 500)
        client, _ = self.sign_in(create_app(self.config))
        self.assertEqual(client.get("/api/lab/control").json["control"]["mode"], "slow")
        with closing(sqlite3.connect(self.config["DATABASE"])) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_order").fetchone()[0], 1)

    def test_reset_returns_normal_clears_own_cart_preserves_history(self):
        self.add()
        self.post("/api/checkout")
        self.add()
        self.mode("error")
        response = self.post("/api/lab/reset")
        self.assertEqual(response.json["control"]["mode"], "normal")
        self.assertEqual(response.json["cart"]["item_count"], 0)
        with closing(sqlite3.connect(self.config["DATABASE"])) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM demo_order").fetchone()[0], 1)

    def test_logout_removes_access(self):
        self.assertEqual(self.post("/api/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/cart").status_code, 401)

    def test_html_stable_ids_and_headers(self):
        html = self.client.get("/")
        for element in ("add-laptop", "add-headphones", "checkout-button", "mode-recovery", "reset-button"):
            self.assertIn(f'id="{element}"', html.text)
        self.assertIn("script-src 'self'", html.headers["Content-Security-Policy"])
        initial_cookie = self.app.test_client().get("/api/session").headers["Set-Cookie"]
        self.assertIn("HttpOnly", initial_cookie)
        self.assertIn("SameSite=Lax", initial_cookie)

    def test_invalid_json_and_large_payloads(self):
        response = self.client.post("/api/cart/items", data="bad json", content_type="application/json", headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/cart/items", data="x" * 17000, content_type="application/json", headers={"X-CSRF-Token": self.token})
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main(verbosity=2)
