# DemoStore

A small, deterministic online store for **monitoring demonstrations**. It covers synthetic browser checks, backend APM with OpenTelemetry, and later browser RUM. You can switch checkout between healthy, slow, failing and recovering on demand, and every request produces readable traces, metrics and logs.

> **Test-only application.** It uses a public demo account, has no real payments and stores simulated orders only. Don't expose it to the Internet.

## What's inside

```text
Browser ──► demostore (Flask/Gunicorn, :8000)
               ├─ HTTP ─► demostore-inventory (127.0.0.1:8101)
               └─ HTTP ─► demostore-payment   (127.0.0.1:8102) ─► demostore-bank (127.0.0.1:8103)
            SQLite: orders, transaction ledger, receipt outbox, shared fault mode
            Optional OTLP/HTTP export of traces, metrics and logs from all four services
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

The background traffic generator (`demostore traffic start|stop`) is designed for the systemd deployment and is not available in the container.

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

In the Zabbix proxy's frontend configuration, **APM → Data collection enabled** must be ticked, or the proxy won't open port 4317.

**What you get:**

| Signal | Content |
| --- | --- |
| Traces | Services `demostore`, `demostore-inventory`, `demostore-payment`, `demostore-bank`. Business spans include `checkout`, `cart.validate`, `pricing.quote`, `inventory.reserve`, `payment.authorize`, `bank.charge`, `order.save` and `notification.enqueue`. HTTP code in `http.response.status_code`. |
| Metrics (cumulative per worker) | `demostore.http.server.requests`, `http.server.request.duration`, `demostore.checkout.count`, `demostore.checkout.duration`, `demostore.business.operations`, `demostore.business.operation.duration`, `demostore.business.warnings`, `demostore.checkout.active` |
| Logs | Event names such as `checkout.completed`, `checkout.failed`, `checkout.rejected`, `notification.deferred`, `lab.mode.changed`, with readable bodies (`Checkout failed transaction=TXN-… stage=payment reason=provider_timeout …`) and trace/span IDs |

Every request is labelled `lab.traffic_type = manual | synthetic | unclassified`. Synthetic sessions start at `/login?traffic=synthetic` (for example a Zabbix Browser item), and CLI traffic is always synthetic. Exclude synthetic traffic from "real user" figures.

## Development

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.lock.txt
python -m unittest discover -s tests -p "test_*.py" -v     # 58 tests, no network needed
```

`tests/smoke_http.py --base-url http://localhost:8080` runs live checks against a running instance. It changes the shared mode temporarily and restores it afterwards.

Every push to `main` runs the unit tests, builds the image, starts it and runs `smoke_http.py` plus CLI checks against the running container. Only then is the image published to `ghcr.io/victorbcredidio/demostore`, tagged `latest` and `sha-<commit>`; `v*` git tags add a version tag.

## Limitations

- Mock dependencies only; compensation is best-effort and receipts are never sent.
- One shared fault mode for all users.
- Tuned for demonstrations, not load testing.
- The browser RUM bundle and NGINX injection used in the original lab are not part of this repository yet.
