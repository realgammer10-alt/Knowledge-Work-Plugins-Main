---
description: Check service health, configure authorized API endpoint probes, or open the local live monitoring dashboard.
argument-hint: "[status | configure endpoints | dashboard] [service or incident context]"
---

# Monitor services

Help the user check service availability and investigate endpoint health.

- If `~~monitoring` is connected and the request concerns production, query the existing monitoring platform first. Report the source and time window; do not imply that a live check was run unless a tool actually returned the result.
- Otherwise, use the local Service Monitoring workflow in [the service-monitoring skill](../skills/monitoring/SKILL.md). Ask the user for an explicit list of endpoints they own or are authorized to monitor, or help them create a JSON configuration file with the documented schema.
- Never infer endpoints by scanning hosts, ports, or address ranges. Do not monitor a target without authorization.
- Explain how to start the local monitor and open its loopback-only dashboard. The dashboard makes GET requests only to the configured endpoints, reports status/latency/failure counts, and keeps history in memory until the process exits.
- If an endpoint is unhealthy, report the observed status and error, distinguish observed facts from hypotheses, and offer a next diagnostic step. Do not claim to page or alert anyone; this local tool does not send alerts.
