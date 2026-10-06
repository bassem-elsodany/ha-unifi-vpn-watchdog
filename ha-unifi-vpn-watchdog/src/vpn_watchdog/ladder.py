"""The fallback order: an explicit sequence of tunnels chosen by the user. #1 is the most preferred.

Nothing is derived from tunnel names and there is no pattern matching or sorting: position in the list is the priority.
"""
from __future__ import annotations

from .config import GroupCfg
from .models import Tunnel


def resolve_order(group: GroupCfg, tunnels: list[Tunnel]) -> list[Tunnel]:
    """The configured sequence as Tunnel objects. Names that do not exist in UniFi (yet) are skipped."""
    by_name = {t.name: t for t in tunnels}
    return [by_name[i.tunnel] for i in group.order if i.tunnel in by_name]


def missing(group: GroupCfg, tunnels: list[Tunnel]) -> list[str]:
    names = {t.name for t in tunnels}
    return [i.tunnel for i in group.order if i.tunnel not in names]


def position_of(group: GroupCfg, tunnel: Tunnel | None) -> int | None:
    """1-based position of `tunnel` in the configured order, or None if it is not listed."""
    if tunnel is None:
        return None
    for i, item in enumerate(group.order, start=1):
        if item.tunnel == tunnel.name:
            return i
    return None


def position_label(group: GroupCfg, tunnel: Tunnel | None) -> str:
    n = position_of(group, tunnel)
    return f"#{n}" if n else ""


def expected_country(group: GroupCfg, tunnel: Tunnel) -> str | None:
    """Country the exit-IP test must see, only if the user typed one for this position."""
    for item in group.order:
        if item.tunnel == tunnel.name:
            return item.expect_country
    return None


def candidates(group: GroupCfg, tunnels: list[Tunnel], current: Tunnel | None) -> list[Tunnel]:
    """Failover order: the configured sequence from #1 down, never the current tunnel."""
    return [t for t in resolve_order(group, tunnels) if not (current and t.id == current.id)]


def failback_targets(group: GroupCfg, tunnels: list[Tunnel], current: Tunnel) -> list[Tunnel]:
    """Tunnels it may move back to: only those listed ABOVE the current one (lower number = more preferred)."""
    pos = position_of(group, current)
    if not pos:
        return []
    return [t for t in resolve_order(group, tunnels) if (position_of(group, t) or 0) < pos]
