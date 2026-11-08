#!/usr/bin/env python3
"""A small, dependency-free health monitor for explicitly configured services."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from json import JSONDecodeError
import json
from pathlib import Path
import socket
from threading import Event, Lock, Thread
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_ENDPOINTS = 50
MAX_CONFIG_BYTES = 128 * 1024
MAX_HISTORY = 60
MAX_WORKERS = 8
MIN_INTERVAL_SECONDS = 5
MAX_INTERVAL_SECONDS = 3600
MAX_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Endpoint:
    name: str
    url: str
    expected_status: int


class NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request: Request, response: Any, code: int, message: str, headers: Any, new_url: str) -> None:
        return None


HTTP_OPENER = build_opener(NoRedirectHandler())


def load_endpoints(config_path: Path) -> list[Endpoint]:
    try:
        with config_path.open("r", encoding="utf-8") as config_file:
            raw = config_file.read(MAX_CONFIG_BYTES + 1)
    except OSError as error:
        raise ValueError(f"Unable to read config file: {error}") from error

    if len(raw.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError(f"Config file exceeds the {MAX_CONFIG_BYTES}-byte limit")

    try:
        document = json.loads(raw)
    except JSONDecodeError as error:
        raise ValueError(f"Invalid JSON at line {error.lineno}, column {error.colno}") from error

    if not isinstance(document, dict) or set(document) != {"endpoints"}:
        raise ValueError('Config must be an object containing only an "endpoints" array')

    entries = document["endpoints"]
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENDPOINTS:
        raise ValueError(f'"endpoints" must contain between 1 and {MAX_ENDPOINTS} entries')

    endpoints: list[Endpoint] = []
    seen_names: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"endpoints[{index}]"
        if not isinstance(entry, dict) or not {"name", "url"}.issubset(entry) or set(entry) - {"name", "url", "expected_status"}:
            raise ValueError(f'{prefix} must contain "name", "url", and optional "expected_status"')

        name, url = entry["name"], entry["url"]
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError(f"{prefix}.name must be a non-empty string of at most 80 characters")
        normalized_name = name.strip()
        if normalized_name.casefold() in seen_names:
            raise ValueError(f"{prefix}.name must be unique")
        seen_names.add(normalized_name.casefold())

        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError(f"{prefix}.url must be a URL of at most 2048 characters")
        try:
            parsed_url = urlsplit(url)
            valid_port = parsed_url.port is None or 1 <= parsed_url.port <= 65535
        except ValueError as error:
            raise ValueError(f"{prefix}.url is invalid: {error}") from error
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or not valid_port
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError(f"{prefix}.url must be an HTTP(S) URL without credentials, query, or fragment")

        expected_status = entry.get("expected_status", 200)
        if type(expected_status) is not int or not 100 <= expected_status <= 599:
            raise ValueError(f"{prefix}.expected_status must be an integer from 100 to 599")
        endpoints.append(Endpoint(normalized_name, url, expected_status))

    return endpoints


def check_endpoint(endpoint: Endpoint, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    checked_at = datetime.now(timezone.utc).isoformat()
    request = Request(endpoint.url, headers={"User-Agent": "KnowledgeWorkHealthMonitor/1.0"})
    error_message = None

    try:
        with HTTP_OPENER.open(request, timeout=timeout) as response:
            status_code = response.status
    except HTTPError as error:
        status_code = error.code
        error.close()
    except (URLError, TimeoutError, socket.timeout, OSError, ValueError) as error:
        status_code = None
        reason = getattr(error, "reason", error)
        error_message = f"Connection failed: {str(reason)[:200]}"

    latency_ms = round((time.monotonic() - started) * 1000, 2)
    is_up = status_code == endpoint.expected_status
    if status_code is not None and not is_up:
        error_message = f"Expected HTTP {endpoint.expected_status}; received HTTP {status_code}"

    return {
        "name": endpoint.name,
        "url": endpoint.url,
        "status": "up" if is_up else "down",
        "lastCheckedAt": checked_at,
        "latencyMs": latency_ms,
        "lastError": error_message,
    }


class MonitorState:
    def __init__(self, endpoints: list[Endpoint], timeout: int) -> None:
        self.endpoints = endpoints
        self.timeout = timeout
        self._lock = Lock()
        self._status: dict[str, dict[str, Any]] = {
            endpoint.name: {
                "name": endpoint.name,
                "url": endpoint.url,
                "status": "unknown",
                "lastCheckedAt": None,
                "latencyMs": None,
                "lastError": None,
                "consecutiveFailures": 0,
                "totalChecks": 0,
                "history": [],
            }
            for endpoint in endpoints
        }

    def check_all(self) -> None:
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(self.endpoints))) as executor:
            futures = {executor.submit(check_endpoint, endpoint, self.timeout): endpoint for endpoint in self.endpoints}
            for future in as_completed(futures):
                results.append(future.result())

        with self._lock:
            for result in results:
                status = self._status[result["name"]]
                status.update(result)
                status["totalChecks"] += 1
                status["consecutiveFailures"] = (
                    0 if result["status"] == "up" else status["consecutiveFailures"] + 1
                )
                status["history"].append(
                    {
                        "checkedAt": result["lastCheckedAt"],
                        "up": result["status"] == "up",
                        "latencyMs": result["latencyMs"],
                    }
                )
                del status["history"][:-MAX_HISTORY]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            endpoints = [dict(value, history=list(value["history"])) for value in self._status.values()]
        up_count = sum(endpoint["status"] == "up" for endpoint in endpoints)
        return {
            "checkedAt": max(
                (endpoint["lastCheckedAt"] for endpoint in endpoints if endpoint["lastCheckedAt"]),
                default=None,
            ),
            "summary": {
                "total": len(endpoints),
                "up": up_count,
                "down": sum(endpoint["status"] == "down" for endpoint in endpoints),
                "unknown": sum(endpoint["status"] == "unknown" for endpoint in endpoints),
            },
            "endpoints": endpoints,
        }


def make_request_handler(state: MonitorState) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            route = urlsplit(self.path).path
            if route == "/api/status":
                body = json.dumps(state.snapshot(), ensure_ascii=False).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            elif route in {"/", "/dashboard.html"}:
                body = (Path(__file__).parent.parent / "dashboard.html").read_bytes()
                content_type = "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return DashboardHandler


def run_monitor(config: Path, port: int, interval: int, timeout: int) -> None:
    endpoints = load_endpoints(config)
    state = MonitorState(endpoints, timeout)
    stop_event = Event()

    def poll() -> None:
        while not stop_event.is_set():
            state.check_all()
            stop_event.wait(interval)

    poll_thread = Thread(target=poll, name="health-check-poller", daemon=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_request_handler(state))
    poll_thread.start()
    print(f"Monitoring {len(endpoints)} configured endpoint(s) at http://127.0.0.1:{server.server_port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping monitor.")
    finally:
        stop_event.set()
        server.shutdown()
        server.server_close()
        poll_thread.join(timeout + 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="Path to a JSON file listing endpoints you own or are authorized to monitor")
    parser.add_argument("--port", type=int, default=8765, help="Local dashboard port (default: 8765)")
    parser.add_argument("--interval", type=int, default=15, help="Seconds between checks, from 5 to 3600 (default: 15)")
    parser.add_argument("--timeout", type=int, default=5, help="Per-request timeout in seconds, from 1 to 30 (default: 5)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be from 1 to 65535")
    if not MIN_INTERVAL_SECONDS <= args.interval <= MAX_INTERVAL_SECONDS:
        parser.error(f"--interval must be from {MIN_INTERVAL_SECONDS} to {MAX_INTERVAL_SECONDS}")
    if not 1 <= args.timeout <= MAX_TIMEOUT_SECONDS:
        parser.error(f"--timeout must be from 1 to {MAX_TIMEOUT_SECONDS}")
    return args


def main() -> int:
    args = parse_args()
    try:
        run_monitor(args.config, args.port, args.interval, args.timeout)
    except ValueError as error:
        print(f"Configuration error: {error}")
        return 2
    except OSError as error:
        print(f"Monitor startup failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
