"""Plain data objects shared across modules. No secrets are ever stored here."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Tunnel:
    id: str
    name: str            # whatever the user called it; the watchdog never interprets it
    enabled: bool
    subnet: str | None = None


def parse_tunnel(raw: dict[str, Any], enabled: bool | None = None) -> Tunnel:
    """Build a Tunnel from a UniFi networkconf object, dropping every other field (keys!)."""
    return Tunnel(
        id=raw["_id"],
        name=raw.get("name", ""),
        enabled=bool(raw.get("enabled")) if enabled is None else enabled,
        subnet=raw.get("ip_subnet"),
    )


@dataclass
class Connection:
    network_id: str
    status: str
    remote_ip: str | None = None
    rx_bps: int | None = None
    tx_bps: int | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def connected(self) -> bool:
        return self.status == "CONNECTED"


@dataclass
class Route:
    id: str
    description: str
    network_id: str | None
    enabled: bool
    kill_switch: bool
    target_networks: frozenset[str]
    target_macs: frozenset[str]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)
    matching: str = "INTERNET"          # what the policy catches: all internet traffic, or only some domains/IPs/regions


@dataclass
class Snapshot:
    tunnels: dict[str, Tunnel]
    connections: dict[str, Connection]
    routes: list[Route]
    networks: dict[str, str]
    wan_ip: str | None = None
    network_info: dict[str, dict[str, Any]] = field(default_factory=dict)   # id -> {name, vlan, subnet, purpose}
    clients: dict[str, dict[str, Any]] = field(default_factory=dict)        # mac -> {name, ip, network}

    def tunnel_by_name(self, name: str) -> Tunnel | None:
        for t in self.tunnels.values():
            if t.name == name:
                return t
        return None


@dataclass
class ProbeResult:
    ok: bool
    reason: str = ""
    ip: str | None = None
    country: str | None = None
    leak: bool = False
    details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "reason": self.reason, "ip": self.ip, "country": self.country, "leak": self.leak}
