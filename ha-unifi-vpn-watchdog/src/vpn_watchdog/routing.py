"""Routing that the watchdog manages for a group, only when you switch it on for that group.

For each VLAN a group has picked, the watchdog keeps ONE routing policy of its own in UniFi, named `vpnwd: <group> › <VLAN>`, that
sends the VLAN through the group's active VPN client. A failover only moves that policy to the new client. The watchdog never
creates, edits or deletes any other policy: a policy is its own only if its name starts with PREFIX (the UniFi client refuses
every other write)."""
from __future__ import annotations

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
