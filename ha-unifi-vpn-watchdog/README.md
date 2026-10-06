# HA UniFi VPN Watchdog

Health-checks the WireGuard VPN clients on a UniFi gateway and, when the one in use stops carrying traffic, **switches on the
next VPN client from your fallback order and switches the failed one off**. That is the only thing it ever changes in UniFi:
**a VPN client's on/off switch. Routing policies are yours: the watchdog never edits one you made. Only if you switch on "manage routing" for a group does it keep its own policy (named `vpnwd: ...`) per picked VLAN pointed at the active client.** Runs as a Docker container or a Home Assistant add-on, has a
web UI (status, jobs, start/stop, config editor), and is configured by one hot-reloaded YAML file.

```
            ┌────────────────────── ha-unifi-vpn-watchdog ──────────────────────┐
 UniFi API  │  snapshot ─► health ─► decision ─► enable + test next ─► alert      │
 (read)  ──►│  clients     status     down?      wait CONNECTED       HA/ntfy    │──► UniFi API: switch a VPN client on / off
            │  policies    blackhole  order      (old one off later)  MQTT       │
            └──────────────────────────────┬────────────────────────────────────┘
                                       web UI / API
```

## How failover works

1. **Detect.** Each cycle (default 15 s) it reads tunnel status, then checks three things, because a tunnel can say
   `CONNECTED` while passing nothing:
   status is `CONNECTED` · receive rate is not stuck at 0 while sending (black hole) · an **exit-IP probe** through
   the tunnel returns a working IP that is not your WAN IP (leak) and, only if you typed an expected country for the step, is in that country.
2. **Decide.** A tunnel is *down* after `failure_threshold` bad polls (or `probe_failure_threshold` bad probes).
3. **Pick.** Candidates come from the group's **fallback order**: a numbered list of tunnels you set yourself. #1 is used when
   it works; if the active tunnel fails the watchdog tries #1, #2, #3 ... skipping tunnels that failed recently. Tunnels can be
   called anything and are never interpreted, sorted or pattern-matched.
4. **Test before switching.** The candidate is switched on and must reach `CONNECTED`. A candidate that fails is quarantined with
   exponential backoff and the next one is tried.
5. **Commit.** The failed client is switched off at the end of the cycle. Nothing else changes: your routing policies in UniFi
   (one per VPN client, all kept switched on, in the priority order you want) send each VLAN or device through whichever client is up.
6. **Guard rails.** `min_hold_seconds` and `max_switches_per_hour` prevent flapping; hard failures bypass the hold,
   not the hourly cap. If nothing works it alerts once and leaves the current client as it is.
7. **Fail back.** When a tunnel higher in your list (a lower number) has tested healthy for `stable_seconds`, traffic moves back
   up to it. It never moves down or sideways while the active tunnel is healthy.
8. **One client switched on per group.** Only the client in use stays switched on; every other client of the group is switched off, so
   traffic never leaves through two exit IPs at once. (While it tests a higher client for failback that one is on briefly.) A VPN client
   that carries a device's own route (a device-targeted policy) is never switched off.

Safe start: until you set a fallback order the watchdog only watches the tunnel in use and never switches anything.

## Quick start (Docker)

```bash
cp .env.example .env                          # UNIFI_API_KEY, WATCHDOG_CONTROL_TOKEN
cp config/config.example.yaml config/config.yaml   # edit networks + ladder
docker compose -f docker/docker-compose.yml up -d
open http://<host>:8080                        # UI; enter the control token to edit/start/stop
```

Multi-arch for a Raspberry Pi: `docker buildx build --platform linux/arm64,linux/amd64 -f docker/Dockerfile -t ha-unifi-vpn-watchdog .`

Local checks, no container needed:

```bash
pip install -e '.[dev]'
ha-unifi-vpn-watchdog validate -c config/config.yaml --env-file .env     # config sanity
ha-unifi-vpn-watchdog discover -c config/config.yaml --env-file .env     # tunnels, routes, resolved ladders
ha-unifi-vpn-watchdog once     -c config/config.yaml --env-file .env     # run one cycle and print the status
pytest
```

## Home Assistant

The project folder **is** the add-on (`config.yaml`, `Dockerfile`, `DOCS.md`).

1. Copy the folder to `/addons/ha_unifi_vpn_watchdog` on the HA host (Samba or SSH add-on), then *Settings → Add-ons → Add-on
   Store → ⋮ → Check for updates* and install **HA UniFi VPN Watchdog** from *Local add-ons*. (Requires HA OS or Supervised;
   on a Container install run the Docker image next to HA and use the MQTT + REST pieces below.)
2. **Configuration** tab: `unifi_api_key`, optionally `notify_service` (e.g. `notify.mobile_app_myphone`).
3. Start. A `config.yaml` is created in the add-on config folder. Open **VPN Watchdog** in the sidebar and add a group under **Settings > VPN groups**.

| HA feature | How |
|---|---|
| Network map | sidebar panel → *Status*: per group, its VLANs with their devices, an animated path through the active VPN client to the internet, and the fallback order as numbered cards you **drag up or down** to reorder (drag a tunnel in from the unused list to add it, drop one on that list to remove it); click a client or device for details |
| Settings UI | sidebar panel → *Settings* tab (form: intervals, thresholds, fallback order, alerts); secrets via the add-on Configuration tab |
| Status / jobs / start-stop | sidebar panel (ingress, authenticated by your HA login) |
| Notifications | `home_assistant` notifier with `supervisor: true`, plus ntfy / Telegram / webhook |
| Entities | MQTT discovery: *Active tunnel, Fallback step, Last decision, Healthy, Failover paused (switch), Force tunnel (select)* per group |
| Logs | add-on *Log* tab |

## Probe modes (`probe.mode`)

| mode | what it does | use when |
|---|---|---|
| `none` | status + black-hole checks only | default |
| `direct` | probe from the watchdog's own egress | the watchdog itself is routed through the VPN |

The earlier `canary` and `remote` modes steered a test device through each tunnel by changing a routing policy. The watchdog no longer
writes any routing policy, so they were removed (a config that still asks for them is refused with an explanation).

Geo-IP databases disagree on VPN address ranges. Keep several endpoints; set
`probe.check_country: false` if you only want "works and is not a leak".

## Alerts

Each alert has an on/off switch, a title and a message with `{placeholders}` (`{group} {tunnel} {previous} {step}
{reason} {tried} ...`), editable in *Settings → Notifications* or under `alerts:` in the YAML:

| alert | sent when | default |
|---|---|---|
| `switch` | a tunnel failed and traffic moved to another tunnel | on |
| `failback` | the preferred tunnel recovered and traffic moved back | on |
| `rotation` | the rotation job moved the group to another tunnel on its schedule | on |
| `rotation_failed` | a rotation was due but no other tunnel passed the test, so nothing moved | on |
| `exhausted` | the active tunnel is down and every candidate failed (critical) | on |
| `leak` | the exit-IP test saw your real WAN address (critical) | on |
| `recovered` | a tunnel is healthy again after `exhausted` | on |
| `blocked` | a switch was needed but held back by the anti-flapping limits | on |
| `startup` | the watchdog (re)started | off |
| `config_error` | a saved configuration was rejected | on |

## Configuration

See [config/config.example.yaml](config/config.example.yaml); every key is documented there. Highlights:

- **Fallback order** (`groups[].order`): the sequence of tunnels, first = most preferred. Each entry is an exact tunnel name,
  or `{tunnel: NAME, expect_country: IT}` if the exit-IP test should check a country that you typed yourself. Tunnels not in the
  list are never used. Empty means the watchdog only watches. Set it in *Settings > Fallback order* (type a position number to move).
- **A group is an ordered list of VPN clients.** It has no VLANs: UniFi's routing policies decide which VLANs and devices use a client, and the
  watchdog never reads or edits them for failover. A VPN client can belong to one group only. For the failover to carry traffic, keep a routing
  policy switched on in UniFi for every client you want it to be able to use (UniFi sends traffic through the first matching policy whose client is up).
- **Renames are safe.** Groups store each tunnel of the fallback order by its UniFi id, with the name only as a label. Renaming a
  VPN client in UniFi changes nothing (the label in `config.yaml` follows). The Status page is drawn from UniFi's own state on every check
  (default every 15 s, or press *Check now*): which policy applies to which VLAN, and to which device, is read, never remembered.
- **Jobs.** Failover is every group's first job. A group can also have a **rotation** job (Settings > Jobs): every N hours, days or weeks
  (days and weeks at a time of day) the group moves to the next tunnel in its fallback order, or to a random one from the order. The
  new tunnel is connected and tested first; one that fails is skipped. Each job has its own switch; "Rotate now" and "Stop rotation"
  are on the Status page. Failover keeps working between rotations; failback to a higher tunnel is paused while a rotation job is on.
  ```yaml
  jobs:
    - {group: iot-and-vpn, kind: rotation, every: 1, unit: days, at: "03:00", go_to: next}   # unit: hours | days | weeks, go_to: next | random
  ```
- **Per-group overrides** for any `detection`, `switching`, `failback` key.
- **Hot reload**: edit the file (or use the UI), it is applied next cycle; an invalid file is rejected and the old
  config keeps running. Typos are errors (unknown keys are rejected). `${VAR}` / `${VAR:-default}` read the environment.

## API (same server as the UI)

Read without auth: `GET /healthz`, `/api/status`, `/metrics` (Prometheus). Control needs `Authorization: Bearer <control_token>`
(or HA ingress): `POST /api/groups/{name|*}/pause|resume`, `/api/groups/{name}/switch|test {"tunnel": "..."}`,
`/api/check-now`, `GET|POST /api/config`, `POST /api/config/validate`.
With no `control_token` set the server is read-only.

## Things learned the hard way (all handled in code)

- Disabling a VPN client does not delete or disable its routing policy: UniFi simply skips a policy whose client is off. That is why the watchdog
  only switches clients and leaves your own policies alone (its own `vpnwd:` policies are moved to the active client).
- UniFi rejects overlapping client subnets (`SubnetOverlapped`), so each tunnel needs a unique `10.5.x.2/24`.
- Servers showing 0 % load never connected in testing; pick servers with some load and let the quarantine skip duds.
- Use `traffic-flows` (UniFi) as independent proof of where traffic exits; the watchdog never trusts a single signal.

## Layout

```
src/vpn_watchdog/   config · unifi · probe · ladder · engine · state · notify · settings · server+ui · safe_mode · ha_mqtt · cli
tests/              56 tests (fake UniFi + fake probe drive the engine; HTTP mocked for the clients)
docker/             Dockerfile · docker-compose.yml
config/             config.example.yaml
config.yaml, Dockerfile, DOCS.md   Home Assistant add-on manifest/build/docs
```
