"""Turns a group's declarative ladder into an ordered list of concrete tunnels."""
from __future__ import annotations

from fnmatch import fnmatchcase

from .config import GroupCfg, LadderStep
from .models import Tunnel


def _match(name: str, patterns: list[str]) -> bool:
    return any(name == p or fnmatchcase(name, p) for p in patterns)


def _sort_key(t: Tunnel) -> list:
    """Natural order (server2 before server10) so a glob expands predictably, whatever the names look like."""
    import re

    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", t.name)]


def resolve_steps(group: GroupCfg, tunnels: list[Tunnel]) -> list[list[Tunnel]]:
    """One ordered list per ladder step; a tunnel appears only in the first step that claims it.
    Inside a step the order is: `prefer` first, then the `tunnels` entries in the order written
    (a glob expands alphabetically)."""
    seen: set[str] = set()
    steps: list[list[Tunnel]] = []
    for step in group.ladder:
        pool = [t for t in _pool(step, tunnels) if t.id not in seen]
        if step.tunnels:
            ordered: list[Tunnel] = []
            for pat in step.tunnels:
                for t in sorted((t for t in pool if t.name == pat or fnmatchcase(t.name, pat)), key=_sort_key):
                    if t not in ordered:
                        ordered.append(t)
        else:
            ordered = sorted(pool, key=_sort_key)
        preferred = [t for pat in step.prefer for t in ordered if t.name == pat or fnmatchcase(t.name, pat)]
        preferred = list(dict.fromkeys(preferred))
        ordered = preferred + [t for t in ordered if t not in preferred]
        seen.update(t.id for t in ordered)
        steps.append(ordered)
    return steps


def _pool(step: LadderStep, tunnels: list[Tunnel]) -> list[Tunnel]:
    pool = [t for t in tunnels if _match(t.name, step.tunnels)]
    if step.exclude:
        pool = [t for t in pool if not _match(t.name, step.exclude)]
    return pool


def step_label(group: GroupCfg, index: int) -> str:
    return group.ladder[index].name or f"Step {index + 1}"


def step_of(group: GroupCfg, tunnels: list[Tunnel], tunnel: Tunnel | None) -> tuple[int, str] | None:
    if tunnel is None:
        return None
    for i, members in enumerate(resolve_steps(group, tunnels)):
        if any(t.id == tunnel.id for t in members):
            return i, step_label(group, i)
    return None


def expected_country(group: GroupCfg, tunnels: list[Tunnel], tunnel: Tunnel) -> str | None:
    """Country the exit-IP test must see, if the user typed one for the step. Otherwise the test only requires that
    the exit works and is not the WAN address."""
    hit = step_of(group, tunnels, tunnel)
    return group.ladder[hit[0]].expect_country if hit else None


def suggest_groups(tunnels: list[Tunnel]) -> list[dict]:
    """Pre-fill for the UI: group tunnels that share a first word (split on space _ - . : /), else one list of all."""
    import re

    if not tunnels:
        return []
    by_tok: dict[str, list[str]] = {}
    for t in sorted(tunnels, key=_sort_key):
        by_tok.setdefault(re.split(r"[\s_\-.:/]+", t.name.strip())[0], []).append(t.name)
    multi = {k: v for k, v in by_tok.items() if len(v) >= 2}
    if len(multi) >= 2 and sum(len(v) for v in multi.values()) * 2 >= len(tunnels):
        out = [{"label": k, "tunnels": v, "expect_country": None} for k, v in multi.items()]
        rest = [n for k, v in by_tok.items() if k not in multi for n in v]
        if rest:
            out.append({"label": "Others", "tunnels": rest, "expect_country": None})
        return out
    return [{"label": "All tunnels", "tunnels": [t.name for t in sorted(tunnels, key=_sort_key)], "expect_country": None}]


def failback_targets(group: GroupCfg, tunnels: list[Tunnel], current: Tunnel) -> list[Tunnel]:
    """Tunnels the watchdog may move BACK to: every tunnel of an earlier step, plus the tunnels the user marked `prefer`
    in the current step. Position inside a step is never a preference, so nothing moves back just because it sorts first."""
    steps = resolve_steps(group, tunnels)
    for si, members in enumerate(steps):
        if any(t.id == current.id for t in members):
            earlier = [t for m in steps[:si] for t in m]
            step = group.ladder[si]
            preferred = [t for t in members if t.id != current.id and _match(t.name, step.prefer)] if step.prefer else []
            # a preferred tunnel only counts if the current one is not itself preferred
            if step.prefer and _match(current.name, step.prefer):
                preferred = []
            return earlier + preferred
    return []


def resolve_ladder(group: GroupCfg, tunnels: list[Tunnel]) -> list[Tunnel]:
    return [t for step in resolve_steps(group, tunnels) for t in step]


def candidates(group: GroupCfg, tunnels: list[Tunnel], current: Tunnel | None) -> list[Tunnel]:
    """Failover order: ladder order, never the current tunnel."""
    return [t for step in resolve_steps(group, tunnels) for t in step if not (current and t.id == current.id)]
