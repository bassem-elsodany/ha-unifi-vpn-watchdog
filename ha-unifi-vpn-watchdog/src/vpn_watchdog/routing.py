"""Routing that the watchdog manages for a group, only when you switch it on for that group.

For each VLAN a group has picked, the watchdog keeps ONE routing policy of its own in UniFi, named `vpnwd: <group> › <VLAN>`, that
sends the VLAN through the group's active VPN client. A failover only moves that policy to the new client. The watchdog never
creates, edits or deletes any other policy: a policy is its own only if its name starts with PREFIX (the UniFi client refuses
every other write)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Route, Snapshot, Tunnel

PREFIX = "vpnwd:"


@dataclass
class Action:
    kind: str                 # "create" | "update" | "delete"
    route_id: str | None
    network_id: str | None    # the VLAN (None for a delete of a stale policy)
    tunnel_id: str | None
    description: str
    text: str                 # what it does, in words


def description(group: str, vlan: str) -> str:
    return f"{PREFIX} {group} › {vlan}"


def owned(r: Route) -> bool:
    return r.description.startswith(PREFIX) and not r.target_macs


def plan(group: str, vlans: list[str], tunnel: Tunnel | None, snap: Snapshot) -> list[Action]:
    """What has to change in UniFi so that every picked VLAN goes through `tunnel`. Nothing when there is no active client."""
    mine = [r for r in snap.routes if owned(r) and r.description.startswith(f"{PREFIX} {group} ›")]
    out: list[Action] = []
    if tunnel is None:
        return out
    seen: set[str] = set()
    for n in vlans:
        name = snap.networks.get(n, n)
        desc = description(group, name)
        r = next((x for x in mine if x.target_networks == frozenset({n}) and x.id not in seen), None)
        if r is None:
            out.append(Action("create", None, n, tunnel.id, desc, f"create the policy \"{desc}\": {name} goes through {tunnel.name}"))
            continue
        seen.add(r.id)
        if r.network_id != tunnel.id or not r.enabled:
            out.append(Action("update", r.id, n, tunnel.id, desc, f"move the policy \"{r.description}\" to {tunnel.name}" + ("" if r.enabled else " and switch it on")))
    for r in mine:
        if r.id not in seen and not any(a.route_id == r.id for a in out):
            out.append(Action("delete", r.id, None, None, r.description, f"delete the policy \"{r.description}\" (its VLAN is no longer picked)"))
    return out


def new_route_body(a: Action, kill_switch: bool = False) -> dict:
    return {"description": a.description, "enabled": True, "matching_target": "INTERNET", "network_id": a.tunnel_id,
            "kill_switch_enabled": kill_switch, "next_hop": "", "domains": [], "ip_addresses": [], "ip_ranges": [], "regions": [],
            "target_devices": [{"network_id": a.network_id, "type": "NETWORK"}]}


def blockers(vlans: list[str], tunnel: Tunnel | None, snap: Snapshot) -> list[dict]:
    """Policies of yours that UniFi reads before the watchdog's own policy and that take a picked VLAN somewhere else.

    A blocker is switched on, catches all internet traffic, is not the watchdog's own, is not for a single device, covers a picked
    VLAN, sits above the watchdog's policy for that VLAN (or there is none yet) and does not already go to the active client."""
    lans = {nid for nid, i in snap.network_info.items() if i.get("purpose") in ("corporate", "guest")}

    def covers(r: Route, n: str) -> bool:
        return n in r.target_networks or (r.all_clients and n in lans)

    def live(r: Route) -> bool:
        t = snap.tunnels.get(r.network_id or "")
        return r.enabled and r.matching == "INTERNET" and (t is None or t.enabled)

    found: dict[str, dict] = {}
    for n in vlans:
        mine = next((i for i, r in enumerate(snap.routes) if owned(r) and r.target_networks == frozenset({n})), len(snap.routes))
        for i, r in enumerate(snap.routes[:mine]):
            if owned(r) or r.target_macs or not live(r) or not covers(r, n) or (tunnel is not None and r.network_id == tunnel.id):
                continue
            b = found.setdefault(r.id, {"id": r.id, "description": r.description, "position": i + 1,
                                        "goes_to": snap.networks.get(r.network_id or "", "the normal internet connection"), "vlans": [], "others": []})
            if snap.networks.get(n, n) not in b["vlans"]:
                b["vlans"].append(snap.networks.get(n, n))
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
