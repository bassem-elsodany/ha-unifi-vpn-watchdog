"""Small JSON state file so quarantines, switch history and pauses survive container restarts."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

log = logging.getLogger("vpn_watchdog.state")


@dataclass
class TunnelState:
    quarantined_until: float = 0.0
    fail_streak: int = 0
    last_reason: str = ""
    last_ok_ts: float | None = None


@dataclass
class GroupState:
    route_id: str | None = None
    current_id: str | None = None
    since: float | None = None
    last_switch_ts: float = 0.0
    switches: list[float] = field(default_factory=list)
    paused: bool = False
    status_failures: int = 0
    probe_failures: int = 0
    last_probe_ts: float = 0.0
    last_probe: dict[str, Any] | None = None
    last_failback_check: float = 0.0
    failback_target: str | None = None
    failback_stable_since: float | None = None
    exhausted: bool = False
    last_decision: str = ""
    healthy: bool | None = None      # None = not evaluated yet (e.g. paused before the first check)


def _load_into(cls: type, data: dict[str, Any]):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


class StateStore:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.tunnels: dict[str, TunnelState] = {}
        self.groups: dict[str, GroupState] = {}
        self.settings: dict[str, Any] = {}      # choices made in the web UI (e.g. notify_service)
        self._dirty = False
        self._load()

    def tunnel(self, tunnel_id: str) -> TunnelState:
        return self.tunnels.setdefault(tunnel_id, TunnelState())

    def group(self, name: str) -> GroupState:
        return self.groups.setdefault(name, GroupState())

    def touch(self) -> None:
        self._dirty = True

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.tunnels = {k: _load_into(TunnelState, v) for k, v in data.get("tunnels", {}).items()}
            self.groups = {k: _load_into(GroupState, v) for k, v in data.get("groups", {}).items()}
            self.settings = dict(data.get("settings", {}))
        except (OSError, ValueError, TypeError) as e:
            log.warning("ignoring unreadable state file %s: %s", self.path, e)

    def save(self, force: bool = False) -> None:
        if not self.path or not (self._dirty or force):
            return
        payload = {
            "tunnels": {k: asdict(v) for k, v in self.tunnels.items()},
            "groups": {k: asdict(v) for k, v in self.groups.items()},
            "settings": self.settings,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=1)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError as e:
            log.error("cannot write state file %s: %s", self.path, e)
