"""Turns a group's declarative ladder into an ordered list of concrete tunnels."""
from __future__ import annotations

from fnmatch import fnmatchcase

from .config import GroupCfg, LadderStep
from .models import Tunnel


def _match(name: str, patterns: list[str]) -> bool:
    return any(fnmatchcase(name, p) for p in patterns)


def _sort_key(t: Tunnel) -> tuple:
    sid = int(t.server_id) if (t.server_id or "").isdigit() else 10**9
    return (t.city or "", sid, t.name)


def resolve_steps(group: GroupCfg, tunnels: list[Tunnel]) -> list[list[Tunnel]]:
    """One ordered list per ladder step; a tunnel appears only in the first step that claims it."""
    seen: set[str] = set()
    steps: list[list[Tunnel]] = []
    for step in group.ladder:
        pool = _pool(step, tunnels)
        pool = [t for t in pool if t.id not in seen]
        preferred: list[Tunnel] = []
        for pat in step.prefer:
            for t in pool:
                if fnmatchcase(t.name, pat) and t not in preferred:
                    preferred.append(t)
        rest = sorted((t for t in pool if t not in preferred), key=_sort_key)
        ordered = preferred + rest
        seen.update(t.id for t in ordered)
        steps.append(ordered)
    return steps


def _pool(step: LadderStep, tunnels: list[Tunnel]) -> list[Tunnel]:
    pool = list(tunnels)
    if step.country:
        pool = [t for t in pool if t.iso == step.country]
    if step.tunnels:
        pool = [t for t in pool if _match(t.name, step.tunnels)]
    if step.exclude:
        pool = [t for t in pool if not _match(t.name, step.exclude)]
    return pool


def resolve_ladder(group: GroupCfg, tunnels: list[Tunnel]) -> list[Tunnel]:
    return [t for step in resolve_steps(group, tunnels) for t in step]


def candidates(
    group: GroupCfg,
    tunnels: list[Tunnel],
    current: Tunnel | None,
    prefer_different_city: bool,
) -> list[Tunnel]:
    """Failover order: ladder order, never the current tunnel. Inside the current tunnel's own country,
    servers in other cities come before further servers in the same city."""
    out: list[Tunnel] = []
    for step in resolve_steps(group, tunnels):
        cands = [t for t in step if not (current and t.id == current.id)]
        if prefer_different_city and current and current.iso and current.city:
            cands.sort(key=lambda t: 1 if (t.iso == current.iso and t.city == current.city) else 0)
        out.extend(cands)
    return out
