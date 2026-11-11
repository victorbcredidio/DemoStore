"use strict";
const byId = id => document.getElementById(id);
const money = cents => new Intl.NumberFormat("en-US", {style:"currency", currency:"USD"}).format(cents / 100);
const csrf = document.querySelector('meta[name="csrf-token"]').content;
async function api(path, data) {
  const options = {credentials:"same-origin", headers:{"Accept":"application/json"}};
  if (data !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.headers["X-CSRF-Token"] = csrf;
    options.body = JSON.stringify(data);
  }
  const response = await fetch(path, options);
  const payload = await response.json();
  if (payload.transaction && byId("transaction-details")) {
    byId("transaction-details").textContent = `Transaction ${payload.transaction.id} · ${payload.transaction.outcome} · ${payload.transaction.reason} · Trace ${payload.transaction.trace_id || "disabled"}`;
  }
  if (!response.ok) {
    const error = new Error(payload.error?.message || "Request failed. Please try again.");
    error.status = response.status;
    throw error;
  }
  return payload;
}
function message(id, text, state) {
  const element = byId(id);
  element.textContent = text;
  if (state) element.dataset.state = state;
}
if (document.body.dataset.page === "login") {
  byId("login-form").addEventListener("submit", async event => {
    event.preventDefault();
    byId("login-submit").disabled = true;
    message("login-feedback", "Signing in…");
    try {
      await api("/api/login", {username:byId("login-username").value, password:byId("login-password").value, traffic_type:byId("traffic-type").value});
      window.location.assign("/");
    } catch (error) {
      message("login-feedback", error.message, "error");
      byId("login-submit").disabled = false;
    }
  });
}
if (document.body.dataset.page === "store") {
  let currentCart = {item_count:0};
  let checkingOut = false;
  function drawCart(cart) {
    currentCart = cart;
    byId("cart-count").textContent = `${cart.item_count} ${cart.item_count === 1 ? "item" : "items"}`;
    byId("cart-total").textContent = money(cart.total_cents);
    byId("cart-items").textContent = cart.items.length ? cart.items.map(item => `${item.name} × ${item.quantity}`).join(" · ") : "Your cart is empty.";
    byId("checkout-button").disabled = !cart.item_count || checkingOut;
    byId("clear-cart-button").disabled = !cart.item_count || checkingOut;
  }
  async function refreshControl() {
    const {control} = await api("/api/lab/control");
    byId("current-mode").textContent = control.mode;
    byId("current-mode").dataset.mode = control.mode;
    byId("delay-ms").value = control.delay_ms;
    document.querySelectorAll(".mode-buttons [data-mode]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.mode === control.mode)));
    return control;
  }
  document.querySelectorAll(".add-button").forEach(button => button.addEventListener("click", async () => {
    button.disabled = true;
    try {
      const result = await api("/api/cart/items", {product_id:button.dataset.product});
      drawCart(result.cart);
      message("checkout-result", "", "idle");
      message("store-feedback", "");
    } catch (error) {message("store-feedback", error.message, "error");}
    finally {button.disabled = false;}
  }));
  byId("clear-cart-button").addEventListener("click", async () => {
    try {drawCart((await api("/api/cart/clear", {})).cart); message("checkout-result", "", "idle");}
    catch (error) {message("store-feedback", error.message, "error");}
  });
  byId("checkout-button").addEventListener("click", async () => {
    checkingOut = true;
    drawCart(currentCart);
    byId("checkout-button").textContent = "Processing checkout…";
    message("checkout-result", "Processing your simulated order…", "loading");
    const start = performance.now();
    let status = 200;
    try {
      const result = await api("/api/checkout", {});
      drawCart(result.cart);
      byId("checkout-result").dataset.orderId = result.order.id;
      message("checkout-result", `Order ${result.order.id} completed · ${money(result.order.total_cents)}. Thank you!`, "success");
    } catch (error) {
      status = error.status || "network error";
      message("checkout-result", error.message, "error");
    } finally {
      const duration = Math.round(performance.now() - start);
      byId("last-checkout").textContent = `Last checkout: HTTP ${status} · ${duration} ms`;
      byId("last-checkout").dataset.status = String(status);
      byId("last-checkout").dataset.durationMs = String(duration);
      checkingOut = false;
      drawCart(currentCart);
      byId("checkout-button").textContent = "Complete Checkout";
      await refreshControl().catch(() => {});
    }
  });
  document.querySelectorAll(".mode-buttons [data-mode]").forEach(button => button.addEventListener("click", async () => {
    try {
      const delay = Number(byId("delay-ms").value);
      await api("/api/lab/control", {mode:button.dataset.mode, delay_ms:delay});
      const control = await refreshControl();
      message("lab-feedback", `Checkout mode: ${control.mode}${control.mode === "slow" ? ` (${control.delay_ms} ms)` : ""}.`, "success");
      message("checkout-result", "", "idle");
    } catch (error) {message("lab-feedback", error.message, "error");}
  }));
  byId("reset-button").addEventListener("click", async () => {
    try {
      const result = await api("/api/lab/reset", {});
      drawCart(result.cart);
      await refreshControl();
      message("checkout-result", "", "idle");
      message("lab-feedback", "Demo reset. Normal mode; your cart is empty.", "success");
    } catch (error) {message("lab-feedback", error.message, "error");}
  });
  byId("logout-button").addEventListener("click", async () => {
    try {await api("/api/logout", {}); window.location.assign("/login");}
    catch (error) {message("store-feedback", error.message, "error");}
  });
  byId("lab-controls").addEventListener("toggle", () => {
    if (byId("lab-controls").open) refreshControl().catch(error => message("lab-feedback", error.message, "error"));
  });
  Promise.all([api("/api/cart").then(result => drawCart(result.cart)), refreshControl()])
    .catch(error => message("store-feedback", error.message, "error"));
}
