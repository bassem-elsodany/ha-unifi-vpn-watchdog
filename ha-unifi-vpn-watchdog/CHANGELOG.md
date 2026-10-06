# Changelog

## 0.18.1

- The add-on description (Home Assistant store) and the READMEs describe the current logic: groups of VLANs and VPN clients, failover, optional rotation and optional routing management.

## 0.18.0

- **Jobs are part of the group.** The Jobs section is gone. Settings > VPN groups now has, for each group, a Failover switch (pause / resume) and a Rotation switch with its timing, "Rotate now" and "Remove rotation". Existing rotation jobs in your config keep working.
- The page introduction and the row descriptions are rewritten for the current logic: a group is its VLANs plus an ordered list of VPN clients, with optional Rotation and Routing.
- For a group with picked VLANs the "Carrying now" line is gone (it could list VLANs that are not really routed through the active client); the warnings cover any mismatch.

## 0.17.0

- **Manage routing (optional, off by default, per group).** Settings > VPN groups > Routing. When on, the watchdog keeps ONE routing policy of its own per picked VLAN, named `vpnwd: <group> › <VLAN>`, pointed at the group's active VPN client, and moves it when a failover happens. Turning it on shows what will be written and asks first.
- **Hard limit:** it only ever creates, edits or deletes policies whose name starts with `vpnwd:`; the UniFi client refuses to change any other policy. It still never creates or deletes a network or VLAN. A failed write is recorded in Events and retried after five minutes, not every cycle.
- UniFi applies the first live policy in its list, so one of your own policies above the watchdog's still wins; the Status page warns when a picked VLAN is routed through a client outside the group.

## 0.16.0

- **You pick the VLANs of a group** (Settings > VPN groups > VLANs). A group is now its VLANs plus its ordered VPN clients, so the Status page always shows which VLANs belong to which group, even when every client is down. VLANs are followed by their UniFi id, so a rename in UniFi changes nothing. A VLAN belongs to one group.
- The watchdog still never writes to UniFi: it checks the routing policies against your choice and warns when a picked VLAN is not routed through any VPN client, is routed through a client outside the group, or when a client in the order has no policy for it.
- A group that picked no VLAN works as before (its VLANs are read from the policies).

## 0.15.3

- Settings save by themselves: a change is saved and applied about a second after you make it, and a small "Saved and applied" note shows it. The Save & apply and Discard buttons and the "unsaved changes" bar are gone. If a change is invalid, the note says why and nothing is saved until it is fixed.

## 0.15.2

- A group keeps its VLANs when none of its VPN clients is on: it also claims VLANs that a routing policy of one of its clients names, unless UniFi really sends them through another client.
- Warning on the Status page and in Settings > VPN groups when a client in the order has no routing policy for one of the group's VLANs (a failover to it would leave that VLAN on the normal internet).
- Settings > VPN groups: the "Timed rotation" row is gone; rotation is set up under Jobs only.

## 0.15.1

- Status: a long fallback order no longer fills the page. A group with more than five clients in its order shows the first three (and the active one) and one dashed line, "+ N more in the fallback order · Show all"; pressing it shows every client and reads "Show fewer". Short orders show in full as before.

## 0.15.0

- **The watchdog now only switches VPN clients on and off.** It never creates, edits, enables or disables a routing policy, and the API client no longer has any way to write one. Which VLANs and devices use which VPN client is entirely your routing policies in UniFi.
- **A group is an ordered list of VPN clients, nothing else.** The VLAN picker and the "If the client dies" setting are gone. Which VLANs a client carries is read from UniFi and shown on the Status page and in the group ("Carrying now, read from UniFi").
- **How it works now:** one client of the group is switched on. When it stops working the watchdog switches on the first working client of the order (connected and tested) and switches the failed one off at the end of the cycle. Failback and rotation do the same. With none of the group's clients on, it switches on the first working one. A VPN client carrying a device's own route is never switched off. A VPN client can belong to one group only (saving refuses a client in two groups; an old config with one is read, the first group keeps it).
- Status: a routing policy counts as applied only when its VPN client is switched on (UniFi skips a policy whose client is off). A group is drawn around the client it has switched on, and says so when no routing policy sends traffic through it.
- Removed: probe modes `canary` and `remote` and the probe agent (they steered a test device through each tunnel by changing a routing policy), `switching.on_exhausted: kill_switch` and the group `kill_switch` / `networks` settings (older configs that mention the latter two still load; they are ignored and cleaned up on the next save).
- **Before you rely on it:** keep a routing policy switched on in UniFi for every VPN client you want a group to be able to use. A client whose policy is off would be switched on but carry nothing.

## 0.14.2

- The Status page can no longer change the fallback order: no dragging of VPN clients, no grip handles, no Alt+arrow keys, and no Move up / Move down / Add / Remove buttons in the details panel (Switch and Test stay). The order is built in Settings > VPN groups only. The reorder API call is gone too.

## 0.14.1

- Building a group's fallback order never sends you to another page: it is all in Settings > VPN groups. The old "Reorder on the Status page" button is gone for good, and the empty lane on Status now points to the Settings editor.

## 0.14.0

- **VPN groups explained and easier to fill in** (Settings > VPN groups). The page now says in plain words what a group is, lists the three steps, and marks every section as required, optional or done. Each group shows whether it is Ready or Needs a fallback order, and says what is missing in one line.
- **The fallback order is edited right there**: numbered rows with a drag handle, up / down arrows and a remove button, "Add a VPN client" with a list of the clients not yet in the order, and "Add all". Before, the order could only be built by dragging on the Status page. A VLAN that belongs to another group is greyed out with that group's name.
- With no group yet, the page explains why you need one and offers "Create your first VPN group".
- Status: a group without a fallback order is labelled NEEDS A FALLBACK ORDER (instead of WATCHING ONLY) and has a "Set the fallback order" button that opens its settings.

## 0.13.1

- **Groups on different VLANs now work independently.** A group owns the routing policies that target exactly its own VLANs. Before, it used every policy that included its VLANs, so a group for VLAN 20 and a group for VLAN 50 shared (and disturbed) the same policies whenever a policy listed both. Policies that also cover other VLANs are left alone. A missing one is created for exactly the group's VLANs, named "<tunnel> (<group>)".
- A VLAN can belong to one group only (the config is refused otherwise).
- If another enabled policy that is not the group's covers the group's VLANs and sits above its policy in UniFi's list, UniFi applies that one instead. The group header now says so, naming the policy and its position. The watchdog never changes such a policy.

## 0.13.0

- **Everything that points at UniFi now points at its id, not only its name.** A group's VLANs and the tunnels in its fallback order are stored with their UniFi id (`id`) and a name label. Renaming a VPN client or a VLAN in UniFi changes nothing: the order, the position numbers, the country typed for a position and the group's VLANs all stay, and the label in `config.yaml` follows the new name. A config that only has names (every config before this version) gets its ids filled in automatically on the first UniFi reading, and is rewritten once.
- A VPN client or VLAN that was deleted and recreated (new id, same name) is found again by its name; one that was deleted for good stays in the config as "missing" and is skipped, never guessed.
- Devices are placed on their VLAN by the VLAN's id (UniFi's `network_id`), not by the VLAN's name.
- Status follows UniFi even for managed groups: a VLAN of a group that UniFi routes through another client right now is drawn where it really goes, not under the group's active tunnel.
- A routing policy that targets all devices is shown as covering every VLAN.
- The Status page and the Settings form talk to the watchdog by id (switching, testing, reordering, ticking VLANs).

## 0.12.2

- Fixed: Status put every VLAN that a VPN policy lists under the same VPN client. When VLAN 20 and VLAN 50 are routed to different clients (policies narrowed to one VLAN each), both showed on whichever client was first in UniFi's list, because the other, switched-off policies still listed both VLANs. Now each VLAN is drawn under the policy UniFi actually applies to it (the first enabled one in its list); VLANs on the same client share a block, VLANs on different clients get a block each.

## 0.12.1

- The app version is shown as a small tag next to the app name at the top left (it is the add-on version, so it matches what Home Assistant lists).

## 0.12.0

- New: **Jobs** (Settings > Jobs). Failover is every group's first job. A group can now also have a **rotation** job: every N hours, days or weeks
  (days and weeks at a time of day) it moves the group to the next tunnel in the fallback order, or to a random one from the order.
  The new tunnel is connected and tested first, a tunnel that fails is skipped, and if none passes nothing moves and you get an alert.
  Each job has its own switch. A group can have one job of each kind.
- Status: a second line in the group header shows the rotation (schedule, time left, the tunnel it goes to next, when it last ran) with
  Rotate now and Stop rotation. Stopping rotation does not stop failover, and the other way round.
- Failback to a higher tunnel is paused for a group while its rotation job is on, because rotation decides when to move.
- Two new alerts: `rotation` and `rotation_failed` (Settings > Notifications).

## 0.11.2

- Removed the Advanced (YAML) tab. Everything is in Settings. If a saved configuration is ever rejected, the YAML editor still opens by itself so it can be repaired.

## 0.11.2

- Removed the Advanced (YAML) tab. Everything is in Settings. If a saved configuration is ever rejected, the YAML editor still opens by itself so it can be repaired.

## 0.11.1

- With the log level on DEBUG, every check cycle now also appears in the Events tab (what was read from UniFi and each group's verdict) and in the add-on log. Before, DEBUG only changed the add-on log, and a healthy check logged nothing. The Events tab otherwise still lists only real events (switch, failback, alerts). Changing the log level needs an add-on restart.

## 0.11.0

- Status: every device with its own route now shows which policy applies (its position in the UniFi list, and its name).
- Status: a device policy that sits below a policy covering the whole VLAN is shown under "Not applied" as overridden, with the policy that really applies. It gets no line on the map and a small ⚠ in the VLAN list.
- UniFi policies are read top-down and the first enabled one that catches all of a device's traffic wins. Policies that only catch some domains or addresses are ignored for this. Before, the last matching policy was used.

## 0.10.1

- Status: the line from every VLAN box is now a dashed moving line, like the others (before, only the last stretch moved and the short stubs were solid).
- Status: the legend sits at the bottom right of the page, no longer between the two Internet boxes.

## 0.10.0
- **Devices with their own route** (approved design "Status page with device routes"). A routing policy that targets individual devices (not a whole network) is now shown on the Status page: a small card "Devices with their own route" lists each device, its VLAN and where it goes, and draws a line from there to the normal connection (amber) or to the VPN client that carries it (violet). The device stays in its VLAN list, marked with an arrow. The tunnel that carries it shows "carries N devices with its own route" even when it is not in a fallback order. The watchdog still never changes these policies.

## 0.9.6
- When you expand "No VPN policy" each VLAN is its own box again, and **every VLAN box has its own connector** into the "normal connection" Internet node (a short line from the box to a shared line, then the arrow). Expanding no longer repeats the summary rows.
- The arrow into the "normal connection" node is now dashed and moving, like the arrows into the active tunnel and the VPN Internet node.

## 0.9.5
- Expanding "No VPN policy" now turns that one box into a single list: each VLAN is a section with its devices. Before, the three summary rows stayed and every VLAN was repeated underneath as its own box (boxes inside boxes). Long device lists scroll inside their section.
- Devices without a name show "unnamed" instead of repeating their IP address twice.

## 0.9.4
- The Status map decides what is a VLAN from UniFi's own network type (LAN/VLAN and guest networks only). The name checks it used before ("Internet...", "One-Click VPN") are gone; WAN, VPN-client and remote-user networks are excluded by type, never by what they are called.

## 0.9.3
- The app now has an icon and a logo in Home Assistant (`icon.png`, `logo.png`); before, the Apps list showed a blank placeholder. A test checks that both files ship.

## 0.9.2
- **The Status map no longer depends on watchdog groups.** It draws what UniFi does: the VLANs that VPN routing policies carry (with their devices), the VPN client that is switched on, the Internet exit, and the VLANs that go straight to the internet. A watchdog group only adds the fallback order, the "if #1 fails" steps and the Stop/Start failover button on top. With no group (a fresh install) you get the same page, without any "create a group" panel.
- Policies that cover overlapping networks are shown as one carried set; the other tunnels with a policy for it are listed under "+ N more tunnels have a policy for these networks".
- Connected tunnel rates read "93 kbps ↓" instead of "idle ↓".

## 0.9.1
- **Status page rebuilt to the approved design (Option A), pixel for pixel**: the same colours, 48 px page margins, 300 / flexible / 240 px columns, 104 px active tunnel card and Internet node, 76 px tunnel cards with 48 px "if #N fails" gaps, section labels directly above their cards, the dashed "straight to the internet" line with its own Internet node, the pill for tunnels outside the order, and VLAN subtitles with the network address (10.0.20.0/24). It fills wide windows and stacks on narrow ones.
- Fixed a regression from 0.9.0: the Settings field style reused the rank badge's class name, so the "#1 / #2" badges turned into big input-like boxes on the Status page. A test now fails if any CSS class is defined twice.
- No group configured yet (fresh install or an emptied config): the Status page shows your VLANs, the direct internet path and a "Create your first VPN group" panel with a button, instead of a bare "No VPN policy" box.
- Removed text that was not in the design or interpreted tunnel names ("expects XX", the country next to the exit IP, "next check of a higher tunnel in Ns"). "Resume all" only appears while something is paused.

## 0.9.0
- **Settings redesigned** (approved design): a section list on the left (Detection, Switching, VPN groups, Notifications) and one flat page per section,
  with no boxes inside boxes. Rows put the label and help on the left and the control on the right, with units inside the fields and the plain-sentence
  explanation ("declared down after about 45 seconds") underneath. Segmented controls replace dropdowns for short choices.
- **VPN groups** is a list plus detail: networks are toggle chips, "If the tunnel dies" is a three-way selector, and the fallback order is a one-line preview
  with "Reorder on the Status page" (and "Add all N" for first-time setup). Group names are edited in place.
- **Notifications**: "Send alerts to" with a test button, then one compact row per alert (switch, level, when it fires). "Edit wording" opens the title and message
  inline with click-to-insert placeholders and a live preview.
- The save bar lists what changed ("Check every: 30 → 20 seconds"), and a dot marks the sections that have unsaved edits.
- Reordering on the Status page no longer discards unsaved Settings edits.

## 0.8.1
- Status page brought back in line with the approved design: IBM Plex Sans/Mono, the column headings (VLANs and their devices / Internet via the VPN),
  "GROUP <name> · FALLBACK ORDER", the active card reading "#1 ACTIVE · N devices behind it", the group strip "Active: ... · exit-IP test passed Ns ago",
  the legend and hint, one compact "No VPN policy" card (expandable) with the "straight to the internet, no VPN" path, and the Internet node level with
  the active card so its arrow is straight. The bypassing device stays visible at the end of a long device list.

## 0.8.0
- **New Status page: the network map.** Each VPN group is a row: its VLANs on the left, every device with its IP and live rate (search box, "show all N"),
  an animated path through the active VPN client to the internet, and the fallback order in the middle as numbered cards. Devices that bypass the VPN
  are marked and drawn with their own path when selected. VLANs with no VPN policy sit in a "No VPN policy" row with a dashed path to the normal connection.
- **Drag to reorder the fallback order.** Drag a client up or down in the lane, drag a tunnel from the "not in the order" list up into the lane to add it,
  or drop a client on that list to remove it. Alt + Arrow Up/Down moves the focused client from the keyboard; the details panel has Move up/down buttons
  (works on touch screens too). The order is saved with a validated write (`POST /api/groups/{name}/order`).
- Click a client or a device for a details panel (status, traffic, VPN server, policy, position, last failure; actions to switch, test, move, add, remove).
- Loading spinners: while UniFi is being read for the first time, in the header while a refresh is in progress, in Settings while they load, and while an order is saved.
- Status API: `map` replaces the per-VLAN list; `loading` says whether UniFi is being read.

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
