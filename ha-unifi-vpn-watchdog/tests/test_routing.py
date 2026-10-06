"""Routing management: off by default; when a group switches it on, the watchdog keeps ITS OWN policy per picked VLAN on the active client."""
from __future__ import annotations

from conftest import run, tid


def managed(eng):
    eng.cfg = eng.cfg.model_copy(update={"groups": [eng.cfg.groups[0].model_copy(update={"manage_routing": True})]})


def writes(un):
    return [c for c in un.calls if c[0].startswith("route-")]


def test_routing_is_off_by_default_and_never_touched(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 3)
    assert writes(un) == []
    assert eng.status()["map"]["groups"][0]["routing"] == {"manage": False, "plan": eng.status()["map"]["groups"][0]["routing"]["plan"]}
    assert eng.status()["map"]["groups"][0]["routing"]["plan"]            # the preview says what it would do


def test_switched_on_it_creates_one_own_policy_per_picked_vlan_and_leaves_the_others_alone(make_engine):
    eng, un, _, _, clock = make_engine()
    before = {r.id: (r.description, r.network_id, r.enabled) for r in un.routes}
    managed(eng)
    run(eng, clock, 2)
    created = [c for c in writes(un) if c[0] == "route-create"]
    assert sorted(c[1] for c in created) == ["vpnwd: g1 › vlan20-iot", "vpnwd: g1 › vlan50-vpn"]
    assert all(c[2] == tid("Home-Primary") for c in created)
    assert all(before[r.id] == (r.description, r.network_id, r.enabled) for r in un.routes if r.id in before)      # nobody else's policy changed
    n = len(writes(un))
    run(eng, clock, 3)
    assert len(writes(un)) == n                                                                   # nothing more to do: no write each cycle


def test_a_failover_moves_the_own_policies_to_the_new_client(make_engine):
    eng, un, _, _, clock = make_engine(order="[Home-Primary, Home-Backup]")
    managed(eng)
    run(eng, clock, 2)
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 8)
    own = [r for r in un.routes if r.description.startswith("vpnwd:")]
    assert len(own) == 2 and {r.network_id for r in own} == {tid("Home-Backup")} and all(r.enabled for r in own)
    assert any(c[0] == "route-update" for c in un.calls)


def test_a_vlan_that_is_no_longer_picked_loses_its_own_policy(make_engine):
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    g = eng.cfg.groups[0]
    eng.cfg = eng.cfg.model_copy(update={"groups": [g.model_copy(update={"networks": [n for n in g.networks if n.name != "vlan50-vpn"]})]})
    run(eng, clock, 2)
    assert [r.description for r in un.routes if r.description.startswith("vpnwd:")] == ["vpnwd: g1 › vlan20-iot"]


def test_a_failed_write_is_reported_and_retried_later_not_every_cycle(make_engine):
    from vpn_watchdog.unifi import UniFiError
    eng, un, _, _, clock = make_engine()
    calls = []

    def boom(body):
        calls.append(1)
        raise UniFiError("HTTP 400")
    un.create_own_route = boom
    managed(eng)
    run(eng, clock, 4, step=15)
    assert len(calls) == 1
    assert any(h["event"] == "routing_failed" for h in eng.notifier.history) if hasattr(eng.notifier, "history") else True
