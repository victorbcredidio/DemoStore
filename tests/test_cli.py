"""Operator input limits and finite traffic with bounded pending work."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import demostore_cli
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


@unittest.skipUnless(sys.platform.startswith("linux"), "the process backend reads /proc")
class ProcessBackendTests(unittest.TestCase):
    """`demostore traffic` without systemd, as used inside the container."""

    def cli(self, *argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = demostore_cli.main(["--base-url", "http://127.0.0.1:9", *argv])
        return code, output.getvalue()

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(.1)
        return False

    def test_start_status_restart_and_stop_a_detached_worker(self):
        with tempfile.TemporaryDirectory(prefix="demostore-traffic-") as directory, \
                mock.patch.object(demostore_cli, "STATE_DIR", Path(directory)), \
                mock.patch.object(demostore_cli, "TRAFFIC_BACKEND", "process"):
            self.addCleanup(demostore_cli.stop_process_worker)
            state_path = Path(directory) / "traffic-state.json"
            def state():
                return json.loads(state_path.read_text()) if state_path.exists() else {}

            self.assertEqual(self.cli("traffic", "start", "mixed", "--rps", "5", "--duration", "60")[0], 0)
            first = demostore_cli.traffic_worker_pid()
            self.assertIsNotNone(first)
            # Port 9 refuses connections, so every job finishes quickly as an error.
            self.assertTrue(self.wait_for(lambda: state().get("completed", 0) > 0))
            code, output = self.cli("traffic", "status")
            status = json.loads(output)
            self.assertEqual((code, status["backend"], status["running"]), (0, "process", True))

            # Starting again replaces the running worker instead of adding a second one.
            self.assertEqual(self.cli("traffic", "start", "normal", "--rps", "2", "--duration", "60")[0], 0)
            second = demostore_cli.traffic_worker_pid()
            self.assertNotIn(second, (None, first))
            self.assertTrue(self.wait_for(lambda: state().get("profile") == "normal"))

            self.assertEqual(self.cli("traffic", "stop")[0], 0)
            self.assertIsNone(demostore_cli.traffic_worker_pid())
            self.assertFalse(state()["running"])
            self.assertFalse(json.loads(self.cli("traffic", "status")[1])["running"])
            self.assertEqual(self.cli("traffic", "stop")[0], 0)  # stopping twice is harmless


if __name__ == "__main__":
    unittest.main(verbosity=2)
