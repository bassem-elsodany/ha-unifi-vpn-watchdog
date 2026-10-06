"""What the watchdog tells you, when, and the default wording. Titles and messages are templates with {placeholders}."""
from __future__ import annotations

import string
from typing import Any

# Placeholders available in every template (empty when not relevant to the event).
PLACEHOLDERS = {
    "group": "VPN group name",
    "tunnel": "active / new tunnel name",
    "previous": "tunnel it moved away from",
    "position": "position of the tunnel in the fallback order, e.g. #2",
    "previous_position": "position of the tunnel it moved away from",
    "reason": "why the decision was taken",
    "tried": "number of candidate tunnels that were tested and failed",
    "groups": "number of groups being watched",
    "mode": "ACTIVE (a fallback order is set) or WATCHING ONLY",
    "error": "error text",
}

EVENTS: dict[str, dict[str, Any]] = {
    "switch": {
        "when": "A tunnel stopped working and traffic was moved to the next tunnel in your fallback order.",
        "level": "warning", "enabled": True,
        "title": "VPN {group}: switched to {tunnel} ({position})",
        "message": "{previous} ({previous_position}) -> {tunnel} ({position}). Reason: {reason}",
    },
    "failback": {
        "when": "A tunnel higher in your fallback order recovered and stayed healthy long enough, so traffic moved back up to it.",
        "level": "info", "enabled": True,
        "title": "VPN {group}: back on {tunnel} ({position})",
        "message": "{previous_position} -> {position}. {reason}",
    },
    "exhausted": {
        "when": "The active tunnel is down and every candidate failed or is paused after failing. Needs your attention.",
        "level": "critical", "enabled": True,
        "title": "VPN {group}: no working tunnel",
        "message": "{tunnel} is down and {tried} other tunnel(s) failed or are cooling down.",
    },
    "leak": {
        "when": "The exit-IP check found traffic leaving with your real WAN address instead of the VPN.",
        "level": "critical", "enabled": True,
        "title": "VPN LEAK on {group}",
        "message": "Traffic through {tunnel} exits with your real WAN IP.",
    },
    "recovered": {
        "when": "After a 'no working tunnel' alert, a tunnel is healthy again.",
        "level": "info", "enabled": True,
        "title": "VPN {group} recovered",
        "message": "{tunnel} is healthy again.",
    },
    "blocked": {
        "when": "A switch was needed but held back by the anti-flapping limits (too many switches, or too soon).",
        "level": "warning", "enabled": True,
        "title": "VPN {group}: switch held back",
        "message": "{reason}",
    },
    "startup": {
        "when": "The watchdog (re)started.",
        "level": "info", "enabled": False,
        "title": "VPN Watchdog started",
        "message": "{groups} group(s), {mode}.",
    },
    "config_error": {
        "when": "A saved configuration was rejected as invalid; the previous one keeps running.",
        "level": "warning", "enabled": True,
        "title": "VPN Watchdog config rejected",
        "message": "{error}",
    },
}


def unknown_placeholders(template: str) -> list[str]:
    try:
        names = [f for _, f, _, _ in string.Formatter().parse(template) if f]
    except ValueError as e:
        return [f"bad template: {e}"]
    return sorted({n for n in names if n not in PLACEHOLDERS})


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


def render(template: str, fields: dict[str, Any]) -> str:
    return string.Formatter().vformat(template, (), _Blank({k: ("" if v is None else v) for k, v in fields.items()}))
