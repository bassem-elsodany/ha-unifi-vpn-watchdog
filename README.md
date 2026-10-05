# HA UniFi VPN Watchdog (Home Assistant add-on repository)

Health-checks the WireGuard VPN clients on a UniFi gateway and fails a policy route over to another server, city or
country when a tunnel stops carrying traffic. Status, controls and a config editor are in a Home Assistant sidebar panel.

## Install
1. Home Assistant (OS or Supervised): **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add `https://github.com/bassem-elsodany/ha-unifi-vpn-watchdog` and press **Add**.
3. Install **HA UniFi VPN Watchdog**, set `unifi_api_key` in its **Configuration** tab, start it.
4. Open **VPN Watchdog** in the sidebar. It starts in dry-run; press **Go live** when the decisions look right.

Documentation: [ha-unifi-vpn-watchdog/README.md](ha-unifi-vpn-watchdog/README.md) · [add-on docs](ha-unifi-vpn-watchdog/DOCS.md)

The same code also runs as a plain Docker container (see `ha-unifi-vpn-watchdog/docker/`).
