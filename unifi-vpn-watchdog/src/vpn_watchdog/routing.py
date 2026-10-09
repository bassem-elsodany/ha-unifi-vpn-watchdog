"""Routing that the watchdog manages for a group, only when you switch it on for that group.

For each VLAN a group has picked, the watchdog keeps ONE routing policy of its own in UniFi, named `vpnwd: <group> › <VLAN>`, that
sends the VLAN through the group's active VPN client; for the devices a group has picked it keeps ONE policy named
`vpnwd: <group> › devices`. A failover only moves those policies to the new client. The watchdog never creates, edits or deletes
any other policy: a policy is its own only if its name starts with PREFIX (the UniFi client refuses every other write).

UniFi uses the first live policy in its list that covers a device, and a new policy always lands at the end. So a device policy would
lose against a VLAN policy created earlier: `order_fix` finds the watchdog's VLAN policies that sit above one of its device policies,
so that they can be created again (which puts them at the end) before the old ones are deleted."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Route, Snapshot, Tunnel

PREFIX = "vpnwd:"


@dataclass
class Action:
    kind: str                 # "create" | "update" | "delete"
    route_id: str | None
    network_id: str | None    # the VLAN (None for a device policy or a delete of a stale policy)
    tunnel_id: str | None
    description: str
    text: str                 # what it does, in words
    macs: tuple[str, ...] = field(default_factory=tuple)      # the devices of a device policy


NOTE = " — made by the VPN Watchdog add-on (group {group}); it moves this policy to the group's working VPN client. Do not edit."


def description(group: str, vlan: str) -> str:
    return f"{PREFIX} {group} › {vlan}" + NOTE.format(group=group)


def device_description(group: str) -> str:
    return description(group, "devices")


def key(text: str) -> str:
    """The part of a policy's name that identifies it (the explanation after the dash is for people)."""
    return text.split(" — ", 1)[0]


def owned(r: Route) -> bool:
    """One of the watchdog's own policies (a VLAN policy or a device policy)."""
    return r.description.startswith(PREFIX)


def owned_vlan(r: Route) -> bool:
    return owned(r) and not r.target_macs


def owned_dev(r: Route) -> bool:
    return owned(r) and bool(r.target_macs)


def plan(group: str, vlans: list[str], devices: list[str], tunnel: Tunnel | None, snap: Snapshot) -> list[Action]:
    """What has to change in UniFi so that every picked VLAN and device goes through `tunnel`. Nothing when there is no active client."""
    mine = [r for r in snap.routes if owned_vlan(r) and r.description.startswith(f"{PREFIX} {group} ›")]
    out: list[Action] = []
    if tunnel is None:
        return out
    seen: set[str] = set()
    for n in vlans:
        name = snap.networks.get(n, n)
        desc = description(group, name)
        matches = [x for x in mine if x.target_networks == frozenset({n}) and x.id not in seen]
        r = matches[-1] if matches else None                  # the newest copy stays (it is the one below the device policies); older copies go
        if r is None:
            out.append(Action("create", None, n, tunnel.id, desc, f"create the policy \"{desc}\": {name} goes through {tunnel.name}"))
            continue
        seen.add(r.id)
        if r.network_id != tunnel.id or not r.enabled or r.description != desc:
            out.append(Action("update", r.id, n, tunnel.id, desc, f"move the policy \"{r.description}\" to {tunnel.name}" + ("" if r.enabled else " and switch it on")))
    for r in mine:
        if r.id not in seen and not any(a.route_id == r.id for a in out):
            out.append(Action("delete", r.id, None, None, r.description, f"delete the policy \"{r.description}\" (it is replaced by a newer copy, or its VLAN is no longer picked)"))
    # the devices of the group: one policy for all of them
    ddesc = device_description(group)
    dev = next((r for r in snap.routes if owned_dev(r) and key(r.description) == key(ddesc)), None)
    macs = tuple(sorted(devices))
    if macs and dev is None:
        out.append(Action("create", None, None, tunnel.id, ddesc, f"create the policy \"{ddesc}\": {len(macs)} device{'s' if len(macs) != 1 else ''} go through {tunnel.name}", macs))
    elif macs and (dev.network_id != tunnel.id or not dev.enabled or set(dev.target_macs) != set(macs) or dev.description != ddesc):
        out.append(Action("update", dev.id, None, tunnel.id, ddesc, f"move the policy \"{ddesc}\" to {tunnel.name}" + ("" if set(dev.target_macs) == set(macs) else " and update its devices"), macs))
    elif not macs and dev is not None:
        out.append(Action("delete", dev.id, None, None, ddesc, f"delete the policy \"{ddesc}\" (the group has no devices any more)"))
    return out


def device_target(macs) -> list[dict]:
    return [{"client_mac": m, "type": "CLIENT"} for m in sorted(macs)]


def new_route_body(a: Action, kill_switch: bool = False) -> dict:
    targets = device_target(a.macs) if a.macs else [{"network_id": a.network_id, "type": "NETWORK"}]
    return {"description": a.description, "enabled": True, "matching_target": "INTERNET", "network_id": a.tunnel_id,
            "kill_switch_enabled": kill_switch, "next_hop": "", "domains": [], "ip_addresses": [], "ip_ranges": [], "regions": [],
            "target_devices": targets}


def order_fix(snap: Snapshot) -> list[Route]:
    """The watchdog's VLAN policies that sit above one of its device policies (the device policy would lose). Create them again, then delete these."""
    dev_at = [i for i, r in enumerate(snap.routes) if owned_dev(r)]
    if not dev_at:
        return []
    last = max(dev_at)
    return [r for i, r in enumerate(snap.routes) if owned_vlan(r) and i < last]


def clone_body(r: Route) -> dict:
    """The same policy as a new one (UniFi gives it a new id and puts it at the end of the list)."""
    return {k: v for k, v in r.raw.items() if k != "_id"}


def blockers(vlans: list[str], devices: list[str], tunnel: Tunnel | None, snap: Snapshot) -> list[dict]:
    """Policies of yours that UniFi reads before the watchdog's own policy and that take a picked VLAN or device somewhere else.

    A blocker is switched on, catches all internet traffic, is not the watchdog's own, covers a picked VLAN (or a picked device), sits above the
    watchdog's policy for it (or there is none yet) and does not already go to the active client. A policy for one single device does not
    block a VLAN."""
    lans = {nid for nid, i in snap.network_info.items() if i.get("purpose") in ("corporate", "guest")}

    def covers(r: Route, n: str) -> bool:
        return n in r.target_networks or (r.all_clients and n in lans)

    def live(r: Route) -> bool:
        t = snap.tunnels.get(r.network_id or "")
        return r.enabled and r.matching == "INTERNET" and (t is None or t.enabled)

    found: dict[str, dict] = {}

    def entry(i: int, r: Route) -> dict:
        return found.setdefault(r.id, {"id": r.id, "description": r.description, "position": i + 1,
                                       "goes_to": snap.networks.get(r.network_id or "", "the normal internet connection"), "vlans": [], "others": []})

    for n in vlans:
        mine = next((i for i, r in enumerate(snap.routes) if owned_vlan(r) and r.target_networks == frozenset({n})), len(snap.routes))
        for i, r in enumerate(snap.routes[:mine]):
            if owned(r) or r.target_macs or not live(r) or not covers(r, n) or (tunnel is not None and r.network_id == tunnel.id):
                continue
            b = entry(i, r)
            if snap.networks.get(n, n) not in b["vlans"]:
                b["vlans"].append(snap.networks.get(n, n))
    for mac in devices:
        c = snap.clients.get(mac) or snap.known.get(mac) or {}
        label = c.get("name") or mac
        dnet = snap.client_network(c) if c else None
        mine = next((i for i, r in enumerate(snap.routes) if owned_dev(r) and mac in r.target_macs), len(snap.routes))
        for i, r in enumerate(snap.routes[:mine]):
            if owned(r) or not live(r) or (tunnel is not None and r.network_id == tunnel.id):
                continue
            applies = mac in r.target_macs or (not r.target_macs and (r.all_clients or (dnet is not None and dnet in r.target_networks)))
            if not applies:
                continue
            b = entry(i, r)
            if label not in b["vlans"]:
                b["vlans"].append(label)
    for r in snap.routes:
        b = found.get(r.id)
        if b is not None:
            covered = lans if r.all_clients else (r.target_networks & lans)
            b["others"] = sorted(snap.networks.get(n, n) for n in covered if snap.networks.get(n, n) not in b["vlans"])
    return list(found.values())


def orphans(snap: Snapshot, groups: set[str]) -> list[Route]:
    """The watchdog's own policies (named `vpnwd: <group> › ...`) whose group no longer exists."""
    out = []
    for r in snap.routes:
        m = re.match(rf"^{re.escape(PREFIX)} (.+?) › ", r.description)
        if m and m.group(1) not in groups:
            out.append(r)
    return out


def has_twin_below(snap: Snapshot, r: Route) -> bool:
    """Is there already a copy of this policy below the last device policy (a clone whose old copy was not deleted yet)?"""
    at = [i for i, x in enumerate(snap.routes) if owned_dev(x)]
    last = max(at) if at else -1
    return any(x.id != r.id and key(x.description) == key(r.description) and i > last for i, x in enumerate(snap.routes))
