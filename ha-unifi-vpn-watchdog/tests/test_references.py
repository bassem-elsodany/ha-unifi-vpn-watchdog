"""Anything in the config that points at UniFi points at its id: renaming a VPN client or a VLAN in UniFi changes nothing."""
from __future__ import annotations

import dataclasses

import yaml

from conftest import active_name, run, tid
from vpn_watchdog.app import App
from vpn_watchdog.config import parse_config
from vpn_watchdog.models import Tunnel
from vpn_watchdog.refs import normalize


def rename_tunnel(un, old, new):
    orig = un.snapshot

    def snap():
        sn = orig()
        t = sn.tunnels[tid(old)]
        sn.tunnels[tid(old)] = dataclasses.replace(t, name=new)
        return sn
    un.snapshot = snap


def test_names_only_config_gets_ids_on_the_first_reading(make_engine):
    eng, un, *_ = make_engine()
    assert all(i.id is None for i in eng.cfg.groups[0].order) and all(n.id is None for n in eng.cfg.groups[0].networks)
    eng.tick()
    g = eng.cfg.groups[0]
    assert [i.id for i in g.order] == [tid(n) for n in ("Home-Primary", "Home-Backup", "Cousin vpn 2", "office/berlin", "office/frankfurt", "zzz-last-resort")]
    assert [n.id for n in g.networks] == ["net-iot", "net-vpn"] and eng.refs_changed


def test_renaming_a_vpn_client_in_unifi_keeps_its_place_in_the_order_and_its_settings(make_engine):
    eng, un, tester, notes, clock = make_engine()
    run(eng, clock, 1)                                       # ids are learned
    rename_tunnel(un, "Home-Backup", "Totally different name")
    run(eng, clock, 1)
    g = eng.cfg.groups[0]
    assert g.order[1].id == tid("Home-Backup") and g.order[1].tunnel == "Totally different name"       # label follows UniFi
    st = eng.status()
    lane = {t["name"]: t for t in st["map"]["groups"][0]["lane"]}
    assert lane["Totally different name"]["position"] == "#2" or lane["Totally different name"]["position"] == 2
    # and failover still goes to it as #2
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 6)
    assert un.active_route().network_id == tid("Home-Backup")        # the policy of that very client (policy names are UniFi's own)


def test_a_country_typed_for_a_position_survives_a_rename(make_engine):
    eng, un, tester, notes, clock = make_engine()
    run(eng, clock, 1)
    rename_tunnel(un, "office/berlin", "Berlin renamed")
    run(eng, clock, 1)
    t = un.snapshot().tunnels[tid("office/berlin")]
    from vpn_watchdog.ladder import expected_country
    assert expected_country(eng.cfg.groups[0], t) == "DE"


def test_a_deleted_and_recreated_client_is_found_again_by_its_name(make_engine):
    eng, un, *_ = make_engine()
    eng.tick()
    g = eng.cfg.groups[0]
    snap = un.snapshot()
    snap.tunnels = {("new-id" if i == tid("Home-Backup") else i): (dataclasses.replace(t, id="new-id") if i == tid("Home-Backup") else t) for i, t in snap.tunnels.items()}
    assert normalize(eng.cfg, snap)
    assert g.order[1].id == "new-id" and g.order[1].tunnel == "Home-Backup"


def test_a_deleted_client_stays_in_the_config_as_missing_and_is_skipped(make_engine):
    eng, un, *_ = make_engine()
    eng.tick()
    snap = un.snapshot()
    del snap.tunnels[tid("Home-Backup")]
    assert not normalize(eng.cfg, snap)                     # nothing to follow: the entry is kept, not dropped or guessed
    from vpn_watchdog.ladder import missing, resolve_order
    assert missing(eng.cfg.groups[0], list(snap.tunnels.values())) == ["Home-Backup"]
    assert "Home-Backup" not in [t.name for t in resolve_order(eng.cfg.groups[0], list(snap.tunnels.values()))]


def test_renaming_a_vlan_in_unifi_keeps_the_group_working(make_engine):
    eng, un, tester, notes, clock = make_engine()
    run(eng, clock, 1)
    orig = un.snapshot

    def snap():
        sn = orig()
        sn.networks["net-iot"] = "IoT renamed"
        sn.network_info["net-iot"] = {**sn.network_info["net-iot"], "name": "IoT renamed"}
        return sn
    un.snapshot = snap
    run(eng, clock, 1)
    g = eng.status()["map"]["groups"][0]
    assert not g.get("unmanaged") and [n["name"] for n in g["networks"]] == ["IoT renamed", "vlan50-vpn"]
    assert eng.cfg.groups[0].networks[0].name == "IoT renamed"


def test_the_config_file_gets_the_ids_written_once(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\ngroups:\n  - {name: g, networks: [vlan20-iot], order: [A, B]}\n")
    a = App(str(f), env={})
    from vpn_watchdog.models import Snapshot
    snap = Snapshot({"ta": Tunnel("ta", "A", True), "tb": Tunnel("tb", "B", True)}, {}, [], {"n1": "vlan20-iot"})
    assert normalize(a.cfg, snap)
    a.engine.cfg = a.cfg
    a.engine.refs_changed = True
    a.persist_refs()
    g = parse_config(f.read_text(), {}).groups[0]
    assert [(i.id, i.tunnel) for i in g.order] == [("ta", "A"), ("tb", "B")] and (g.networks[0].id, g.networks[0].name) == ("n1", "vlan20-iot")
    first = f.read_text()
    a.persist_refs()
    assert f.read_text() == first                           # nothing more to write


def test_status_follows_unifi_when_a_vlan_is_routed_elsewhere_than_its_group(make_engine):
    """The group lists both VLANs, but UniFi sends vlan20 through another client: the page shows what UniFi does."""
    eng, un, *_ = make_engine()
    run(eng, clock := eng.clock, 1)
    un.routes.insert(0, dataclasses.replace(un.routes[1], id="split", description="elsewhere", network_id=tid("zzz-last-resort"), enabled=True,
                                            target_networks=frozenset({"net-iot"})))
    run(eng, clock, 1)
    blocks = eng.status()["map"]["groups"]
    by = {b["name"]: b for b in blocks}
    assert by["g1"]["active"] == "Home-Primary" and [n["name"] for n in by["g1"]["networks"]] == ["vlan50-vpn"]
    other = next(b for b in blocks if b is not by["g1"])
    assert other["active"] == "zzz-last-resort" and [n["name"] for n in other["networks"]] == ["vlan20-iot"]


def test_a_policy_for_all_devices_covers_every_vlan(make_engine):
    eng, un, *_ = make_engine()
    eng.cfg = eng.cfg.model_copy(update={"groups": []})
    un.routes = [dataclasses.replace(r, target_networks=frozenset(), all_clients=True) if r.description == "Home-Primary" else r for r in un.routes]
    eng.tick()
    blocks = eng.status()["map"]["groups"]
    assert len(blocks) == 1 and blocks[0]["active"] == "Home-Primary" and {n["name"] for n in blocks[0]["networks"]} == {"vlan20-iot", "vlan50-vpn"}


def test_devices_are_placed_by_vlan_id_not_by_vlan_name(make_engine):
    eng, un, *_ = make_engine()
    eng.cfg = eng.cfg.model_copy(update={"groups": []})
    orig = un.snapshot

    def snap():
        sn = orig()
        sn.clients = {"aa:aa": {"name": "plug", "ip": "10.0.20.9", "network": "an old name", "network_id": "net-iot", "rate_bps": 10, "wired": False}}
        return sn
    un.snapshot = snap
    eng.tick()
    g = eng.status()["map"]["groups"][0]
    iot = next(n for n in g["networks"] if n["id"] == "net-iot")
    assert [d["name"] for d in iot["devices"]] == ["plug"]


def test_saving_the_settings_form_stores_ids_and_a_second_save_changes_nothing(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\ngroups:\n  - {name: g, networks: [vlan20-iot], order: [A]}\n")
    a = App(str(f), env={})
    form = {"groups": [{"name": "g", "_orig": "g", "networks": [{"id": "n1", "name": "vlan20-iot"}], "kill_switch": None,
                        "order": [{"tunnel": "A", "id": "ta", "expect_country": "de"}, {"tunnel": "B", "id": "tb", "expect_country": ""}]}]}
    assert a.save_settings(form) is None
    g = parse_config(f.read_text(), {}).groups[0]
    assert [(i.id, i.tunnel, i.expect_country) for i in g.order] == [("ta", "A", "DE"), ("tb", "B", None)] and g.networks[0].id == "n1"
    assert yaml.safe_load(f.read_text())["groups"][0]["networks"] == [{"id": "n1", "name": "vlan20-iot"}]
