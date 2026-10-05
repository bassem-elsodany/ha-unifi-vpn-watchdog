from __future__ import annotations

import dataclasses
import re

import pytest

from vpn_watchdog.config import DEFAULT_NAMING, parse_config
from vpn_watchdog.engine import Engine
from vpn_watchdog.models import Connection, ProbeResult, Route, Snapshot, parse_tunnel
from vpn_watchdog.state import StateStore

PATTERN = re.compile(DEFAULT_NAMING)
NETS = {"net-iot": "vlan20-iot", "net-vpn": "vlan50-vpn"}

TUNNELS = [
    "IT__ROME__418__1.1.1.1", "IT__ROME__511__1.1.1.2", "IT__MILAN__244__1.1.1.3",
    "DE__BERLIN__1552__2.2.2.1", "DE__FRANKFURT__1287__2.2.2.2",
    "FR__PARIS__953__3.3.3.1",
]


def tid(name: str) -> str:
    return f"id-{name}"


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeUniFi:
    """In-memory UniFi. `dead` tunnels never connect; `muted` tunnels connect but receive nothing."""

    def __init__(self, active: str = "IT__ROME__418__1.1.1.1", enabled: set[str] | None = None):
        self.dry_run = False
        self.dead: set[str] = set()
        self.muted: set[str] = set()
        self.enabled = {tid(n) for n in (enabled if enabled is not None else {active})}
        self.calls: list[tuple] = []
        self.route = Route("route-1", active, tid(active), True, False, frozenset(NETS), frozenset(),
                           {"description": active, "network_id": tid(active), "kill_switch_enabled": False})
        self.extra_routes: list[Route] = []

    def snapshot(self) -> Snapshot:
        tunnels = {}
        conns = {}
        for n in TUNNELS:
            i = tid(n)
            tunnels[i] = parse_tunnel({"_id": i, "name": n, "enabled": i in self.enabled}, PATTERN)
            if i in self.enabled:
                bad = i in self.dead
                conns[i] = Connection(i, "CONNECTING" if bad else "CONNECTED", None if bad else "9.9.9.9",
                                      None if bad else (0 if i in self.muted else 5000), None if bad else 6000)
        return Snapshot(tunnels, conns, [self.route, *self.extra_routes], dict(NETS), "92.0.0.1")

    def set_tunnel_enabled(self, i, en):
        self.calls.append(("enable", i, en))
        (self.enabled.add if en else self.enabled.discard)(i)

    def set_route(self, route, *, network_id=None, description=None, kill_switch=None, enabled=None):
        self.calls.append(("route", network_id, kill_switch))
        r = self.route if route.id == self.route.id else next(x for x in self.extra_routes if x.id == route.id)
        upd = {}
        if network_id is not None:
            upd["network_id"] = network_id
        if kill_switch is not None:
            upd["kill_switch"] = kill_switch
        if enabled is not None:
            upd["enabled"] = enabled
        new = dataclasses.replace(r, **upd, description=description or r.description,
                                  raw={**r.raw, "description": description or r.description})
        if r is self.route:
            self.route = new

    def create_route(self, *a, **k):
        self.calls.append(("create", a, k))

    def close(self):
        pass


class FakeTester:
    def __init__(self, can_pretest=True, enabled=True):
        self.can_pretest = can_pretest
        self.enabled = enabled
        self.bad: set[str] = set()
        self.leak: set[str] = set()
        self.tested: list[tuple[str, bool]] = []

    def test(self, tunnel, snap, *, pre):
        self.tested.append((tunnel.name, pre))
        if tunnel.id in self.leak:
            return ProbeResult(False, "LEAK", leak=True)
        return ProbeResult(tunnel.id not in self.bad, "ok" if tunnel.id not in self.bad else "no traffic")


class RecordingNotifier:
    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def emit(self, event, title, message, level="info", key=None, **fields):
        self.events.append((event, title))

    def kinds(self):
        return [e for e, _ in self.events]


CFG = """
dry_run: false
interval_seconds: 15
state_file: /tmp/unused
unifi: {{api_key: x}}
detection: {{failure_threshold: 3, probe_failure_threshold: 2, probe_interval_seconds: 15}}
switching: {{connect_timeout_seconds: 10, min_hold_seconds: 60, max_switches_per_hour: 6,
            quarantine: {{base_seconds: 120, factor: 2, max_seconds: 3600}} {extra_switching} }}
failback: {{enabled: {failback}, check_interval_seconds: 30, stable_seconds: 60}}
standby: {{warm: 1, disable_unused: true, max_enabled: 6}}
groups:
  - name: g1
    networks: [vlan20-iot, vlan50-vpn]
    ladder:
      - {{country: IT, prefer: ["IT__ROME__418__*"]}}
      - {{country: DE}}
      - {{country: FR}}
"""


@pytest.fixture
def make_engine():
    def _make(active="IT__ROME__418__1.1.1.1", failback=True, extra_switching="", pretest=True, tester_enabled=True, enabled=None):
        cfg = parse_config(CFG.format(failback=str(failback).lower(), extra_switching=extra_switching), env={})
        clock = FakeClock()
        un = FakeUniFi(active, enabled)
        tester = FakeTester(can_pretest=pretest, enabled=tester_enabled)
        notes = RecordingNotifier()
        eng = Engine(cfg, un, tester, notes, StateStore(None), clock)
        return eng, un, tester, notes, clock
    return _make


def run(eng, clock, ticks, step=15):
    for _ in range(ticks):
        eng.tick()
        clock.t += step


def active_name(un: FakeUniFi) -> str:
    return un.route.description
