"""DemoStore operator CLI and a finite, bounded, systemd-managed traffic generator."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import http.cookiejar
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from demostore.contracts import SCENARIOS, RUN_ID

STATE_DIR = Path("/var/lib/demostore")
TRAFFIC_UNIT = "demostore-traffic.service"
DEFAULT_BASE_URL = os.environ.get("DEMO_BASE_URL", "http://127.0.0.1:8000")
PROFILES = {
    "normal": (("normal", 100),),
    "mixed": (("normal", 60), ("warning", 8), ("slow", 5), ("card-declined", 7),
              ("out-of-stock", 7), ("timeout", 4), ("error", 3), ("retry", 3), ("notification-warning", 3)),
    "degraded": (("normal", 40), ("slow", 20), ("payment-degraded", 20), ("warning", 10), ("retry", 5), ("timeout", 5)),
}
INCIDENT = ("healthy", "degraded", "incident", "recovery")
INCIDENT_MIX = {
    "healthy": (("normal", 100),), "degraded": PROFILES["degraded"],
    "incident": (("normal", 30), ("timeout", 25), ("error", 15), ("card-declined", 10), ("out-of-stock", 10), ("database-error", 10)),
    "recovery": (("recovery", 100),),
}


class Client:
    def __init__(self, base, marker=""):
        self.base = base.rstrip("/")
        self.marker = marker
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        self.csrf = self.call("GET", "/api/session")[1]["csrf_token"]
        status, data, _ = self.call("POST", "/api/login", {
            "username": os.environ.get("DEMO_USERNAME", "demo_user"),
            "password": os.environ.get("DEMO_PASSWORD", "demo_pass"), "traffic_type": "synthetic"})
        if status != 200:
            raise RuntimeError(f"Lab login failed: HTTP {status}")
        self.csrf = data["csrf_token"]

    def call(self, method, path, data=None):
        headers = {"Accept": "application/json"}
        if self.marker:
            headers["X-Lab-Run-Id"] = self.marker
        encoded = None
        if data is not None:
            encoded = json.dumps(data).encode()
            headers.update({"Content-Type": "application/json", "X-CSRF-Token": self.csrf or ""})
        req = urllib.request.Request(self.base + path, data=encoded, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=15 if path == "/api/checkout" else 5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response), response.headers

    def close(self):
        self.call("POST", "/api/logout", {})


def run_once(base, scenario, product="laptop", quantity=1, delay_ms=1500, marker=""):
    if scenario not in SCENARIOS or product not in {"laptop", "headphones", "bundle"}:
        raise ValueError("Unsupported scenario/product")
    if not 1 <= quantity <= 99 or not 100 <= delay_ms <= 10000:
        raise ValueError("Quantity must be 1-99 and delay 100-10000 ms")
    if marker and not RUN_ID.fullmatch(marker):
        raise ValueError("Invalid validation ID")
    client = Client(base, marker)
    begin = time.perf_counter()
    try:
        for selected in ("laptop", "headphones") if product == "bundle" else (product,):
            status, _, _ = client.call("POST", "/api/cart/items", {"product_id": selected, "quantity": quantity})
            if status != 200:
                raise RuntimeError(f"Could not populate the test cart: HTTP {status}")
        status, body, headers = client.call("POST", "/api/checkout", {"scenario": scenario, "delay_ms": delay_ms})
        transaction = body.get("transaction", {})
        return {"scenario": scenario, "http_status": status, "transaction_id": transaction.get("id"),
                "trace_id": headers.get("X-Trace-Id"), "order_id": body.get("order", {}).get("id"),
                "outcome": transaction.get("outcome", "unknown"), "reason": transaction.get("reason"),
                "stage": transaction.get("stage"), "warnings": transaction.get("warnings", []),
                "duration_ms": round((time.perf_counter() - begin) * 1000), "validation_id": marker or None}
    finally:
        client.close()


def validate_traffic(config):
    if config.get("profile") not in {*PROFILES, "incident"}:
        raise ValueError("Unsupported traffic profile")
    rps, duration, phase = config.get("rps"), config.get("duration"), config.get("phase_seconds", 15)
    if type(rps) not in (int, float) or not 0.1 <= rps <= 5:
        raise ValueError("RPS must be from 0.1 to 5")
    if type(duration) not in (int, float) or not 1 <= duration <= 3600:
        raise ValueError("Duration must be from 1 to 3600 seconds")
    if type(phase) not in (int, float) or not 1 <= phase <= 300:
        raise ValueError("Incident phase must be from 1 to 300 seconds")
    if type(config.get("seed", 17)) is not int:
        raise ValueError("Seed must be an integer")
    return config


def atomic_json(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    for attempt in range(10):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            # Windows readers briefly prevent replacement; Linux rename remains
            # atomic without this retry. Do not hide a persistent access failure.
            if os.name != "nt" or attempt == 9:
                raise
            time.sleep(.01)


def traffic_worker(config, state_path, stop=None, execute=None):
    """At most eight submitted jobs and four executing jobs; no unbounded queue."""
    validate_traffic(config)
    stop = stop or threading.Event()
    execute = execute or run_once
    randomizer = random.Random(config.get("seed", 17))
    lock, slots = threading.Lock(), threading.BoundedSemaphore(8)
    state = {"running": True, "profile": config["profile"], "phase": "healthy", "rps": config["rps"],
             "duration": config["duration"], "started_at": datetime.now(timezone.utc).isoformat(),
             "submitted": 0, "completed": 0, "success": 0, "rejected": 0, "errors": 0,
             "warnings": 0, "saturated": 0, "in_flight": 0, "last_transaction": None, "last_trace_id": None}
    began, next_at = time.monotonic(), time.monotonic()
    def save():
        atomic_json(state_path, state)
    def job(scenario):
        try:
            result = execute(config.get("base_url", DEFAULT_BASE_URL), scenario,
                             delay_ms=1500, marker=config.get("validation_id", ""))
            with lock:
                category = {"success": "success", "rejected": "rejected"}.get(result["outcome"], "errors")
                state[category] += 1
                state["warnings"] += bool(result.get("warnings"))
                state["last_transaction"], state["last_trace_id"] = result.get("transaction_id"), result.get("trace_id")
        except Exception:
            with lock:
                state["errors"] += 1
        finally:
            with lock:
                state["completed"] += 1
                state["in_flight"] -= 1
                save()
            slots.release()
    with ThreadPoolExecutor(max_workers=4) as pool:
        while not stop.is_set() and time.monotonic() - began < config["duration"]:
            elapsed = time.monotonic() - began
            phase = INCIDENT[min(3, int(elapsed / config["phase_seconds"]))] if config["profile"] == "incident" else config["profile"]
            distribution = INCIDENT_MIX[phase] if config["profile"] == "incident" else PROFILES[config["profile"]]
            scenario = randomizer.choices([item[0] for item in distribution], [item[1] for item in distribution])[0]
            with lock:
                state["phase"] = phase
                if slots.acquire(blocking=False):
                    state["submitted"] += 1
                    state["in_flight"] += 1
                    pool.submit(job, scenario)
                else:
                    state["saturated"] += 1
                save()
            next_at += 1 / config["rps"]
            stop.wait(max(0, next_at - time.monotonic()))
    with lock:
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    return state


def require_local_root():
    if os.name != "posix" or os.geteuid() != 0 or STATE_DIR.resolve() != Path("/var/lib/demostore"):
        raise ValueError("Traffic start/stop requires root on the DemoStore VM")


def load_local_account():
    path = Path("/etc/demostore/demostore.env")
    if os.name == "posix" and os.geteuid() == 0 and path.is_file():
        for line in path.read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key in {"DEMO_USERNAME", "DEMO_PASSWORD"}:
                    os.environ.setdefault(key, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="DemoStore transaction and traffic controls")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    run = commands.add_parser("run")
    run.add_argument("scenario", choices=sorted(SCENARIOS))
    run.add_argument("--product", choices=("laptop", "headphones", "bundle"), default="laptop")
    run.add_argument("--quantity", type=int, default=1)
    run.add_argument("--delay-ms", type=int, default=1500)
    run.add_argument("--validation-id", default="")
    run.add_argument("--json", action="store_true")
    history = commands.add_parser("transactions")
    history.add_argument("--limit", type=int, default=10)
    details = commands.add_parser("transaction")
    details.add_argument("id")
    traffic = commands.add_parser("traffic").add_subparsers(dest="traffic_command", required=True)
    start = traffic.add_parser("start")
    start.add_argument("profile", choices=(*PROFILES, "incident"))
    start.add_argument("--rps", type=float, default=1)
    start.add_argument("--duration", type=float)
    start.add_argument("--phase-seconds", type=float, default=15)
    start.add_argument("--seed", type=int, default=17)
    start.add_argument("--validation-id", default="")
    traffic.add_parser("status")
    traffic.add_parser("stop")
    commands.add_parser("_traffic-worker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    load_local_account()
    try:
        if args.command == "run":
            result = run_once(args.base_url, args.scenario, args.product, args.quantity, args.delay_ms, args.validation_id)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print(f"Scenario: {result['scenario']} | HTTP {result['http_status']} | {result['outcome']}")
                print(f"Transaction: {result['transaction_id']} | Order: {result['order_id'] or 'none'}")
                print(f"Trace: {result['trace_id']} | Stage: {result['stage']} | Reason: {result['reason']}")
                print(f"Duration: {result['duration_ms']} ms | Warnings: {', '.join(result['warnings']) or 'none'}")
            return 0 if result.get("transaction_id") else 2
        if args.command == "_traffic-worker":
            config = json.loads((STATE_DIR / "traffic-request.json").read_text())
            stop = threading.Event()
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            print(json.dumps(traffic_worker(config, STATE_DIR / "traffic-state.json", stop)))
            return 0
        if args.command == "traffic":
            if args.traffic_command == "status":
                path = STATE_DIR / "traffic-state.json"
                state = json.loads(path.read_text()) if path.exists() else {"running": False}
                active = subprocess.run(["systemctl", "is-active", "--quiet", TRAFFIC_UNIT]).returncode == 0
                print(json.dumps({**state, "service_active": active, "running": active and state.get("running", False)}, indent=2))
                return 0
            require_local_root()
            if args.traffic_command == "stop":
                subprocess.run(["systemctl", "stop", TRAFFIC_UNIT], check=True)
                print("DemoStore traffic stopped; bounded in-flight requests drained.")
                return 0
            if args.validation_id and not RUN_ID.fullmatch(args.validation_id):
                raise ValueError("Invalid validation ID")
            config = validate_traffic({"profile": args.profile, "rps": args.rps,
                "duration": args.duration if args.duration is not None else args.phase_seconds * 4 if args.profile == "incident" else 120,
                "phase_seconds": args.phase_seconds, "seed": args.seed, "base_url": args.base_url,
                "validation_id": args.validation_id})
            subprocess.run(["systemctl", "stop", TRAFFIC_UNIT], check=True)
            atomic_json(STATE_DIR / "traffic-request.json", config)
            # The unit runs as demostore, and must read the root-created request.
            import grp
            path = STATE_DIR / "traffic-request.json"
            os.chown(path, 0, grp.getgrnam("demostore").gr_gid)
            path.chmod(0o640)
            subprocess.run(["systemctl", "start", TRAFFIC_UNIT], check=True)
            print(json.dumps(config, indent=2))
            return 0
        client = Client(args.base_url)
        try:
            path = "/api/lab/status" if args.command == "status" else "/api/lab/transactions?limit=" + str(args.limit) if args.command == "transactions" else "/api/lab/transactions/" + args.id
            status, result, _ = client.call("GET", path)
            print(json.dumps(result, indent=2))
            return 0 if status == 200 else 2
        finally:
            client.close()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError):
        print("DemoStore command failed. Check the scenario/limits, service health, local permissions and test-account configuration.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
