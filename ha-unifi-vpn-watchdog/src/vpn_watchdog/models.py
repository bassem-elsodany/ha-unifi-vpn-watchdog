"""Plain data objects shared across modules. No secrets are ever stored here."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Tunnel:
    id: str
    name: str
    enabled: bool
    iso: str | None = None
    city: str | None = None
    server_id: str | None = None
    ip: str | None = None
    subnet: str | None = None


def parse_tunnel(raw: dict[str, Any], pattern: re.Pattern[str], enabled: bool | None = None) -> Tunnel:
    """Build a Tunnel from a UniFi networkconf object, dropping every other field (keys!)."""
    name = raw.get("name", "")
    m = pattern.match(name)
    parts = m.groupdict() if m else {}
    iso = parts.get("iso")
    return Tunnel(
        id=raw["_id"],
        name=name,
        enabled=bool(raw.get("enabled")) if enabled is None else enabled,
        iso=iso.upper() if iso else None,
        city=(parts.get("city") or None) and parts["city"].upper(),
        server_id=parts.get("id"),
        ip=parts.get("ip"),
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


@dataclass
class Snapshot:
    tunnels: dict[str, Tunnel]
    connections: dict[str, Connection]
    routes: list[Route]
    networks: dict[str, str]
    wan_ip: str | None = None

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
