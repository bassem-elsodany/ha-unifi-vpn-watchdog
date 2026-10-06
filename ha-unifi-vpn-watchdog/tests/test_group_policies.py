"""The watchdog's only write to UniFi is a VPN client's switch (on / off). Routing policies are UniFi's business."""
from __future__ import annotations

import pytest

from conftest import active_name, run, tid, TUNNELS
from vpn_watchdog.config import GroupCfg, parse_config


def test_it_never_writes_a_routing_policy(make_engine):
    eng, un, tester, notes, clock = make_engine()
    assert not hasattr(un, "set_route") and not hasattr(un, "create_route")
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 6)
    un.dead.discard(tid("Home-Primary"))
    run(eng, clock, 12)
    assert un.calls and all(c[0] == "enable" for c in un.calls)


def test_the_real_client_has_no_way_to_write_a_routing_policy():
    from vpn_watchdog.unifi import UniFiClient
    assert not hasattr(UniFiClient, "set_route") and not hasattr(UniFiClient, "create_route")
    import inspect, vpn_watchdog.unifi as u
    src = inspect.getsource(u)
    assert "trafficroutes" in src and not any(f'"{m}", self._v2("trafficroutes' in src for m in ("PUT", "POST", "DELETE"))


def test_a_client_that_is_in_two_groups_stays_with_the_first(make_engine):
    eng, un, *_ = make_engine()
    eng.cfg = eng.cfg.model_copy(update={"groups": [
        GroupCfg(name="a", order=["Home-Primary", "Home-Backup"]),
        GroupCfg(name="b", order=["Home-Backup", "Cousin vpn 2"])]})
    eng.tick()
    snap = un.snapshot()
    assert [t.name for t in eng._pool(eng.cfg.groups[0], snap)] == ["Home-Primary", "Home-Backup"]
    assert [t.name for t in eng._pool(eng.cfg.groups[1], snap)] == ["Cousin vpn 2"]


def test_saving_refuses_a_client_that_is_in_two_groups(tmp_path):
    from vpn_watchdog.app import App
    f = tmp_path / "c.yaml"
    f.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\ngroups:\n  - {name: a, order: [X, Y]}\n")
    a = App(str(f), env={})
    err = a.validate_text("unifi: {api_key: k}\ngroups:\n  - {name: a, order: [X, Y]}\n  - {name: b, order: [Y, Z]}\n")
    assert err and "'Y'" in err and "one group only" in err
    assert a.validate_text("unifi: {api_key: k}\ngroups:\n  - {name: a, order: [X]}\n  - {name: b, order: [Y]}\n") is None


def test_old_configs_with_vlans_or_a_kill_switch_still_load_and_are_ignored():
    cfg = parse_config("unifi: {api_key: k}\ngroups:\n  - {name: a, networks: [vlan20], kill_switch: false, order: [X]}\n", env={})
    assert cfg.groups[0].order[0].tunnel == "X"


def test_modes_that_steered_a_device_through_a_policy_are_refused_with_a_reason():
    from vpn_watchdog.config import ConfigError
    for mode in ("canary", "remote"):
        with pytest.raises(ConfigError, match="routing policy"):
            parse_config(f"unifi: {{api_key: k}}\nprobe: {{mode: {mode}}}\ngroups: []\n", env={})
    with pytest.raises(ConfigError, match="routing policy"):
        parse_config("unifi: {api_key: k}\nswitching: {on_exhausted: kill_switch}\ngroups: []\n", env={})


def test_status_ignores_a_routing_policy_whose_vpn_client_is_switched_off(make_engine):
    """UniFi skips a policy whose client is off, so the next policy that covers the VLAN is the one that applies."""
    import dataclasses
    eng, un, *_ = make_engine()
    eng.cfg = eng.cfg.model_copy(update={"groups": []})
    un.routes = [dataclasses.replace(r, enabled=True) for r in un.routes]            # every policy is switched on, as the user keeps them
    un.enabled = {tid("Home-Backup")}                                                # but only one client is on
    eng.tick()
    blocks = eng.status()["map"]["groups"]
    assert len(blocks) == 1 and blocks[0]["active"] == "Home-Backup" and {n["name"] for n in blocks[0]["networks"]} == {"vlan20-iot", "vlan50-vpn"}


def test_a_group_shows_even_when_no_vlan_uses_its_client(make_engine):
    import dataclasses
    eng, un, *_ = make_engine()
    un.routes = [dataclasses.replace(r, enabled=False) for r in un.routes]
    eng.tick()
    g = eng.status()["map"]["groups"][0]
    assert g["name"] == "g1" and g["active"] == "Home-Primary" and g["networks"] and "No routing policy" in g["conflict"]


def test_a_client_without_a_policy_for_the_groups_vlan_is_flagged(make_engine):
    eng, un, *_ = make_engine(order="[Home-Primary, Home-Backup]")
    eng.tick()
    assert eng.status()["map"]["groups"][0]["gaps"] == []
    un.routes = [r for r in un.routes if r.network_id != tid("Home-Backup")]
    eng.tick()
    gaps = eng.status()["map"]["groups"][0]["gaps"]
    assert gaps and all(x.startswith("Home-Backup has no routing policy for") for x in gaps)


def test_a_picked_vlan_with_no_live_vpn_policy_is_flagged_and_stays_in_the_group(make_engine):
    import dataclasses
    eng, un, *_ = make_engine()
    un.routes = [dataclasses.replace(r, enabled=False) for r in un.routes]
    eng.tick()
    g = eng.status()["map"]["groups"][0]
    assert g["declared"] and {n["name"] for n in g["networks"]} == {"vlan20-iot", "vlan50-vpn"}
    assert any("vlan20-iot is not routed through any VPN client" in x for x in g["gaps"])
