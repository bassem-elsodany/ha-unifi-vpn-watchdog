from __future__ import annotations

import dataclasses
import pytest

from vpn_watchdog.config import parse_config
from vpn_watchdog.engine import Engine
from vpn_watchdog.models import Connection, ProbeResult, Route, Snapshot, parse_tunnel
from vpn_watchdog.state import StateStore

NETS = {"net-iot": "vlan20-iot", "net-vpn": "vlan50-vpn"}

ORDER = '[Home-Primary, Home-Backup, "Cousin vpn 2", {tunnel: office/berlin, expect_country: DE}, office/frankfurt, zzz-last-resort]'

TUNNELS = [
    "Home-Primary", "Home-Backup", "Cousin vpn 2",
    "office/berlin", "office/frankfurt",
    "zzz-last-resort",
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
    """In-memory UniFi with a routing policy per tunnel (like the real setup; the watchdog only reads them).
    `dead` tunnels never connect; `muted` tunnels connect but receive nothing."""

    def __init__(self, active: str = "Home-Primary", enabled: set[str] | None = None, with_policies: bool = True):
        self.dead: set[str] = set()
        self.muted: set[str] = set()
        self.enabled = {tid(n) for n in (enabled if enabled is not None else {active})}
        self.calls: list[tuple] = []
        self.routes: list[Route] = []
        self.clients: dict = {}        # online devices: mac -> {name, ip, network_id, ...}
        self.known: dict = {}          # every known device (the device picker)
        if with_policies:
            for n in TUNNELS:
                self.routes.append(self._mk(f"route-{n}", n, n == active))

    @staticmethod
    def _mk(rid, name, on, kill=False):
        return Route(rid, name, tid(name), on, kill, frozenset(NETS), frozenset(), {"description": name})

    def active_names(self) -> list[str]:
        """The VPN clients that are switched on (the only thing the watchdog ever changes)."""
        return [n for n in TUNNELS if tid(n) in self.enabled]

    def active_route(self):
        return next((r for r in self.routes if r.enabled), None)

    def snapshot(self) -> Snapshot:
        tunnels = {}
        conns = {}
        for n in TUNNELS:
            i = tid(n)
            tunnels[i] = parse_tunnel({"_id": i, "name": n, "enabled": i in self.enabled})
            if i in self.enabled:
                bad = i in self.dead
                conns[i] = Connection(i, "CONNECTING" if bad else "CONNECTED", None if bad else "9.9.9.9",
                                      None if bad else (0 if i in self.muted else 5000), None if bad else 6000)
        info = {i: {"name": n, "vlan": None, "subnet": None, "purpose": "corporate"} for i, n in NETS.items()}
        return Snapshot(tunnels, conns, list(self.routes), dict(NETS), "92.0.0.1", info, dict(self.clients), dict(self.known))

    def create_own_route(self, body):
        assert body["description"].startswith("vpnwd:")
        self.calls.append(("route-create", body["description"], body["network_id"]))
        nets = frozenset(t["network_id"] for t in body["target_devices"] if t.get("type") == "NETWORK")
        macs = frozenset(t["client_mac"] for t in body["target_devices"] if t.get("type") == "CLIENT")
        self._seq = getattr(self, "_seq", 0) + 1
        self.routes.append(Route(f"own-{self._seq}", body["description"], body["network_id"], body.get("enabled", True), False, nets, macs, dict(body)))

    def update_own_route(self, route_id, network_id, enabled=True, target_devices=None, description=None):
        import dataclasses
        i = next(k for k, r in enumerate(self.routes) if r.id == route_id)
        assert self.routes[i].description.startswith("vpnwd:")
        self.calls.append(("route-update", route_id, network_id))
        macs = self.routes[i].target_macs if target_devices is None else frozenset(t["client_mac"] for t in target_devices)
        self.routes[i] = dataclasses.replace(self.routes[i], network_id=network_id, enabled=enabled, target_macs=macs, **({"description": description} if description else {}))

    def set_route_enabled(self, route_id, enabled):
        import dataclasses
        i = next(k for k, r in enumerate(self.routes) if r.id == route_id)
        self.calls.append(("route-enabled", route_id, enabled))
        self.routes[i] = dataclasses.replace(self.routes[i], enabled=enabled)
        return self.routes[i].description

    def delete_own_route(self, route_id):
        assert next(r for r in self.routes if r.id == route_id).description.startswith("vpnwd:")
        self.calls.append(("route-delete", route_id))
        self.routes = [r for r in self.routes if r.id != route_id]

    def set_tunnel_enabled(self, i, en):
        self.calls.append(("enable", i, en))
        (self.enabled.add if en else self.enabled.discard)(i)

    def close(self):
        pass


class FakeTester:
    def __init__(self, can_pretest=True, enabled=True):
        self.can_pretest = can_pretest
        self.enabled = enabled
        self.bad: set[str] = set()
        self.leak: set[str] = set()
        self.tested: list[tuple[str, bool]] = []
        self.expects: list = []

    def test(self, tunnel, snap, *, pre, expect=None):
        self.tested.append((tunnel.name, pre))
        self.expects.append(expect)
        if tunnel.id in self.leak:
            return ProbeResult(False, "LEAK", leak=True)
        return ProbeResult(tunnel.id not in self.bad, "ok" if tunnel.id not in self.bad else "no traffic")


class RecordingNotifier:
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.checks: list[dict] = []

    def record_check(self, message):
        self.checks.append({"ts": 0, "event": "check", "level": "info", "message": message})

    def emit(self, event, title, message, level="info", key=None, **fields):
        self.events.append((event, title))

    def kinds(self):
        return [e for e, _ in self.events]


CFG = """
interval_seconds: 15
state_file: /tmp/unused
unifi: {{api_key: x}}
detection: {{failure_threshold: 3, probe_failure_threshold: 2, probe_interval_seconds: 15}}
switching: {{connect_timeout_seconds: 10, min_hold_seconds: 60, max_switches_per_hour: 6,
            quarantine: {{base_seconds: 120, factor: 2, max_seconds: 3600}} {extra_switching} }}
failback: {{enabled: {failback}, check_interval_seconds: 30, stable_seconds: 60}}
groups:
  - name: g1
    networks: [vlan20-iot, vlan50-vpn]
    order: {order}
"""


@pytest.fixture
def make_engine():
    def _make(active="Home-Primary", failback=True, extra_switching="", pretest=True, tester_enabled=True, enabled=None, order=ORDER, with_policies=True):
        cfg = parse_config(CFG.format(failback=str(failback).lower(), extra_switching=extra_switching, order=order), env={})
        clock = FakeClock()
        un = FakeUniFi(active, enabled, with_policies)
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
    on = un.active_names()
    assert len(on) == 1, f"exactly one policy must be on, got {on}"
    return on[0]
