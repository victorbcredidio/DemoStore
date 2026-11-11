"""Operator input limits and finite traffic with bounded pending work."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from demostore_cli import validate_traffic, traffic_worker


class CliTests(unittest.TestCase):
    def test_rejects_unbounded_duration_rate_and_unknown_profile(self):
        valid = {"profile": "mixed", "rps": 1, "duration": 60, "phase_seconds": 15}
        for key, value in (("rps", 0), ("rps", 1000), ("rps", float("nan")), ("duration", 0),
                           ("duration", 99999), ("phase_seconds", 0), ("profile", "PRIVATE")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_traffic({**valid, key: value})

    def test_traffic_backpressure_stop_drains_and_reports_actual_outcomes(self):
        with tempfile.TemporaryDirectory(prefix="demostore-traffic-") as directory:
            path = Path(directory) / "state.json"
            stop, release = threading.Event(), threading.Event()
            self.addCleanup(release.set)
            self.addCleanup(stop.set)
            def execute(*_, **__):
                release.wait(5)
                return {"outcome": "rejected", "transaction_id": "TXN-00000001", "trace_id": "a" * 32, "warnings": []}
            config = {"profile": "mixed", "rps": 5, "duration": 4, "phase_seconds": 1}
            thread = threading.Thread(target=traffic_worker, args=(config, path, stop, execute))
            thread.start()
            try:
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    if path.exists():
                        state = json.loads(path.read_text())
                        if state["saturated"] > 0:
                            break
                    time.sleep(.02)
                self.assertGreater(state["saturated"], 0)
                self.assertLessEqual(state["in_flight"], 8)
                stop.set()
                release.set()
                thread.join(5)
                self.assertFalse(thread.is_alive())
                state = json.loads(path.read_text())
                self.assertFalse(state["running"])
                self.assertEqual(state["in_flight"], 0)
                self.assertEqual(state["submitted"], state["completed"])
                self.assertEqual(state["rejected"], state["completed"])
            finally:
                stop.set()
                release.set()
                thread.join(5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
