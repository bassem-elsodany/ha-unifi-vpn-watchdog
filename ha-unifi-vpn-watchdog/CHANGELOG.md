# Changelog

## 0.4.0
- **You set the order.** The fallback order is a numbered list of tunnels (#1, #2, #3 ...) that you choose and arrange: pick a
  tunnel per row, move rows with the arrows or by typing a position, add all remaining tunnels in one click. No steps,
  patterns, groups or alphabetical rules. #1 is used first; if the active tunnel fails the next ones are tried in that order, and
  traffic moves back up the list when a higher tunnel recovers. Tunnels not in the list are never used.
- An empty order is allowed: a fresh install only watches until you set it.
- `ladder` (0.3.0) is replaced by `order`; the old key is rejected with instructions, nothing is converted.
- `{step}` / `{previous_step}` alert placeholders became `{position}` / `{previous_position}`; the HA sensor is "Fallback position".

## 0.3.0
- **Name-agnostic.** Tunnel names are never interpreted: no country, city or naming-convention assumptions anywhere.
  A fallback order is a list of steps; each step is a set of tunnels you pick by name or pattern, with any label you like.
  The Settings tab has a tunnel checklist per step, a preferred tunnel, and an optional "expect exit country" you type.
- Removed settings (`naming`, ladder `country`, `switching.prefer_different_city`) are rejected with a message saying what to do
  instead; nothing is converted or guessed. The `{country}`, `{city}` and `{previous_country}` alert placeholders became
  `{step}` and `{previous_step}`.
- **Safe mode:** if the config is rejected at start-up the web UI still starts (failover off) with the error and the YAML
  editor, and normal operation begins as soon as the file is valid.
- Status page: every tunnel lists the routing policies that use it (what they carry, on/off, kill switch, which are managed
  by the watchdog), plus a card for policies that bypass the VPN.
- The HA entity "Exit country" became "Fallback step".

## 0.2.0
- Settings are now a form (Settings tab), not YAML: check interval, thresholds, switch limits, failback, spare tunnels,
  exit-IP test, per-group networks and an orderable country/server list, with live explanations ("down after about 45 s").
  Values are validated before saving; the previous file is kept as `config.yaml.bak`. The YAML editor remains under Advanced.
- Alerts: every alert (switch, failback, exhausted, leak, recovered, blocked, startup, config_error) can be turned on or off
  and its title and message edited with {placeholders}. The Settings tab says when each one is sent.
- The page is left-aligned and full width.
- Fix: saving settings failed for configs that used `${VAR}` inside inline `{...}` YAML.

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
