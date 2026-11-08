import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from monitor import (
    MAX_HISTORY,
    Endpoint,
    MonitorState,
    check_endpoint,
    load_endpoints,
    make_request_handler,
)


class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        responses = {
            "/ok": (200, {}),
            "/empty": (204, {}),
            "/fail": (500, {}),
            "/redirect": (302, {"Location": "/ok"}),
        }
        status, headers = responses.get(self.path, (404, {}))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def log_message(self, format, *args):
        return


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.fixture_thread = Thread(target=cls.fixture_server.serve_forever, daemon=True)
        cls.fixture_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.fixture_server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.fixture_server.shutdown()
        cls.fixture_server.server_close()
        cls.fixture_thread.join(timeout=2)

    def write_config(self, value):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config_path = Path(directory.name) / "endpoints.json"
        config_path.write_text(json.dumps(value), encoding="utf-8")
        return config_path

    def test_loads_valid_config_and_defaults_expected_status(self):
        path = self.write_config({"endpoints": [{"name": " Orders ", "url": self.base_url + "/ok"}]})
        self.assertEqual(load_endpoints(path), [Endpoint("Orders", self.base_url + "/ok", 200)])

    def test_rejects_invalid_config_shapes_and_urls(self):
        invalid_documents = [
            {"unexpected": []},
            {"endpoints": []},
            {"endpoints": [{"name": "a", "url": "https://user:pass@example.com/health"}]},
            {"endpoints": [{"name": "a", "url": "https://example.com/health?token=secret"}]},
            {"endpoints": [{"name": "a", "url": "ftp://example.com/health"}]},
            {"endpoints": [{"name": "a", "url": "https://example.com", "unexpected": True}]},
            {"endpoints": [{"name": "a", "url": "https://example.com"}, {"name": "A", "url": "https://example.org"}]},
            {"endpoints": [{"name": "a", "url": "https://example.com", "expected_status": True}]},
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    load_endpoints(self.write_config(document))

    def test_rejects_invalid_json_and_oversized_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endpoints.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid JSON"):
                load_endpoints(path)
            path.write_text(" " * (128 * 1024 + 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "exceeds"):
                load_endpoints(path)

    def test_check_endpoint_reports_expected_status_and_latency(self):
        result = check_endpoint(Endpoint("OK", self.base_url + "/ok", 200), timeout=2)
        self.assertEqual(result["status"], "up")
        self.assertIsInstance(result["latencyMs"], float)
        self.assertTrue(result["lastCheckedAt"])
        self.assertIsNone(result["lastError"])

    def test_check_endpoint_reports_http_errors_without_following_redirects(self):
        failed = check_endpoint(Endpoint("failed", self.base_url + "/fail", 200), timeout=2)
        redirected = check_endpoint(Endpoint("redirect", self.base_url + "/redirect", 200), timeout=2)
        self.assertEqual(failed["status"], "down")
        self.assertIn("HTTP 500", failed["lastError"])
        self.assertEqual(redirected["status"], "down")
        self.assertIn("HTTP 302", redirected["lastError"])

    def test_check_endpoint_accepts_configured_non_default_status(self):
        result = check_endpoint(Endpoint("empty", self.base_url + "/empty", 204), timeout=2)
        self.assertEqual(result["status"], "up")

    def test_check_endpoint_reports_connection_failures(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            unused_port = listener.getsockname()[1]
        result = check_endpoint(Endpoint("offline", f"http://127.0.0.1:{unused_port}/health", 200), timeout=2)
        self.assertEqual(result["status"], "down")
        self.assertIn("Connection failed:", result["lastError"])

    def test_state_tracks_failures_resets_and_bounds_history(self):
        state = MonitorState([Endpoint("service", self.base_url + "/ok", 200)], timeout=2)
        with patch("monitor.check_endpoint", side_effect=[
            {"name": "service", "url": self.base_url + "/ok", "status": "down", "lastCheckedAt": "1", "latencyMs": 1.0, "lastError": "failed"},
            {"name": "service", "url": self.base_url + "/ok", "status": "up", "lastCheckedAt": "2", "latencyMs": 1.0, "lastError": None},
        ] + [
            {"name": "service", "url": self.base_url + "/ok", "status": "up", "lastCheckedAt": str(index), "latencyMs": 1.0, "lastError": None}
            for index in range(MAX_HISTORY + 2)
        ]):
            state.check_all()
            self.assertEqual(state.snapshot()["endpoints"][0]["consecutiveFailures"], 1)
            state.check_all()
            self.assertEqual(state.snapshot()["endpoints"][0]["consecutiveFailures"], 0)
            for _ in range(MAX_HISTORY + 2):
                state.check_all()

        result = state.snapshot()
        self.assertEqual(result["summary"], {"total": 1, "up": 1, "down": 0, "unknown": 0})
        self.assertEqual(result["endpoints"][0]["totalChecks"], MAX_HISTORY + 4)
        self.assertEqual(len(result["endpoints"][0]["history"]), MAX_HISTORY)

    def test_dashboard_and_status_api_are_served_without_cache(self):
        state = MonitorState([Endpoint("service", self.base_url + "/ok", 200)], timeout=2)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_request_handler(state))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        origin = f"http://127.0.0.1:{server.server_port}"

        with urlopen(origin + "/api/status") as response:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            data = json.loads(response.read())
        self.assertEqual(data["summary"]["unknown"], 1)
        self.assertEqual(data["endpoints"][0]["name"], "service")

        with urlopen(origin + "/") as response:
            page = response.read().decode("utf-8")
        self.assertIn('window.setInterval(refresh, 5000)', page)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)


if __name__ == "__main__":
    unittest.main()
