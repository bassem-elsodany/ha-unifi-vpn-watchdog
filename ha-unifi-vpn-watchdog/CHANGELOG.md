# Changelog

## 0.1.0
- First release: UniFi WireGuard client health checks, ladder-based failover (same country other city, then next country),
  quarantine with back-off, failback, warm standby, dry-run by default.
- Web UI (status, jobs, start/stop, force switch, validating config editor), Prometheus metrics.
- Home Assistant: ingress panel, MQTT discovery entities, notifications through the Supervisor.
- Optional probe agent for real exit-IP checks through a canary client.
