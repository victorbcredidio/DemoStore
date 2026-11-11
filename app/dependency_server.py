"""Start the selected dependency using fixed loopback addresses and bounded workers."""
import os
import sys
from demostore.contracts import PORTS

component = os.environ.get("DEMO_COMPONENT")
if component not in PORTS:
    raise SystemExit("DEMO_COMPONENT must be inventory, payment or bank")
os.execv(sys.executable, [sys.executable, "-m", "gunicorn", "--bind", f"127.0.0.1:{PORTS[component]}",
    "--workers", "1", "--worker-class", "gthread", "--threads", "4" if component == "bank" else "2",
    "--timeout", "30", "--access-logfile", "-", "--error-logfile", "-", "dependency_wsgi:app"])
