---
name: service-monitoring
description: Set up or inspect service health checks, endpoint availability, response latency, or a live local status dashboard. Trigger with "monitor an API", "check service health", "endpoint status", "service availability", or "monitoring dashboard".
---

# Service Monitoring

Use the local monitor for a small, explicit list of HTTP health endpoints. Only check services the user owns or is authorized to monitor. For production telemetry, historical metrics, or alerting, use the connected `~~monitoring` platform instead.

## Local dashboard

The monitor uses only the Python standard library. Create a JSON configuration file outside the plugin directory:

```json
{
  "endpoints": [
    { "name": "Orders API", "url": "https://orders.example.com/health" },
    { "name": "Catalog API", "url": "https://catalog.example.com/health", "expected_status": 204 }
  ]
}
```

Each endpoint needs a unique name and an HTTP(S) URL without credentials, query strings, or fragments. The default expected status is `200`. The configuration is limited to 50 endpoints; each check uses `GET`, follows no redirects, and times out after five seconds by default.

Run from the repository root:

```bash
python engineering/skills/monitoring/scripts/monitor.py --config path/to/endpoints.json
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Checks start immediately and repeat every 15 seconds. The dashboard refreshes its status every five seconds. Stop the process with Ctrl+C. Customize the polling period and timeout with `--interval` (5–3600 seconds) and `--timeout` (1–30 seconds); change the dashboard port with `--port` (1–65535). The server binds to loopback only and has no configuration-writing API.

The dashboard reports current up/down/unknown status, expected versus received status, response latency, last check time, consecutive failures, and up to 60 recent checks per endpoint. State is held in memory and resets when the process stops; this lightweight tool does not send alerts or replace production monitoring.

Run the local unit and HTTP integration tests from the repository root with `python -m unittest discover -s engineering/skills/monitoring/scripts -p "test_*.py" -v`.

## Production and incident workflows

When a monitoring connector is available, prefer its existing alerts, metrics, and dashboards instead of repeatedly polling production endpoints. Confirm the monitored service, time window, and user authorization before querying. For an incident, summarize the affected service, observed status and latency, when the issue began, impact, actions taken, and the next update time; follow the [incident response workflow](../incident-response/SKILL.md).
