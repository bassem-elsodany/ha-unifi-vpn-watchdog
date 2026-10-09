# Resolvr

[![CI](https://github.com/bassem-elsodany/homelab-ha-addons/actions/workflows/ci.yml/badge.svg)](https://github.com/bassem-elsodany/homelab-ha-addons/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE)

A monitoring dashboard for [Technitium DNS Server](https://technitium.com/dns/) in a Home Assistant sidebar panel: live query traffic, clients, query logs, cache, zones, DHCP, and a block-list status page with an Update now button. It uses your Home Assistant sign-in, so there is no separate login.

![Resolvr overview](https://raw.githubusercontent.com/bassem-elsodany/resolvr-dns-dashboard/main/docs/screenshots/overview.png)

This add-on runs the published [Resolvr](https://github.com/bassem-elsodany/resolvr-dns-dashboard) image: the dashboard itself lives in its own repository, with screenshots and the full feature list. This folder holds only the Home Assistant packaging.

## Install
1. Add this repository to Home Assistant (see the [catalogue](../README.md)).
2. Install **Resolvr**, open its **Configuration** tab and set `technitium_url` and `technitium_token` (see the [add-on documentation](DOCS.md)).
3. Start it and open **Resolvr** in the sidebar.

## License

[MIT](../LICENSE)
