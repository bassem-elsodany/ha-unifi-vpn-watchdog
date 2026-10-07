# Homelab Home Assistant add-ons

A collection of Home Assistant add-ons for a homelab. Add this repository once and every add-on in it shows up in the Add-on Store.

## Install
1. Home Assistant (OS or Supervised): **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
2. Add `https://github.com/bassem-elsodany/homelab-ha-addons` and press **Add**.
3. Pick an add-on from the list below, install it, and follow its documentation.

## Add-ons

| Add-on | What it does |
|---|---|
| [**HA UniFi VPN Watchdog**](ha-unifi-vpn-watchdog/README.md) | Groups your UniFi VLANs and devices with an ordered list of WireGuard VPN clients, health-checks the one in use and fails over to the next when it stops carrying traffic. Can rotate clients on a timer, keep warm standbys, and (optionally) keep routing policies pointed at the active client. Graphic status map, settings and a phone layout in a Home Assistant sidebar panel. [Add-on docs](ha-unifi-vpn-watchdog/DOCS.md) |

More add-ons will be added as folders next to this one, each with its own README.

![HA UniFi VPN Watchdog status](ha-unifi-vpn-watchdog/docs/screenshots/status-dark.png)

## Layout

```
repository.yaml        what Home Assistant reads when you add this repository
<add-on>/              one folder per add-on (config.yaml, Dockerfile, DOCS.md, README.md, source)
```

The HA UniFi VPN Watchdog code also runs as a plain Docker container (see `ha-unifi-vpn-watchdog/docker/`).
