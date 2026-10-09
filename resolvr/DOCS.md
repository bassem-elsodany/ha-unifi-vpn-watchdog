# Resolvr

Resolvr is a monitoring dashboard for [Technitium DNS Server](https://technitium.com/dns/): live query traffic, clients, query logs, cache, zones, DHCP, blocking and block-list updates. This add-on runs it inside Home Assistant and puts it in the sidebar.

It only needs the address of your Technitium server and an API token. It does not need Technitium to run as a Home Assistant add-on: any Technitium server your Home Assistant machine can reach will do.

## First start
1. In Technitium, create an API token: **Administration → Sessions → Create Token**. Use a name you will recognise, such as `resolvr`.
2. Open this add-on's **Configuration** tab and set:
   - `technitium_url`: the address of the server's web console, for example `http://192.168.1.10:5380`. An IP address is the most reliable choice: a name has to be looked up, and Home Assistant may use this very DNS server to do it.
   - `technitium_token`: the token from step 1.
3. **Start** the add-on and open **Resolvr** in the sidebar. There is no sign-in: Home Assistant has already signed you in.

To change the address or token later, edit the Configuration tab and restart the add-on. The Connection Settings page inside Resolvr then shows the address but cannot change it.

## Who can use it
The sidebar entry is shown to Home Assistant administrators only, and everyone who opens it has full access to Resolvr, including its admin actions (flushing the cache, updating block lists, blocking domains, editing feeds). There are no separate Resolvr users to manage, so the Users page is not shown. The add-on publishes no network port: it can only be reached through Home Assistant.

## What you will see
- **Overview** in four tabs: traffic, resolution, top lists and infrastructure.
- **Clients**: pick a device and see what it is asking for.
- **Query Logs**, **Cache**, **Zones**, **DHCP** and more.
- **Blocked Zones**: when your block lists were last updated, an Update now button, your feeds and domains, and a check that tells you which feed blocked a name.

The Resolution, Clients and Query Logs pages need one of Technitium's query-logging apps installed on the server (Apps → App Store → Query Logs). The pages tell you when it is missing.

## Your data
- Resolvr keeps a small database in the add-on's data folder. Home Assistant keeps it across restarts and updates and includes it in backups. It holds the Technitium address and token, so treat a backup like a password.
- A few small things (the open tab, block-list and cache history, which clients you have already seen) are remembered in your browser, not on the server.

## Troubleshooting
- **"Could not reach the server" or no data:** check `technitium_url` from the Home Assistant machine, and that the token is valid (create a new one if unsure). An `https://` address with a self-signed certificate is not accepted; use `http://` on your network or a trusted certificate.
- **Pages say a query-logging app is missing:** install one in Technitium (Apps → App Store).
- **Logs:** the add-on's **Log** tab.
