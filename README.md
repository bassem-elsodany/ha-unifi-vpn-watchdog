# HA UniFi VPN Watchdog (Home Assistant add-on repository)

Groups your VLANs with an ordered list of UniFi WireGuard VPN clients, health-checks the one in use, and fails over to the next
client when it stops carrying traffic. It can rotate clients on a timer and, if you switch it on, keep the routing policies of your
VLANs pointed at the active client. Status, settings and a config editor are in a Home Assistant sidebar panel.

## Install
1. Home Assistant (OS or Supervised): **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add `https://github.com/bassem-elsodany/ha-unifi-vpn-watchdog` and press **Add**.
3. Install **HA UniFi VPN Watchdog**, set `unifi_api_key` in its **Configuration** tab, start it.
4. Open **VPN Watchdog** in the sidebar, then create a group: pick its VLANs and build its fallback order.

Documentation: [ha-unifi-vpn-watchdog/README.md](ha-unifi-vpn-watchdog/README.md) · [add-on docs](ha-unifi-vpn-watchdog/DOCS.md)

The same code also runs as a plain Docker container (see `ha-unifi-vpn-watchdog/docker/`).
