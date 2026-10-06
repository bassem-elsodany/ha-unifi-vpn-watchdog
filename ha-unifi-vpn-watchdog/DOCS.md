# HA UniFi VPN Watchdog

Monitors your UniFi WireGuard VPN clients and moves the policy route to another tunnel from your fallback order when a
tunnel stops carrying traffic.

## First start
1. **Configuration tab:** set `unifi_api_key` (UniFi > Settings > Integrations > API key) and, if needed, `unifi_url`.
2. Start the add-on. It creates `config.yaml` in the add-on config folder and starts in **dry-run**: it logs and
   alerts what it *would* do but changes nothing.
3. Open **VPN Watchdog** in the sidebar: status, events, start/stop per group, force switch, and a **Settings** form for
   everything (check interval, thresholds, fallback order, alerts). **Advanced** has the raw YAML.
4. When the decisions look right, press **Go live**.

## Home Assistant integration
- **Sidebar panel** (ingress): no extra login, controls are protected by your HA session.
- **Entities** (MQTT discovery). With the Mosquitto add-on nothing needs configuring. With an external broker (the one HA's MQTT
  integration uses) fill `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` in the Configuration tab. For the Mosquitto add-on use host
  `core-mosquitto`, port `1883` and a Home Assistant user (create a dedicated one under Settings → People → Users). Entities: active tunnel, fallback step, healthy, last decision,
  "Failover paused" switch and "Force tunnel" select, grouped under a "VPN <group>" device.
- **Notifications:** panel → *Config* tab → *Notifications*: choose from the notify services Home Assistant reports (phones,
  persistent notification, ...), press *Use this*, then *Send test*. The default is a persistent notification.
- **Logs:** the add-on **Log** tab.

## Probe agent (recommended)
A tunnel can report CONNECTED while passing nothing. For a real exit-IP test the watchdog points a *canary* client
at each tunnel and asks it for its public IP. An add-on has no MAC address of its own, so run the small agent on
a host that does (for example a macvlan container) and set `probe.mode: remote` in the config:

    docker run -d --network canary_net --mac-address 02:42:0a:00:3c:c8 --ip 10.0.60.200 \
      ha-unifi-vpn-watchdog agent --config /config/config.yaml

See the project README for the full compose file.
