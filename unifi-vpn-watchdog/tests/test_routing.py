"""Routing management: off by default; when a group switches it on, the watchdog keeps ITS OWN policy per picked VLAN on the active client."""
from __future__ import annotations

from vpn_watchdog.routing import key
from conftest import run, tid


def managed(eng):
    eng.cfg = eng.cfg.model_copy(update={"groups": [eng.cfg.groups[0].model_copy(update={"manage_routing": True})]})


def writes(un):
    return [c for c in un.calls if c[0].startswith("route-")]


def test_routing_is_off_by_default_and_never_touched(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 3)
    assert writes(un) == []
    routing = eng.status()["map"]["groups"][0]["routing"]
    assert routing["manage"] is False and routing["blockers"] == []
    assert routing["plan"]            # the preview says what it would do


def test_switched_on_it_creates_one_own_policy_per_picked_vlan_and_leaves_the_others_alone(make_engine):
    eng, un, _, _, clock = make_engine()
    before = {r.id: (r.description, r.network_id, r.enabled) for r in un.routes}
    managed(eng)
    run(eng, clock, 2)
    created = [c for c in writes(un) if c[0] == "route-create"]
    assert sorted(key(c[1]) for c in created) == ["vpnwd: g1 › vlan20-iot", "vpnwd: g1 › vlan50-vpn"]
    assert all("VPN Watchdog add-on" in c[1] for c in created)             # a person looking at UniFi can tell who made it
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
    assert [key(r.description) for r in un.routes if r.description.startswith("vpnwd:")] == ["vpnwd: g1 › vlan20-iot"]


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


def user_policy(un, rid="mine", enabled=True, nets=frozenset({"net-iot"})):
    from vpn_watchdog.models import Route
    un.routes.insert(0, Route(rid, "my old policy", "wan", enabled, False, nets, frozenset(), {"description": "my old policy"}))


def group_status(eng):
    return eng.status()["map"]["groups"][0]


def test_an_older_policy_of_yours_that_takes_a_picked_vlan_elsewhere_is_reported_as_a_blocker(make_engine):
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    assert group_status(eng)["routing"]["blockers"] == []
    user_policy(un)
    run(eng, clock, 1)
    b = group_status(eng)["routing"]["blockers"]
    assert [x["id"] for x in b] == ["mine"] and b[0]["vlans"] == ["vlan20-iot"] and b[0]["position"] == 1
    assert "vlan50-vpn" not in b[0]["vlans"]


def test_nothing_is_reported_or_changed_when_routing_is_not_managed(make_engine):
    eng, un, _, _, clock = make_engine()
    user_policy(un)
    run(eng, clock, 3)
    assert group_status(eng)["routing"]["blockers"] == [] and not [c for c in un.calls if c[0] == "route-enabled"]


def test_a_policy_for_one_device_or_one_that_is_off_is_not_a_blocker(make_engine):
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    user_policy(un, "off", enabled=False)
    from vpn_watchdog.models import Route
    un.routes.insert(0, Route("dev", "one device", "wan", True, False, frozenset(), frozenset({"aa:bb"}), {"description": "one device"}))
    run(eng, clock, 1)
    assert group_status(eng)["routing"]["blockers"] == []


def test_confirmed_switch_off_then_back_on(make_engine):
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    user_policy(un)
    run(eng, clock, 1)
    eng.submit("blocker-off", "g1", "mine")
    run(eng, clock, 1)
    assert next(r for r in un.routes if r.id == "mine").enabled is False
    s = group_status(eng)["routing"]
    assert s["blockers"] == [] and [x["id"] for x in s["switched_off"]] == ["mine"]
    eng.submit("blocker-on", "g1", "mine")
    run(eng, clock, 1)
    assert next(r for r in un.routes if r.id == "mine").enabled is True and group_status(eng)["routing"]["switched_off"] == []


def test_it_refuses_to_switch_off_a_policy_that_is_not_a_blocker_or_to_switch_on_one_it_did_not_switch_off(make_engine):
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    before = [(r.id, r.enabled) for r in un.routes]
    eng.submit("blocker-off", "g1", "route-Home-Backup")             # a policy of yours that blocks nothing
    eng.submit("blocker-on", "g1", "route-Home-Backup")              # one the watchdog never switched off
    run(eng, clock, 1)
    assert [(r.id, r.enabled) for r in un.routes] == before
    assert [h["event"] for h in eng.notifier.history].count("routing_refused") == 2 if hasattr(eng.notifier, "history") else True


def test_a_deleted_groups_own_policies_are_removed_but_not_when_no_group_is_configured(make_engine):
    from vpn_watchdog.config import GroupCfg
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    own = lambda: sorted(key(r.description) for r in un.routes if r.description.startswith("vpnwd:"))
    assert own() == ["vpnwd: g1 › vlan20-iot", "vpnwd: g1 › vlan50-vpn"]
    g1 = eng.cfg.groups[0]
    eng.cfg = eng.cfg.model_copy(update={"groups": []})                    # a config that lost all its groups must not wipe the policies
    run(eng, clock, 6)
    assert len(own()) == 2
    other = GroupCfg(name="g2", order=[{"tunnel": "Home-Primary"}])
    eng.cfg = eng.cfg.model_copy(update={"groups": [other]})               # g1 was deleted, another group exists
    run(eng, clock, 1)
    assert len(own()) == 2                                                  # one cycle is not enough (it may be a glitch)
    run(eng, clock, 4)
    assert own() == []
    assert all(c[0] != "route-enabled" for c in un.calls)                  # nobody else's policy was touched


def test_a_policy_with_the_old_short_name_is_renamed_in_place(make_engine):
    import dataclasses
    eng, un, _, _, clock = make_engine()
    managed(eng)
    run(eng, clock, 2)
    ids = {r.id for r in un.routes if r.description.startswith("vpnwd:")}
    un.routes = [dataclasses.replace(r, description=key(r.description)) if r.id in ids else r for r in un.routes]
    run(eng, clock, 2)
    own = [r for r in un.routes if r.description.startswith("vpnwd:")]
    assert {r.id for r in own} == ids and all("VPN Watchdog add-on" in r.description for r in own)
