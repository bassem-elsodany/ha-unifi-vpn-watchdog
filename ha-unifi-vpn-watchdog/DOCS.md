# HA UniFi VPN Watchdog

Monitors your UniFi WireGuard VPN clients and switches on the next VPN client from your fallback order when a
tunnel stops carrying traffic.

## First start
1. **Configuration tab:** set `unifi_api_key` (UniFi > Settings > Integrations > API key) and, if needed, `unifi_url`.
2. Start the add-on. It creates `config.yaml` in the add-on config folder.
3. Open **VPN Watchdog** in the sidebar, go to **Settings > VPN groups** and add a group: tick the networks that should use the VPN, then
   add your tunnels under **Fallback order** and number them 1, 2, 3 ... Until you do, the watchdog only watches.
4. Open **VPN Watchdog** in the sidebar: status, events, start/stop per group, force switch, and a **Settings** form for
   everything (check interval, thresholds, fallback order, alerts).
5. When the decisions look right, press **Go live**.

## Home Assistant integration
- **Sidebar panel** (ingress): no extra login, controls are protected by your HA session.
- **Entities** (MQTT discovery). With the Mosquitto add-on nothing needs configuring. With an external broker (the one HA's MQTT
  integration uses) fill `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` in the Configuration tab. For the Mosquitto add-on use host
  `core-mosquitto`, port `1883` and a Home Assistant user (create a dedicated one under Settings → People → Users). Entities: active tunnel, fallback step, healthy, last decision,
  "Failover paused" switch and "Force tunnel" select, grouped under a "VPN <group>" device.
- **Notifications:** panel → *Config* tab → *Notifications*: choose from the notify services Home Assistant reports (phones,
  persistent notification, ...), press *Use this*, then *Send test*. The default is a persistent notification.
- **Logs:** the add-on **Log** tab.

## Before you rely on it
The watchdog only switches your VPN clients on and off. UniFi's own routing policies send the VLANs and devices through whichever client is up, so
keep a routing policy **switched on** in UniFi for every VPN client you put in a group (in the priority order you want). A client whose policy is off
would be switched on but carry nothing.
