import threading
import time
import urllib.request
import json

from conftest import make_engine  # noqa: F401  (fixture)
from conftest import run, tid
from vpn_watchdog.safe_mode import wait_until_valid

GOOD = "unifi: {api_key: k}\ngroups:\n  - {name: g, networks: [n], order: [T1]}\n"
OLD = "unifi: {api_key: k}\ngroups:\n  - {name: g, networks: [n], ladder: [{country: IT}]}\n"


def test_status_lists_routing_policies_per_tunnel(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 1)
    st = eng.status()
    primary = st["tunnels"]["Home-Primary"]["routes"]
    assert len(primary) == 1
    r = primary[0]
    assert r["enabled"] and r["managed_by"] == "g1" and r["networks"] == ["vlan20-iot", "vlan50-vpn"] and r["kill_switch"] is False
    backup = st["tunnels"]["Home-Backup"]["routes"]
    assert len(backup) == 1 and backup[0]["enabled"] is False and backup[0]["managed_by"] == "g1"   # its own policy, switched off
    assert st["groups"]["g1"]["position"] == "#1" and "country" not in st["groups"]["g1"]


def test_routes_to_non_tunnels_are_shown_as_direct(make_engine):
    from vpn_watchdog.models import Route
    eng, un, _, _, clock = make_engine()
    un.routes.append(Route("r-direct", "Cameras direct", "net-wan", True, False, frozenset(), frozenset({"ec:62:60:c9:64:88"}), {}))
    run(eng, clock, 1)
    d = eng.status()["direct_routes"]
    assert [x["description"] for x in d] == ["Cameras direct"] and d[0]["clients"] == ["ec:62:60:c9:64:88"]


def test_safe_mode_serves_the_ui_and_starts_normally_once_the_file_is_fixed(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text(OLD)
    stop = threading.Event()
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("ok", wait_until_valid(str(f), {"WATCHDOG_CONTROL_TOKEN": "tok"}, "old config", stop)))
    # the server binds the default port 8080; use a free port through the file's server section instead
    f.write_text(OLD + "server: {port: 18765, host: 127.0.0.1, control_token: tok}\n")
    t.start()
    time.sleep(1.2)
    status = json.loads(urllib.request.urlopen("http://127.0.0.1:18765/api/status", timeout=3).read())
    assert status["safe_mode"] is True and "old config" in status["error"] and status["groups"] == {}
    req = urllib.request.Request("http://127.0.0.1:18765/api/config", headers={"Authorization": "Bearer tok"})
    assert "ladder" in json.loads(urllib.request.urlopen(req, timeout=3).read())["yaml"]       # editor works
    f.write_text(GOOD)                                                                              # user fixes the file
    t.join(timeout=10)
    assert result.get("ok") is True


def test_status_map_has_devices_the_fallback_lane_the_pool_and_direct_vlans(make_engine):
    from vpn_watchdog.models import Route
    eng, un, _, _, clock = make_engine()
    orig = un.snapshot

    def snap_with_devices():
        s = orig()
        s.networks = {**s.networks, "net-cam": "vlan30-cameras"}
        s.network_info = {"net-iot": {"name": "vlan20-iot", "vlan": 20, "subnet": "10.0.20.1/24", "purpose": "corporate"}, "net-vpn": {"name": "vlan50-vpn", "vlan": 50, "subnet": "10.0.50.1/24", "purpose": "corporate"},
                          "net-cam": {"name": "vlan30-cameras", "vlan": 30, "subnet": "10.0.30.1/24", "purpose": "corporate"}}
        s.clients = {"aa:aa": {"name": "TV", "ip": "10.0.50.5", "network": "vlan50-vpn", "rate_bps": 1_400_000, "wired": True},
                     "bb:bb": {"name": "Meter", "ip": "10.0.20.9", "network": "vlan20-iot", "rate_bps": 9_000, "wired": False},
                     "cc:cc": {"name": "Phone", "ip": "10.0.20.10", "network": "vlan20-iot", "rate_bps": 50_000, "wired": False},
                     "dd:dd": {"name": "Idle plug", "ip": "10.0.20.11", "network": "vlan20-iot", "rate_bps": 0, "wired": False},
                     "ee:ee": {"name": "Cam", "ip": "10.0.30.2", "network": "vlan30-cameras", "rate_bps": 3_000, "wired": True}}
        return s

    un.snapshot = snap_with_devices
    un.routes.append(Route("r-direct", "Meter direct", "net-wan", True, False, frozenset(), frozenset({"bb:bb"}), {}))
    run(eng, clock, 1)
    m = eng.status()["map"]
    g = m["groups"][0]
    assert g["name"] == "g1" and g["active"] == "Home-Primary" and g["has_order"]
    iot = next(n for n in g["networks"] if n["name"] == "vlan20-iot")
    assert (iot["vlan"], iot["subnet"], iot["count"], iot["bypass_count"]) == (20, "10.0.20.1/24", 3, 1)
    assert [d["name"] for d in iot["devices"]] == ["Meter", "Phone", "Idle plug"]               # bypassing first, then busiest, then idle
    assert iot["devices"][0]["bypass"] == {"goes_to": "the normal internet connection", "policy": "Meter direct", "kind": "normal", "tunnel": None}
    assert [d["active"] for d in iot["devices"]] == [True, True, False]
    assert [t["name"] for t in g["lane"]] == ["Home-Primary", "Home-Backup", "Cousin vpn 2", "office/berlin", "office/frankfurt", "zzz-last-resort"]
    assert [t["position"] for t in g["lane"]] == [1, 2, 3, 4, 5, 6] and [t["active"] for t in g["lane"]] == [True] + [False] * 5
    assert g["lane"][3]["expect_country"] == "DE" and g["lane"][0]["policy_on"] and not g["lane"][1]["policy_on"]
    assert g["pool"] == [] and g["exit"]["server"] == "9.9.9.9"
    assert [n["name"] for n in m["direct"]] == ["vlan30-cameras"] and m["direct"][0]["count"] == 1 and m["wan_ip"] == "92.0.0.1"


def test_tunnels_outside_the_order_are_in_the_pool(make_engine):
    eng, un, _, _, clock = make_engine(order="[Home-Primary, Home-Backup]")
    run(eng, clock, 1)
    g = eng.status()["map"]["groups"][0]
    assert [t["name"] for t in g["lane"]] == ["Home-Primary", "Home-Backup"]
    assert sorted(p["name"] for p in g["pool"]) == ["Cousin vpn 2", "office/berlin", "office/frankfurt", "zzz-last-resort"]
