# DemoStore

A small, deterministic online store for **monitoring demonstrations**. It covers synthetic browser checks, backend APM with OpenTelemetry, and browser RUM (Real User Monitoring). You can switch checkout between healthy, slow, failing and recovering on demand, and every request produces readable traces, metrics and logs.

> **Test-only application.** It uses a public demo account, has no real payments and stores simulated orders only. Don't expose it to the Internet.

## What's inside

```text
Browser ──► demostore (Flask/Gunicorn, :8000)
               ├─ HTTP ─► demostore-inventory (127.0.0.1:8101)
               └─ HTTP ─► demostore-payment   (127.0.0.1:8102) ─► demostore-bank (127.0.0.1:8103)
            SQLite: orders, transaction ledger, receipt outbox, shared fault mode
            Optional OTLP/HTTP export of traces, metrics and logs from all four services
            Optional browser RUM: the page loads /__rum__/rum-v1.js, which reports to /__rum__/v1/*,
            and the store relays that to the same Collector (service demostore-browser)
```

- **Store:** login, two products (Laptop $999, Headphones $149), a per-browser cart and a simulated checkout.
- **Real service boundaries:** inventory, payment and bank are separate processes called over HTTP with W3C trace-context propagation, so one checkout is a 4-service distributed trace (17 spans in the normal case).
- **12 fault scenarios**, selectable in the page's *Lab controls* or per request from the CLI.
- **Stable element IDs** (`login-username`, `add-laptop`, `checkout-button`, `checkout-result`, …) for Selenium or Zabbix Browser items.
- **Privacy by design:** telemetry never contains bodies, credentials, cookies, raw URLs or stack traces. Metric labels are bounded.

The container runs all four processes. The three mock services listen only on the container's loopback interface; port 8000 is the only one exposed.

## Run it

### Docker or Podman

```bash
docker run -d --name demostore -p 8080:8000 \
  -v demostore-data:/var/lib/demostore \
  ghcr.io/victorbcredidio/demostore:latest
```

Podman uses the same command with `podman` instead of `docker`.

Open **http://localhost:8080** and sign in with the lab account shown on the login page (`demo_user` / `demo_pass` by default).

Check it's healthy:

```bash
curl http://localhost:8080/healthz
# {"release": "...", "service": "demostore", "status": "ok"}
```

### Docker Compose

```bash
docker compose up -d          # uses the published image
docker compose up -d --build  # builds from this repository instead
```

### Build the image yourself

```bash
docker build -t demostore:local --build-arg DEMO_RELEASE_ID=local .
docker run -d --name demostore -p 8080:8000 -v demostore-data:/var/lib/demostore demostore:local
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEMO_USERNAME` / `DEMO_PASSWORD` | `demo_user` / `demo_pass` | Test account. The login page shows it only while the defaults are in use. |
| `DEMO_SECRET_KEY` | generated once into `/var/lib/demostore/.secret_key` | Signs session cookies and internal service calls. Keep it stable, or carts are lost on restart. |
| `DEMO_PORT` | `8000` | Storefront port inside the container |
| `DEMO_WORKERS` / `DEMO_THREADS` | `2` / `2` | Gunicorn workers and threads for the storefront |
| `DEMO_COOKIE_SECURE` | `0` | Set to `1` when users reach the store over HTTPS (for example behind a TLS reverse proxy) |
| `DEMO_RELEASE_ID` | image build value | Shown in `/healthz` and used as `service.version` in telemetry |
| `DEMO_TELEMETRY_ENABLED` | `0` | `1` turns on OpenTelemetry export |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | none | Base OTLP/HTTP URL, for example `http://collector:4318`; the entrypoint derives the `/v1/traces`, `/v1/metrics` and `/v1/logs` endpoints |
| `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT` | `http://127.0.0.1:4318/v1/…` | Per-signal endpoints (override the base) |
| `OTEL_METRIC_EXPORT_INTERVAL` | `15000` | Metric export interval in ms |
| `DEMO_RUM_ENABLED` | `0` | `1` adds the browser RUM observer to every page and opens the `/__rum__/v1/*` relay (see [Browser RUM](#browser-rum)) |
| `DEMO_RUM_OTLP_ENDPOINT` | the backend's OTLP endpoints | Base OTLP/HTTP URL for browser telemetry only, if it should go to a different Collector |
| `DEMO_TRAFFIC_BACKEND` | `process` in the image | How `demostore traffic` runs: `process` (detached background process) or `systemd` (`demostore-traffic.service`) |

All state (SQLite database and session key) is in the `/var/lib/demostore` volume. The container runs as UID 10001. If you bind-mount a host directory instead of a named volume, make it writable for that UID.

## Fault scenarios

The **Lab controls** panel changes checkout behaviour for *every* session (shared mode). Browsing, health and the controls themselves stay fast.

| Scenario | HTTP | Result |
| --- | --- | --- |
| `normal`, `recovery` | 200 | Successful 4-service transaction; order saved |
| `slow` | 200 | Bank waits for the configured delay (100–10000 ms) |
| `warning` | 200 | Success with a low-stock warning |
| `payment-degraded` | 200 | Success; bank responds in about 650 ms |
| `retry` | 200 | First bank attempt fails (503), the second succeeds; both are visible |
| `notification-warning` | 200 | Order saved; receipt left pending for retry |
| `error` | 500 | Payment service raises a controlled error; inventory is released |
| `timeout` | 502 | Payment gives up after 1 s; the bank still finishes at about 3 s |
| `database-error` | 503 | Order save fails; payment voided and inventory released (compensation) |
| `out-of-stock` | 409 | Business rejection; payment is never called |
| `card-declined` | 402 | Business rejection by the mock bank; inventory released |

4xx results are **business rejections**, not technical errors; telemetry keeps them separate (`transaction.outcome = success | error | rejected`).

## Operator CLI

The `demostore` command is available inside the container. It signs in as a *synthetic* session and uses per-request scenarios, so it never changes the shared mode.

```bash
docker exec demostore demostore status                       # services, release, current mode
docker exec demostore demostore run slow --delay-ms 1500     # one transaction; prints HTTP, TXN id, trace id
docker exec demostore demostore run card-declined --json
docker exec demostore demostore transactions --limit 5
docker exec demostore demostore transaction TXN-00000001
```

### Background traffic

`demostore traffic` sends a steady, bounded stream of synthetic transactions (at most 4 in flight) for a fixed time, so APM views and Telemetry query items have data to show.

```bash
docker exec demostore demostore traffic start mixed --rps 1 --duration 600   # 10 minutes of mixed outcomes
docker exec demostore demostore traffic status                               # counters, current phase, last trace id
docker exec demostore demostore traffic stop                                 # drains in-flight requests, then stops
```

| Profile | Mix |
| --- | --- |
| `normal` | Healthy checkouts only |
| `mixed` | 60% normal plus warnings, slow, declined, out-of-stock, timeouts, errors and retries |
| `degraded` | 40% normal; the rest slow, degraded payments, warnings, retries and timeouts |
| `incident` | Four phases of `--phase-seconds` each (default 15 s): healthy → degraded → incident → recovery |

Limits: `--rps` 0.1–5, `--duration` 1–3600 s (default 120 s; `incident` defaults to 4 phases). `--seed` makes the scenario sequence repeatable. Starting again replaces the running generator.

Inside the container the generator runs as a background process (`DEMO_TRAFFIC_BACKEND=process`); its output is in `/var/lib/demostore/traffic-worker.log`. It stops with the container and doesn't restart on its own. On a VM with the original systemd units, the same commands drive `demostore-traffic.service` (run them as root).

## Sending telemetry to Zabbix (or any OTLP backend)

The app exports **OTLP over HTTP (protobuf)**. A Zabbix 8.0 APM-enabled proxy receives **OTLP/gRPC** on port 4317, so put an OpenTelemetry Collector in between:

```yaml
# otel-collector config (Collector 0.162+; older versions name the exporter "otlp")
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  batch: {}
exporters:
  otlp_grpc/zabbix:
    endpoint: <zabbix-apm-proxy>:4317
    tls:
      insecure: true   # lab only; configure TLS for anything else
service:
  pipelines:
    traces:  { receivers: [otlp], processors: [batch], exporters: [otlp_grpc/zabbix] }
    metrics: { receivers: [otlp], processors: [batch], exporters: [otlp_grpc/zabbix] }
    logs:    { receivers: [otlp], processors: [batch], exporters: [otlp_grpc/zabbix] }
```

Then run DemoStore with:

```bash
-e DEMO_TELEMETRY_ENABLED=1 -e OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-host>:4318
```

> **Collector on the same host as the container?** Don't use `127.0.0.1`: inside the container it is the container itself. Point DemoStore at the host instead, `OTEL_EXPORTER_OTLP_ENDPOINT=http://host.containers.internal:4318` (Podman) or `http://host.docker.internal:4318` (Docker; on Linux add `--add-host=host.docker.internal:host-gateway`). The Collector's HTTP receiver must then listen on more than loopback (`0.0.0.0:4318` as above, followed by a Collector restart); block port 4318 from the rest of the network with a firewall.
>
> Quick check from inside the container (prints `200`):
> ```bash
> podman exec demostore python -c "import urllib.request as u; print(u.urlopen(u.Request('http://host.containers.internal:4318/v1/traces', data=b'', headers={'Content-Type':'application/x-protobuf'}), timeout=3).status)"
> ```

In the Zabbix proxy's frontend configuration, **APM → Data collection enabled** must be ticked, or the proxy won't open port 4317.

Zabbix stores incoming metrics in ClickHouse (for example `otel_metrics_sum` and `otel_metrics_histogram`); it doesn't create items from them automatically. Use the APM views, or create **Telemetry query** items to turn them into item history and triggers. Traces and logs appear only when the store receives real requests (health checks are excluded); an idle store sends metrics only.

**What you get:**

| Signal | Content |
| --- | --- |
| Traces | Services `demostore`, `demostore-inventory`, `demostore-payment`, `demostore-bank`. Business spans include `checkout`, `cart.validate`, `pricing.quote`, `inventory.reserve`, `payment.authorize`, `bank.charge`, `order.save` and `notification.enqueue`. HTTP code in `http.response.status_code`. |
| Metrics (cumulative per worker) | `demostore.http.server.requests`, `http.server.request.duration`, `demostore.checkout.count`, `demostore.checkout.duration`, `demostore.business.operations`, `demostore.business.operation.duration`, `demostore.business.warnings`, `demostore.checkout.active` |
| Logs | Event names such as `checkout.completed`, `checkout.failed`, `checkout.rejected`, `notification.deferred`, `lab.mode.changed`, with readable bodies (`Checkout failed transaction=TXN-… stage=payment reason=provider_timeout …`) and trace/span IDs |

Every request is labelled `lab.traffic_type = manual | synthetic | unclassified`. Synthetic sessions start at `/login?traffic=synthetic` (for example a Zabbix Browser item), and CLI traffic is always synthetic. Exclude synthetic traffic from "real user" figures.

## Browser RUM

RUM measures what each visitor's browser actually experienced: page load, Web Vitals, API calls, the checkout click until the result is visible, and JavaScript errors. A small OpenTelemetry observer (built from [`rum/`](rum/) into the image) runs in the page and reports as service **`demostore-browser`**.

```text
Browser ── GET /, /login ─────────────► demostore   (page includes <script src="/__rum__/rum-v1.js?v=…">)
        ── POST /__rum__/v1/{traces,metrics,logs} (OTLP/JSON) ─► demostore relay ─► Collector :4318 ─► Zabbix proxy :4317
        ── fetch /api/checkout + traceparent ─► demostore ─► payment ─► bank     (one trace, browser to bank)
```

Everything stays on the store's own origin, so there is no CORS and the Collector never has to be reachable from browsers.

### Turn it on

Add `-e DEMO_RUM_ENABLED=1` to a container that already sends backend telemetry. With the Collector on the same Podman host:

```bash
podman run -d --name demostore -p 8080:8000 \
  -e DEMO_TELEMETRY_ENABLED=1 -e DEMO_RUM_ENABLED=1 \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=http://host.containers.internal:4318 \
  -v demostore-data:/var/lib/demostore \
  ghcr.io/victorbcredidio/demostore:latest
```

No Collector change is needed: the store relays browser data as OTLP/JSON to the same `/v1/traces`, `/v1/metrics` and `/v1/logs` endpoints as the backend. To send browser data elsewhere, set `DEMO_RUM_OTLP_ENDPOINT`. `/api/lab/status` reports `rum_enabled`; if the bundle is missing the store logs an error and runs without RUM.

### What is collected, and what isn't

| Collected (bounded, fixed vocabulary) | Never collected |
| --- | --- |
| Navigation timing; Web Vitals TTFB, FCP, LCP, INP, CLS (each update with `web.vital.id` and `web.vital.sequence`); timing of `store.js`, `store.css` and the observer itself; long tasks; same-origin `/api/*` call timing and status; checkout click → visible result (`browser.checkout`); JavaScript error *categories*; clicks on a fixed set of buttons | Input values, credentials, cookies, CSRF tokens, request/response bodies, full URLs and query strings, page text, console output, exception messages and stacks, session replay, screenshots, persistent user IDs |

Browser spans: `browser.navigation`, `browser.web_vital`, `browser.resource`, `browser.long_task`, `browser.action`, `browser.checkout`, `browser.error`, `GET|POST /api/…`. Metrics (delta temporality): `demostore.browser.http.duration`, `demostore.browser.checkout.duration`, `demostore.browser.long_task.duration`, `demostore.browser.errors`. Logs: `browser.page.observed`, `browser.web_vital`, `browser.checkout.visible_result`, `browser.error`.

Labels on every browser signal: `lab.traffic_type` (`synthetic` for `/login?traffic=synthetic` and for automated browsers such as Selenium; `manual` otherwise), `page.route` (`/`, `/login` or `other`), `rum.page.id` (random per page view, not per person), `lab.browser_simulation`. The resource carries `service.namespace=user-experience-lab` and `service.version` = the store's `DEMO_RELEASE_ID`, the same as the backend.

The **Browser RUM lab** panel inside *Lab controls* creates one-off browser problems in that tab: a 600 ms stall (long task and a poor INP), a delayed layout shift (CLS), a JavaScript error and an unhandled rejection. After using it, that page's data is marked `lab.browser_simulation=true`.

### Find it in Zabbix

Checked on Zabbix 8.0.0. Browser data shows up about 10–30 seconds after the visit; Web Vitals are completed when the tab is hidden or closed.

**See one checkout from the browser down to the bank**

1. Open the store in a browser, sign in, add a product and click **Checkout**.
2. In Zabbix, go to **APM → Traces**.
3. In the filter, type `browser.checkout` in **Operation name** and click **Apply**. Each row is one checkout started in a browser: *Service name* `demostore-browser`, with the number of spans next to it (19 for a normal checkout).
4. Click a row. The **Span tree** opens on the right:

   ```text
   demostore-browser    browser.checkout          ← the click until the result is visible
   demostore-browser      POST /api/checkout      ← the request, as the browser saw it
   demostore                POST /api/checkout    ← the same request, as the server saw it
   demostore                  checkout → cart.validate, pricing.quote, POST /reserve, POST /authorize, order.save …
   demostore-payment            payment.authorize → POST /charge
   demostore-bank                 bank.charge
   ```

To find **one particular** checkout, put its trace ID in the **Trace ID** field instead. The store returns it in the `X-Trace-Id` response header (browser developer tools → Network → `checkout`).

**Filter tips (APM → Traces)**

- **Service name** is a text search over *every* span of a trace, not a list: `demostore` matches all five services. `demostore-browser` also lists many one-span traces, because each Web Vital, file load and button click is its own small trace. Use **Operation name** to narrow down: `browser.checkout`, `browser.web_vital`, `browser.error`, `browser.navigation`.
- **Span attributes**, for example `lab.traffic_type` *Equals* `manual`, keeps only people and drops Zabbix Browser items and other automation. Other useful keys: `ui.outcome`, `web.vital.name`, `web.vital.rating`, `error.type`, `http.response.status_code`.
- **Status → Error** shows failed checkouts and the browser lab's simulated JavaScript errors.

**Logs and metrics**

- **APM → Logs:** **Service name** `demostore-browser`. There is no event-name filter in this view; filter with **Body** instead: `browser web_vital`, `browser error`, `browser checkout visible_result` or `browser page observed`. The values are in the log attributes (`web.vital.name`, `web.vital.value`, `web.vital.rating`).
- **APM → Metrics:** **Service name** `demostore-browser`: `demostore.browser.http.duration`, `demostore.browser.checkout.duration`, `demostore.browser.long_task.duration`, `demostore.browser.errors`.

To confirm arrival directly in ClickHouse, run this on the ClickHouse host. If your ClickHouse user has a password, add `--user <user> --password`.

```bash
clickhouse-client -q "
SELECT 'spans' AS signal, count() AS rows, max(Timestamp) AS latest FROM zabbix.otel_traces WHERE ServiceName='demostore-browser' AND Timestamp > now() - INTERVAL 15 MINUTE
UNION ALL SELECT 'logs', count(), max(Timestamp) FROM zabbix.otel_logs WHERE ServiceName='demostore-browser' AND Timestamp > now() - INTERVAL 15 MINUTE
UNION ALL SELECT 'histograms', count(), max(TimeUnix) FROM zabbix.otel_metrics_histogram WHERE ServiceName='demostore-browser' AND TimeUnix > now() - INTERVAL 15 MINUTE
UNION ALL SELECT 'sums', count(), max(TimeUnix) FROM zabbix.otel_metrics_sum WHERE ServiceName='demostore-browser' AND TimeUnix > now() - INTERVAL 15 MINUTE"

# One checkout, browser to bank (paste a trace ID from X-Trace-Id):
clickhouse-client -q "SELECT ServiceName, SpanName, SpanAttributes['lab.traffic_type'] FROM zabbix.otel_traces WHERE TraceId='<trace-id>' ORDER BY Timestamp"
```

### Relay safety rules

`/__rum__/v1/{traces,metrics,logs}` accepts `POST` with `Content-Type: application/json` only (others: 405 or 415), at most 256 KiB (413), and 10 requests/s per client address with a burst of 30 (429; counted per Gunicorn worker). Browser headers are not forwarded, redirects are not followed, the upstream URL is fixed by configuration, and the upstream timeout is 2 s (502 if the Collector is unreachable; shopping is unaffected). Any other `/__rum__/` path is 404. The relay is excluded from backend traces and metrics, so RUM doesn't produce telemetry about itself.

### HTTP vs HTTPS

The container serves plain HTTP. On `localhost`/`127.0.0.1` a browser treats that as a secure context; from another machine (`http://<host>:8080`) it doesn't. The observer itself uses no secure-context-only API. In a test from another machine over plain HTTP with Chrome, the checkout trace, logs, metrics and the TTFB, FCP, INP and CLS Web Vitals all arrived in Zabbix; LCP didn't appear in that one run. Each page records the situation as `browser.secure_context` on the `browser.page.observed` log.

For HTTPS, put a TLS reverse proxy (NGINX, Caddy, Traefik) in front of port 8000 that passes the path through unchanged, and run the store with `-e DEMO_COOKIE_SECURE=1`. `/__rum__/` must be proxied like any other path; don't cache or buffer-rewrite it. Behind a proxy all visitors share the proxy's address for the rate limit.

## Development

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.lock.txt
python -m unittest discover -s tests -p "test_*.py" -v     # 72 tests (one is Linux-only), no network needed
```

`tests/smoke_http.py --base-url http://localhost:8080` runs live checks against a running instance. It changes the shared mode temporarily and restores it afterwards.

The browser observer (Node 24, versions pinned in `rum/package-lock.json`):

```bash
cd rum
npm ci --ignore-scripts
npm test                     # route/privacy policy tests
npm run build                # writes rum/dist/rum-v1.js, LICENSES.txt, manifest.json
# against a running store with DEMO_RUM_ENABLED=1 and DEMO_TELEMETRY_ENABLED=1:
RUM_BASE_URL=http://127.0.0.1:8080 RUM_CHROME_PATH=/usr/bin/google-chrome node browser_acceptance.mjs
```

To run the store from source with RUM, set `DEMO_RUM_ENABLED=1` and `DEMO_RUM_DIR=<repo>/rum/dist`.

Every push to `main` runs the unit tests, builds the image (including the observer's policy tests and bundle build), starts it and runs `smoke_http.py`, CLI checks and a short traffic-generator run against the running container. A second container then runs with RUM on next to a throwaway Collector; headless Chrome logs in, shops, checks out and triggers the browser simulations, and `tests/rum_e2e_check.py` asserts that the Collector received `demostore-browser` spans, metrics and logs, and that checkout traces contain browser and backend spans under one trace ID. Only then is the image published to `ghcr.io/victorbcredidio/demostore`, tagged `latest` and `sha-<commit>`; `v*` git tags add a version tag.

## Limitations

- Mock dependencies only; compensation is best-effort and receipts are never sent.
- One shared fault mode for all users.
- Tuned for demonstrations, not load testing.
- OpenTelemetry's browser SDKs are still experimental upstream; the pinned versions (`rum/package.json`) are the ones tested here. Missing Web Vitals mean "not reported", not zero; INP needs an interaction, and the last export of a page can be lost if the browser is killed.
- The initial HTML page load is not part of a browser trace (the observer isn't running yet); only `/api/*` calls made after it loads join backend traces.
- Third-party RUM dashboards built for the standard OpenTelemetry web auto-instrumentation (`documentLoad`, user-interaction and fetch spans with `session.id` and `url.full`, Web Vitals as `rum.*` log attributes) show DemoStore's browser data only partly: requests and errors appear, but sessions, page loads and Web Vitals stay empty. The core Zabbix APM views above show everything.
