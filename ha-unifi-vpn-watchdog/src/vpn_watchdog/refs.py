"""Everything in the config that points at something in UniFi points at its id, never only at its name.

A VPN client or a VLAN can be renamed in UniFi at any time. The config keeps `id` (what is matched) and `name` (a label, and the
way an older config that only has names is found the first time). `normalize` runs on every UniFi reading: it fills in missing ids,
follows a rename by id (refreshing the label), and finds a tunnel or VLAN that was deleted and recreated again by its name."""
from __future__ import annotations

from .config import Config, GroupCfg, OrderItem
from .models import Snapshot


def normalize(cfg: Config, snap: Snapshot) -> bool:
    """Bring ids and labels in line with UniFi. Returns True when something changed (so the config file is rewritten once)."""
    changed = False
    for g in cfg.groups:
        used: set[str] = set()
        for item in g.order:
            t = snap.tunnels.get(item.id or "")
            if t is None:
                hits = [x for x in snap.tunnels.values() if x.name == item.tunnel and x.id not in used]
                t = hits[0] if len(hits) == 1 else None
            if t is None:
                continue
            used.add(t.id)
            if item.id != t.id or item.tunnel != t.name:
                item.id, item.tunnel, changed = t.id, t.name, True
    return changed


def group_raw(g: GroupCfg) -> dict:
    """How a group's references are written to config.yaml."""
    return {
        "order": [{k: v for k, v in (("tunnel", i.tunnel), ("id", i.id), ("expect_country", i.expect_country)) if v} for i in g.order],
    }


def item_for(group: GroupCfg, tunnel_id: str, tunnel_name: str) -> OrderItem | None:
    return next((i for i in group.order if (i.id == tunnel_id) or (not i.id and i.tunnel == tunnel_name)), None)
