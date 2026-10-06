"""A group can route devices (by MAC) as well as VLANs: one policy of its own for all its devices, always above the watchdog's VLAN policies."""
from __future__ import annotations

import pytest

from conftest import run, tid
from vpn_watchdog.config import DevRef, GroupCfg, parse_config
from vpn_watchdog.models import Route

TV, CAM = "aa:bb:cc:dd:ee:01", "aa:bb:cc:dd:ee:02"


def setup(eng, un, devices=(TV,), vlans=True, routing=True):
    un.clients = {TV: {"name": "livingTv", "ip": "10.0.20.5", "network_id": "net-iot", "network": "vlan20-iot", "rate_bps": 0, "wired": False}}
    un.known = dict(un.clients)
    g = eng.cfg.groups[0]
    upd = {"devices": [DevRef(mac=m) for m in devices], "manage_routing": routing}
    if not vlans:
        upd["networks"] = []
    eng.cfg = eng.cfg.model_copy(update={"groups": [g.model_copy(update=upd)]})


def own(un):
    return [r for r in un.routes if r.description.startswith("vpnwd:")]


def test_a_mac_is_normalised_and_a_bad_one_is_refused():
    assert DevRef(mac="AA-BB-CC-DD-EE-01").mac == TV
    with pytest.raises(ValueError):
        DevRef(mac="not-a-mac")
    cfg = parse_config("unifi: {api_key: x}\ngroups: [{name: tv, order: [a], devices: [AABBCCDDEE01]}]", env={})
    assert cfg.groups[0].devices[0].mac == TV
    with pytest.raises(Exception):
        parse_config("unifi: {api_key: x}\ngroups: [{name: tv, order: [a], devices: [AABBCCDDEE01, aa:bb:cc:dd:ee:01]}]", env={})


def test_one_device_policy_for_all_the_devices_of_the_group_on_the_active_client(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, devices=(TV, CAM), vlans=False)
    run(eng, clock, 3)
    pol = own(un)
    assert [r.description for r in pol] == ["vpnwd: g1 › devices"]
    assert pol[0].target_macs == {TV, CAM} and pol[0].network_id == tid("Home-Primary") and pol[0].enabled


def test_the_device_policy_follows_a_failover_and_a_change_of_devices(make_engine):
    eng, un, _, _, clock = make_engine(order="[Home-Primary, Home-Backup]")
    setup(eng, un, vlans=False)
    run(eng, clock, 2)
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 8)
    assert own(un)[0].network_id == tid("Home-Backup")
    setup(eng, un, devices=(TV, CAM), vlans=False)
    run(eng, clock, 2)
    assert own(un)[0].target_macs == {TV, CAM} and len(own(un)) == 1


def test_a_device_policy_created_after_the_vlan_policies_gets_them_moved_below_it_without_a_gap(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, devices=(), routing=True)                                   # VLAN policies only
    run(eng, clock, 3)
    first = {r.description: r.id for r in own(un)}
    assert set(first) == {"vpnwd: g1 › vlan20-iot", "vpnwd: g1 › vlan50-vpn"}
    setup(eng, un, devices=(TV,))                                              # now the TV: its policy lands at the end of UniFi's list
    n = len(un.calls)
    run(eng, clock, 4)
    descs = [r.description for r in own(un)]
    assert descs[0] == "vpnwd: g1 › devices"                                  # the device policy now comes first
    assert sorted(descs[1:]) == ["vpnwd: g1 › vlan20-iot", "vpnwd: g1 › vlan50-vpn"] and len(descs) == 3     # every VLAN still has exactly one policy
    assert all(r.id not in first.values() for r in own(un) if r.description != "vpnwd: g1 › devices")        # they are new copies
    calls = [c for c in un.calls[n:] if c[0].startswith("route-")]
    for d in ("vlan20-iot", "vlan50-vpn"):                                   # for each VLAN the new copy was created before the old one was deleted
        c = next(i for i, x in enumerate(calls) if x[0] == "route-create" and d in x[1])
        x = next(i for i, y in enumerate(calls) if y[0] == "route-delete" and y[1] == first[f"vpnwd: g1 › {d}"])
        assert c < x
    before = len(un.calls)
    run(eng, clock, 3)
    assert len(un.calls) == before                                            # settled: nothing more to do


def test_a_device_belongs_to_one_group_the_first_one_routes_it(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un)
    g1 = eng.cfg.groups[0]
    g2 = GroupCfg(name="g2", order=[{"tunnel": "zzz-last-resort"}], devices=[DevRef(mac=TV)], manage_routing=True)
    eng.cfg = eng.cfg.model_copy(update={"groups": [g1, g2]})
    run(eng, clock, 3)
    assert [r.description for r in own(un) if r.target_macs] == ["vpnwd: g1 › devices"]
    status = {g["name"]: g for g in eng.status()["map"]["groups"] if not g.get("unmanaged")}
    assert any("already in group g1" in x for x in status["g2"]["gaps"])


def test_a_policy_of_yours_above_a_picked_device_is_a_blocker(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, vlans=False)
    un.routes.insert(0, Route("mine", "my old tv policy", "wan", True, False, frozenset(), frozenset({TV}), {"description": "my old tv policy"}))
    run(eng, clock, 3)
    b = eng.status()["map"]["groups"][0]["routing"]["blockers"]
    assert [x["id"] for x in b] == ["mine"] and b[0]["vlans"] == ["livingTv"]
    un.routes.insert(0, Route("lan", "my vlan policy", "wan", True, False, frozenset({"net-iot"}), frozenset(), {"description": "my vlan policy"}))     # the TV's own VLAN
    run(eng, clock, 1)
    assert {x["id"] for x in eng.status()["map"]["groups"][0]["routing"]["blockers"]} == {"mine", "lan"}


def test_the_status_lists_the_devices_of_the_group_and_not_as_an_own_route(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, vlans=False)
    run(eng, clock, 3)
    m = eng.status()["map"]
    g = m["groups"][0]
    assert g["device_net"]["name"] == "g1 · devices" and [d["name"] for d in g["device_net"]["devices"]] == ["livingTv"] and g["device_net"]["devices"][0]["online"]
    assert m["own"] == [] and m["own_off"] == []                                # the watchdog's device policy is not "a device with its own route"


def test_the_overlap_with_a_vlan_of_another_group_is_explained(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, vlans=False)
    other = GroupCfg(name="g2", order=[{"tunnel": "zzz-last-resort"}], networks=[{"name": "vlan20-iot"}])
    eng.cfg = eng.cfg.model_copy(update={"groups": [eng.cfg.groups[0], other]})
    run(eng, clock, 3)
    g1 = next(g for g in eng.status()["map"]["groups"] if g["name"] == "g1")
    assert any("livingTv is in vlan20-iot, which group g2 routes" in x for x in g1["gaps"])


def test_a_group_with_devices_but_routing_off_says_so_and_changes_nothing(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, vlans=False, routing=False)
    run(eng, clock, 3)
    assert own(un) == []
    assert any("Routing is off" in x for x in eng.status()["map"]["groups"][0]["gaps"])


def test_removing_the_devices_or_the_group_removes_the_device_policy(make_engine):
    eng, un, _, _, clock = make_engine()
    setup(eng, un, vlans=False)
    run(eng, clock, 3)
    assert len(own(un)) == 1
    setup(eng, un, devices=(), vlans=False)
    run(eng, clock, 2)
    assert own(un) == []


def test_the_settings_form_round_trips_the_devices_and_lists_the_known_clients(make_engine):
    from vpn_watchdog import settings
    eng, un, *_ = make_engine()
    setup(eng, un, devices=(TV,), vlans=False)
    form = settings.extract(eng.cfg)
    assert form["groups"][0]["devices"] == [{"mac": TV, "name": ""}]
    form["groups"][0]["devices"].append({"mac": "AA:BB:CC:DD:EE:02", "name": "cam"})
    raw = {"unifi": {"api_key": "x"}, "groups": [{"name": "g1", "order": ["Home-Primary"]}]}
    out = settings.apply(raw, form)["groups"][0]["devices"]
    assert out == [{"mac": TV}, {"mac": CAM, "name": "cam"}]
    m = settings.meta(un.snapshot())
    assert m["clients"][0]["mac"] == TV and m["clients"][0]["name"] == "livingTv" and m["clients"][0]["online"]
