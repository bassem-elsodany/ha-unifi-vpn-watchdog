# Changelog

## 0.7.1
- The UI now uses the full window width: the network map adds columns as the window grows, the Settings cards and the alert editor flow
  into columns, and nothing is capped at a fixed width (it still collapses to one column on narrow screens).

## 0.7.0
- **Network map replaces the tunnel table.** The Status page shows one card per VLAN (VLAN id, subnet, device count, managing group).
  Inside it: its devices, an animated path through the VPN client that is switched on for that VLAN, and the internet; the other VPN
  clients that carry the VLAN sit in a collapsible list with status dots. Devices that bypass the VPN (for example an exit-policy for one
  client) are listed on their VLAN. VLANs that use a VPN come first; VLANs with no VPN policy show "direct to the internet".
- Click any VPN client for a details panel: status, traffic, policy, fallback position, last failure, and actions: switch the group to it,
  test it, move it up/down, add it to or remove it from the fallback order. Escape closes the panel.

## 0.6.0
- **No spare tunnels.** Only the tunnel in use is connected; every other tunnel in your fallback order is disconnected. Several tunnels
  up for the same VLAN let traffic leave through different exit IPs and look odd to firewalls and the VPN provider. The `standby` section
  was removed (an old config containing it is rejected with that explanation). While testing a higher tunnel for failback that one is
  connected briefly.
- Status page: every column of the tunnel table sorts (click a header again to reverse); the choice is remembered.

## 0.5.1
- **Groups are yours to create.** Settings > VPN groups: add, rename and delete groups, and tick each group's networks. A fresh install
  starts with no group at all (nothing from the author's network is built in) and only watches until you add one.

## 0.5.0
- **Every tunnel keeps its own routing policy.** A switch turns the new tunnel's policy on and then the old one off (make before
  break). Before, one policy was re-pointed and renamed, so the previous tunnel's policy vanished. Policies are never renamed or
  moved; a tunnel that has none gets one created. `switching.rename_route_to_tunnel` and `groups[].route_id` were removed.
- **No spare tunnels by default** (`standby.warm` is now 0): only the tunnel in use is connected, instead of also connecting the
  next two in your list.
- The simulation mode was removed. A fresh install with no fallback order only watches; it acts once you set one. Old config files
  that still contain the `dry_run` line must delete it.

## 0.4.1
- With no fallback order set the watchdog still shows the tunnel in use, checks its health and alerts if it goes down (it just
  never switches). Before, the active tunnel showed as "none". The "no fallback order set" warning is logged once, not every cycle.

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
  quarantine with back-off, failback, warm standby.
- Web UI (status, jobs, start/stop, force switch, validating config editor), Prometheus metrics.
- Home Assistant: ingress panel, MQTT discovery entities, notifications through the Supervisor.
- Optional probe agent for real exit-IP checks through a canary client.
