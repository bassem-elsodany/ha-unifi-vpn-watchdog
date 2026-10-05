# Changelog

## 0.1.4
- Fix: the MQTT fields (`mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password`) did not appear in the
  Configuration tab because optional options without defaults are hidden by Home Assistant. They now have defaults.

## 0.1.3
- MQTT: the log now shows the Supervisor's actual reason when it offers no broker, with a fix hint.
- Notifications: the panel's Config tab lists the notify services found in Home Assistant (each phone's
  `notify.mobile_app_*`, persistent notification, ...) in a dropdown, saves the choice and has a "Send test" button.
  The free-text `notify_service` option is gone.

## 0.1.2
- MQTT: if the Supervisor offers no broker (no Mosquitto add-on, e.g. an external broker used by HA's MQTT
  integration), use the new `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` options from the Configuration tab.

## 0.1.1
- Fix: the add-on crashed on start when the Supervisor offered no MQTT service. MQTT failures are now logged as a
  warning and the watchdog keeps running without the HA entities.
- Quieter logs: HTTP library debug output is no longer shown at DEBUG level.

## 0.1.0
- First release: UniFi WireGuard client health checks, ladder-based failover (same country other city, then next country),
  quarantine with back-off, failback, warm standby, dry-run by default.
- Web UI (status, jobs, start/stop, force switch, validating config editor), Prometheus metrics.
- Home Assistant: ingress panel, MQTT discovery entities, notifications through the Supervisor.
- Optional probe agent for real exit-IP checks through a canary client.
