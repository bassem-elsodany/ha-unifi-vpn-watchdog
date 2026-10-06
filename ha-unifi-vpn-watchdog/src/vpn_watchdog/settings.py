"""Form-friendly view of the configuration, so the UI can offer fields instead of YAML.

`extract` reads the effective values; `apply` writes the form back into the raw YAML dict (keys the form does not
know about, such as secrets, route pins and per-group overrides, are preserved); `meta` lists what the UI can offer
(networks, tunnels, suggested steps) from the last UniFi snapshot.
"""
from __future__ import annotations

import copy
from typing import Any

from .alerts import EVENTS, PLACEHOLDERS
from .config import Config
from .models import Snapshot

ALERT_INFO = {
    "events": {n: {"when": d["when"], "level": d["level"]} for n, d in EVENTS.items()},
    "placeholders": PLACEHOLDERS,
}


def extract(cfg: Config) -> dict[str, Any]:
    return {
        "interval_seconds": cfg.interval_seconds,
        "detection": {
            "failure_threshold": cfg.detection.failure_threshold,
            "probe_failure_threshold": cfg.detection.probe_failure_threshold,
            "probe_interval_seconds": cfg.detection.probe_interval_seconds,
            "blackhole_enabled": cfg.detection.blackhole.enabled,
        },
        "switching": {
            "min_hold_seconds": cfg.switching.min_hold_seconds,
            "max_switches_per_hour": cfg.switching.max_switches_per_hour,
            "connect_timeout_seconds": cfg.switching.connect_timeout_seconds,
        },
        "failback": {
            "enabled": cfg.failback.enabled,
            "check_interval_seconds": cfg.failback.check_interval_seconds,
            "stable_seconds": cfg.failback.stable_seconds,
        },
        "probe": {
            "mode": cfg.probe.mode,
            "check_country": cfg.probe.check_country,
        },
        "alerts": {n: {"enabled": a.enabled, "title": a.title, "message": a.message} for n, a in cfg.alerts.items()},
        "jobs": [{"kind": j.kind, "group": j.group, "enabled": j.enabled, "every": j.every, "unit": j.unit, "at": j.at, "go_to": j.go_to}
                 for j in cfg.jobs],
        "groups": [
            {
                "name": g.name,
                "_orig": g.name,
                "order": [{"tunnel": i.tunnel, "id": i.id, "expect_country": i.expect_country or ""} for i in g.order],
            }
            for g in cfg.groups
        ],
    }


def _num(v: Any, kind=int):
    return kind(v)


def apply(raw: dict[str, Any], form: dict[str, Any]) -> dict[str, Any]:
    """Return a new raw config dict with the form's values applied. Raises KeyError/ValueError/TypeError on bad input."""
    out = copy.deepcopy(raw)
    if "interval_seconds" in form:
        out["interval_seconds"] = _num(form["interval_seconds"])

    d = form.get("detection", {})
    det = out.setdefault("detection", {})
    for k in ("failure_threshold", "probe_failure_threshold", "probe_interval_seconds"):
        if k in d:
            det[k] = _num(d[k])
    if "blackhole_enabled" in d:
        det.setdefault("blackhole", {})["enabled"] = bool(d["blackhole_enabled"])

    s = form.get("switching", {})
    sw = out.setdefault("switching", {})
    for k in ("min_hold_seconds", "max_switches_per_hour"):
        if k in s:
            sw[k] = _num(s[k])
    if "connect_timeout_seconds" in s:
        sw["connect_timeout_seconds"] = _num(s["connect_timeout_seconds"], float)

    f = form.get("failback", {})
    fb = out.setdefault("failback", {})
    if "enabled" in f:
        fb["enabled"] = bool(f["enabled"])
    for k in ("check_interval_seconds", "stable_seconds"):
        if k in f:
            fb[k] = _num(f[k])

    p = form.get("probe", {})
    pr = out.setdefault("probe", {})
    if "mode" in p:
        pr["mode"] = p["mode"]
    if "check_country" in p:
        pr["check_country"] = bool(p["check_country"])
    if "alerts" in form:
        al = out.setdefault("alerts", {})
        for name, a in form["alerts"].items():
            if name not in EVENTS:
                raise ValueError(f"unknown alert {name!r}")
            al[name] = {"enabled": bool(a["enabled"]), "title": str(a["title"]), "message": str(a["message"])}

    if "groups" in form:
        existing = {g.get("name"): g for g in out.get("groups", []) if isinstance(g, dict)}
        groups = []
        for fg in form["groups"]:
            g = copy.deepcopy(existing.get(fg.get("_orig") or "", {}))      # keeps per-group overrides across a rename
            g["name"] = str(fg["name"]).strip()
            g.pop("networks", None)                          # which VLANs use a VPN client is UniFi's business, not the group's
            g.pop("kill_switch", None)
            order = []
            for item in fg.get("order", []):
                name = str(item.get("tunnel") or "").strip()
                if not name:
                    continue                      # an unfilled row is simply dropped
                entry: dict[str, Any] = {"tunnel": name}
                if str(item.get("id") or "").strip():
                    entry["id"] = str(item["id"]).strip()
                if str(item.get("expect_country") or "").strip():
                    entry["expect_country"] = str(item["expect_country"]).strip().upper()
                order.append(entry)
            g["order"] = [e["tunnel"] if set(e) == {"tunnel"} else e for e in order]
            g.pop("ladder", None)
            groups.append(g)
        out["groups"] = groups
        renamed = {fg.get("_orig"): str(fg["name"]).strip() for fg in form["groups"] if fg.get("_orig")}
        if "jobs" not in form:                    # a rename keeps the group's jobs with it
            for j in out.get("jobs") or []:
                j["group"] = renamed.get(j.get("group"), j.get("group"))
    if "jobs" in form:
        renamed = {fg.get("_orig"): str(fg["name"]).strip() for fg in form.get("groups", []) if fg.get("_orig")}
        names = {g.get("name") for g in out.get("groups", []) if isinstance(g, dict)}
        jobs = []
        for j in form["jobs"]:
            grp = renamed.get(j.get("group"), j.get("group"))
            if grp not in names:
                continue                          # its group was deleted: so are its jobs
            jobs.append({"kind": str(j.get("kind") or "rotation"), "group": grp, "enabled": bool(j.get("enabled", True)),
                         "every": _num(j["every"]), "unit": str(j["unit"]), "at": str(j.get("at") or "03:00"), "go_to": str(j.get("go_to") or "next")})
        out["jobs"] = jobs
    return out


def meta(snap: Snapshot | None) -> dict[str, Any]:
    if snap is None:
        return {"tunnels": [], "ready": False, "alert_info": ALERT_INFO}
    tunnels = sorted(snap.tunnels.values(), key=lambda t: t.name.lower())
    return {
        "alert_info": ALERT_INFO,
        "tunnels": [{"id": t.id, "name": t.name, "enabled": t.enabled} for t in tunnels],
        "ready": True,
    }
