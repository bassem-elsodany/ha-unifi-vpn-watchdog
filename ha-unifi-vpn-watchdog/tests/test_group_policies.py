"""A group owns the policies that target exactly its VLANs: groups on different VLANs never share or disturb each other's policies."""
from __future__ import annotations

import pytest

from conftest import run, tid, TUNNELS
from vpn_watchdog.config import ConfigError, GroupCfg, parse_config
from vpn_watchdog.models import Route


def two_groups(eng, un, on20="Home-Primary", on50="Home-Backup"):
    eng.cfg = eng.cfg.model_copy(update={"groups": [
        GroupCfg(name="g20", networks=["vlan20-iot"], order=["Home-Primary", "Home-Backup", "Cousin vpn 2"]),
        GroupCfg(name="g50", networks=["vlan50-vpn"], order=["Home-Backup", "Home-Primary", "Cousin vpn 2"])]})
    un.routes = []
    for n in TUNNELS:
        un.routes.append(Route(f"r20-{n}", f"{n} 20", tid(n), n == on20, False, frozenset({"net-iot"}), frozenset(), {}))
        un.routes.append(Route(f"r50-{n}", f"{n} 50", tid(n), n == on50, False, frozenset({"net-vpn"}), frozenset(), {}))
    un.enabled = {tid(on20), tid(on50)}


def on(un, vlan):
    return sorted(r.description.rsplit(" ", 1)[0] for r in un.routes if r.enabled and r.id.startswith(f"r{vlan}-"))


def test_failover_of_one_group_leaves_the_other_groups_vlan_alone(make_engine):
    eng, un, tester, notes, clock = make_engine()
    two_groups(eng, un)
    run(eng, clock, 2)
    assert on(un, 20) == ["Home-Primary"] and on(un, 50) == ["Home-Backup"]
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 8)
    assert on(un, 20) == ["Home-Backup"]                      # g20 moved to its #2 ...
    assert on(un, 50) == ["Home-Backup"]                      # ... and g50, on the same client, was not touched
    un.dead.add(tid("Home-Backup"))
    run(eng, clock, 12)
    assert len(on(un, 50)) == 1 and len(on(un, 20)) == 1      # each group still has exactly one policy on


def test_policies_that_cover_more_than_the_groups_vlans_are_not_the_groups(make_engine):
    eng, un, *_ = make_engine()
    two_groups(eng, un)
    un.routes.append(Route("both", "covers both", tid("Cousin vpn 2"), False, False, frozenset({"net-iot", "net-vpn"}), frozenset(), {}))
    routes = {g.name: [r.id for r in eng._group_routes(g, un.snapshot())] for g in eng.cfg.groups}
    assert "both" not in routes["g20"] + routes["g50"]
    assert all(i.startswith("r20-") for i in routes["g20"]) and all(i.startswith("r50-") for i in routes["g50"])


def test_a_missing_policy_is_created_for_exactly_the_groups_vlans(make_engine):
    eng, un, *_ = make_engine(with_policies=False, enabled=set())
    eng.cfg = eng.cfg.model_copy(update={"groups": [GroupCfg(name="g20", networks=["vlan20-iot"], order=["Home-Primary", "Home-Backup"])]})
    eng.tick()
    made = [r for r in un.routes if r.id.startswith("new-")]
    assert len(made) == 1 and made[0].target_networks == frozenset({"net-iot"}) and made[0].network_id == tid("Home-Primary") and made[0].enabled
    assert made[0].description == "Home-Primary (g20)"


def test_another_enabled_policy_above_the_groups_is_reported_not_changed(make_engine):
    eng, un, *_ = make_engine()
    two_groups(eng, un)
    un.routes.insert(0, Route("old", "an older policy", tid("Cousin vpn 2"), True, False, frozenset({"net-iot", "net-vpn"}), frozenset(), {}))
    run(eng, clock := eng.clock, 2)
    g = {x["name"]: x for x in eng.status()["map"]["groups"]}
    assert "an older policy" in g["g20"]["conflict"] and "an older policy" in g["g50"]["conflict"]
    assert next(r for r in un.routes if r.id == "old").enabled          # never touched
    un.routes = [r for r in un.routes if r.id != "old"]
    run(eng, clock, 2)
    assert all(x["conflict"] is None for x in eng.status()["map"]["groups"] if not x.get("unmanaged"))


def test_a_vlan_cannot_be_in_two_groups():
    base = "interval_seconds: 15\nstate_file: /tmp/x\nunifi: {api_key: x}\n"
    with pytest.raises(ConfigError, match="two groups"):
        parse_config(base + "groups:\n  - {name: a, networks: [v20, v50]}\n  - {name: b, networks: [v50]}\n", env={})
    parse_config(base + "groups:\n  - {name: a, networks: [v20]}\n  - {name: b, networks: [v50]}\n", env={})
