# HA UniFi VPN Watchdog

A group is a set of VLANs and/or devices plus an ordered list of your UniFi WireGuard VPN clients. The watchdog switches on the next client
from your fallback order when the one in use stops carrying traffic, can rotate clients on a timer, can keep warm standbys connected, and
(only if you switch it on) keeps its own routing policy for each picked VLAN or device pointed at the active client. VPN clients can have
any names: they are shown exactly as you named them in UniFi and are never interpreted.

## First start
1. **Configuration tab:** set `unifi_api_key` (UniFi > Settings > Integrations > API key) and, if needed, `unifi_url`.
2. Start the add-on. It creates `config.yaml` in the add-on config folder.
3. Open **VPN Watchdog** in the sidebar, go to **Settings > VPN groups** and add a group:
   - tick the VLANs this group is for, and/or add devices (a TV, a camera: a device is picked by its MAC, so use a fixed-address device),
   - add your VPN clients under **Fallback order** and number them 1, 2, 3 ... Until you do, the group only watches,
   - optional: **Warm standbys** (keep the next clients connected so a failover is instant), **Rotation** (move to another client on a
     timer), and **Routing** (the watchdog keeps its own policy per picked VLAN or device on the active client).
4. The **Status** page shows the group's VLANs with their devices, the path through the active VPN client to the exit address, and the
   fallback order. Click a VLAN to open its devices; click a device or a client for details. Settings saves by itself a moment after each change.
5. A VPN client can be in more than one group. A client that another group still uses is never switched off, and a device belongs to one group.

## Home Assistant integration
- **Sidebar panel** (ingress): no extra login, controls are protected by your HA session. On a phone the page switches to an app-like layout.
- **Entities** (MQTT discovery). With the Mosquitto add-on nothing needs configuring. With an external broker (the one HA's MQTT
  integration uses) fill `mqtt_host`, `mqtt_port`, `mqtt_username`, `mqtt_password` in the Configuration tab. For the Mosquitto add-on use host
  `core-mosquitto`, port `1883` and a Home Assistant user (create a dedicated one under Settings → People → Users). Entities: active tunnel, fallback step, healthy, last decision,
  "Failover paused" switch and "Force tunnel" select, grouped under a "VPN <group>" device.
- **Notifications:** panel → *Config* tab → *Notifications*: choose from the notify services Home Assistant reports (phones,
  persistent notification, ...), press *Use this*, then *Send test*. The default is a persistent notification.
- **Logs:** the add-on **Log** tab.

## Before you rely on it
The watchdog switches your VPN clients on and off. Unless you switch on **Routing** for a group (Settings > VPN groups > Routing, off by default), it never
touches a routing policy: UniFi's own policies send the VLANs and devices through whichever client is up, so keep a routing policy **switched on** in
UniFi for every VPN client you put in a group. A client whose policy is off would be switched on but carry nothing. The Status page warns when a group's
VLANs do not go through its active client.

With **Routing** on you do not need that: the watchdog keeps one policy of its own per picked VLAN, and one for the group's devices, pointed at the
active client. Those policies are named `vpnwd: <group> › <VLAN or devices>` followed by a note that the add-on made them, so you can recognise them in
UniFi. It never edits or deletes a policy you made. UniFi applies the first live policy in its list, so a policy of yours that covers the same VLAN and
sits above the watchdog's one still wins. The Status page warns about it and offers to switch that policy off, only after you confirm.

## Not available yet
Failover on speed or delay (for example: when the download is below a number you choose) is planned. UniFi cannot speed-test a single VPN client, so it
will need a small dedicated test device. Today a group fails over when its client stops carrying traffic.
