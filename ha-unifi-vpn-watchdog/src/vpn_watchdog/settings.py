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
from .ladder import suggest_groups
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
            "on_exhausted": cfg.switching.on_exhausted,
        },
        "failback": {
            "enabled": cfg.failback.enabled,
            "check_interval_seconds": cfg.failback.check_interval_seconds,
            "stable_seconds": cfg.failback.stable_seconds,
        },
        "standby": {
            "warm": cfg.standby.warm,
            "disable_unused": cfg.standby.disable_unused,
            "max_enabled": cfg.standby.max_enabled,
        },
        "probe": {
            "mode": cfg.probe.mode,
            "check_country": cfg.probe.check_country,
            "canary_mac": cfg.probe.canary.mac or "",
            "remote_url": cfg.probe.remote_url or "",
        },
        "alerts": {n: {"enabled": a.enabled, "title": a.title, "message": a.message} for n, a in cfg.alerts.items()},
        "groups": [
            {
                "name": g.name,
                "networks": list(g.networks),
                "kill_switch": g.kill_switch,
                "ladder": [
                    {"name": st.name or "", "tunnels": list(st.tunnels), "prefer": list(st.prefer),
                     "exclude": list(st.exclude), "expect_country": st.expect_country or ""}
                    for st in g.ladder
                ],
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
    if "on_exhausted" in s:
        if s["on_exhausted"] not in ("keep", "kill_switch"):
            raise ValueError("on_exhausted must be keep or kill_switch")
        sw["on_exhausted"] = s["on_exhausted"]

    f = form.get("failback", {})
    fb = out.setdefault("failback", {})
    if "enabled" in f:
        fb["enabled"] = bool(f["enabled"])
    for k in ("check_interval_seconds", "stable_seconds"):
        if k in f:
            fb[k] = _num(f[k])

    sb = form.get("standby", {})
    st = out.setdefault("standby", {})
    for k in ("warm", "max_enabled"):
        if k in sb:
            st[k] = _num(sb[k])
    if "disable_unused" in sb:
        st["disable_unused"] = bool(sb["disable_unused"])

    p = form.get("probe", {})
    pr = out.setdefault("probe", {})
    if "mode" in p:
        pr["mode"] = p["mode"]
    if "check_country" in p:
        pr["check_country"] = bool(p["check_country"])
    if "canary_mac" in p:
        mac = str(p["canary_mac"]).strip()
        if mac:
            pr.setdefault("canary", {})["mac"] = mac
        elif isinstance(pr.get("canary"), dict):
            pr["canary"].pop("mac", None)
    if "remote_url" in p:
        url = str(p["remote_url"]).strip()
        if url:
            pr["remote_url"] = url
        else:
            pr.pop("remote_url", None)

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
            g = copy.deepcopy(existing.get(fg["name"], {}))      # keeps route_id and overrides
            g["name"] = str(fg["name"]).strip()
            g["networks"] = [str(n) for n in fg.get("networks", [])]
            if fg.get("kill_switch") is None:
                g.pop("kill_switch", None)
            else:
                g["kill_switch"] = bool(fg["kill_switch"])
            ladder = []
            for step in fg.get("ladder", []):
                clean = {}
                if str(step.get("name") or "").strip():
                    clean["name"] = str(step["name"]).strip()
                for key in ("tunnels", "prefer", "exclude"):
                    if step.get(key):
                        clean[key] = [str(x) for x in step[key]]
                if str(step.get("expect_country") or "").strip():
                    clean["expect_country"] = str(step["expect_country"]).strip().upper()
                ladder.append(clean)
            g["ladder"] = ladder
            groups.append(g)
        out["groups"] = groups
    return out


def meta(snap: Snapshot | None) -> dict[str, Any]:
    if snap is None:
        return {"networks": [], "tunnels": [], "suggestions": [], "ready": False, "alert_info": ALERT_INFO}
    tunnel_ids = set(snap.tunnels)
    networks = sorted(
        n for i, n in snap.networks.items()
        if i not in tunnel_ids and not n.startswith("Internet") and n != "One-Click VPN"
    )
    tunnels = sorted(snap.tunnels.values(), key=lambda t: t.name.lower())
    return {
        "alert_info": ALERT_INFO,
        "networks": networks,
        "tunnels": [{"name": t.name, "enabled": t.enabled} for t in tunnels],
        "suggestions": suggest_groups(tunnels),
        "ready": True,
    }
