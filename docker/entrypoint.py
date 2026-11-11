#!/usr/bin/env python3
"""Container entrypoint for DemoStore.

Runs the storefront (Gunicorn on 0.0.0.0:$DEMO_PORT) plus the three mock services it
calls over loopback HTTP: inventory (127.0.0.1:8101), payment (:8102) and bank (:8103).
If any process exits, all of them are stopped so the container exits and the restart
policy can recover it. SIGTERM/SIGINT stop everything gracefully, which lets each
worker flush its pending telemetry.
"""
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
import urllib.request

APP_DIR = Path(os.environ.get("DEMO_APP_DIR", "/opt/demostore/app"))
DEPENDENCIES = (("inventory", 8101), ("payment", 8102), ("bank", 8103))
STOP_TIMEOUT_SECONDS = 40


def log(message):
    print(f"[entrypoint] {message}", flush=True)


def prepare_environment():
    data = Path(os.environ.setdefault("DEMO_DATA_DIR", "/var/lib/demostore"))
    data.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DEMO_DATABASE", str(data / "lab.sqlite3"))

    # The session-signing key must survive restarts, or every browser cart becomes invalid.
    if not os.environ.get("DEMO_SECRET_KEY"):
        key_file = data / ".secret_key"
        if not key_file.exists():
            descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_hex(32))
            log(f"generated a persistent session key in {key_file}")
        os.environ["DEMO_SECRET_KEY"] = key_file.read_text().strip()

    # Convenience: one base endpoint instead of three per-signal endpoints.
    base = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if base:
        for name in ("traces", "metrics", "logs"):
            os.environ.setdefault(f"OTEL_EXPORTER_OTLP_{name.upper()}_ENDPOINT", f"{base}/v1/{name}")


def start(name, args, extra_env=None):
    log(f"starting {name}")
    return name, subprocess.Popen(args, cwd=APP_DIR, env={**os.environ, **(extra_env or {})})


def wait_healthy(port, timeout=20.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def main():
    prepare_environment()
    stopping = False

    def request_stop(signum, _frame):
        nonlocal stopping
        log(f"received signal {signum}; stopping")
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    processes = [start(f"demostore-{component}", [sys.executable, "dependency_server.py"],
                       {"DEMO_COMPONENT": component}) for component, _ in DEPENDENCIES]
    for component, port in DEPENDENCIES:
        if not wait_healthy(port):
            log(f"warning: demostore-{component} is not healthy yet on 127.0.0.1:{port}")

    port = os.environ.get("DEMO_PORT", "8000")
    processes.append(start("demostore", [
        sys.executable, "-m", "gunicorn", "--bind", f"0.0.0.0:{port}",
        "--workers", os.environ.get("DEMO_WORKERS", "2"), "--worker-class", "gthread",
        "--threads", os.environ.get("DEMO_THREADS", "2"), "--timeout", "30", "--graceful-timeout", "30",
        "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]))

    exit_code = 0
    while not stopping:
        for name, process in processes:
            code = process.poll()
            if code is not None:
                log(f"{name} exited with code {code}; stopping the container")
                exit_code = code or 1
                stopping = True
                break
        else:
            time.sleep(0.5)

    # Storefront first, then its dependencies.
    for _name, process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    for name, process in processes:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log(f"{name} did not stop in time; killing it")
            process.kill()
            process.wait()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
