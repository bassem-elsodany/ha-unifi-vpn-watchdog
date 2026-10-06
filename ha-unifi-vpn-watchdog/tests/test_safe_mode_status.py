import threading
import time
import urllib.request
import json

from conftest import make_engine  # noqa: F401  (fixture)
from conftest import run, tid
from vpn_watchdog.safe_mode import wait_until_valid

GOOD = "unifi: {api_key: k}\ngroups:\n  - {name: g, networks: [n], ladder: [{tunnels: ['*']}]}\n"
OLD = "unifi: {api_key: k}\ngroups:\n  - {name: g, networks: [n], ladder: [{country: IT}]}\n"


def test_status_lists_routing_policies_per_tunnel(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 1)
    st = eng.status()
    primary = st["tunnels"]["Home-Primary"]["routes"]
    assert len(primary) == 1
    r = primary[0]
    assert r["enabled"] and r["managed_by"] == "g1" and r["networks"] == ["vlan20-iot", "vlan50-vpn"] and r["kill_switch"] is False
    assert st["tunnels"]["Home-Backup"]["routes"] == []                    # no policy: carries nothing
    assert st["groups"]["g1"]["step"] == "Home" and "country" not in st["groups"]["g1"]


def test_routes_to_non_tunnels_are_shown_as_direct(make_engine):
    from dataclasses import replace
    eng, un, _, _, clock = make_engine()
    un.extra_routes.append(replace(un.route, id="r-direct", description="Cameras direct", network_id="net-wan",
                                   target_networks=frozenset(), target_macs=frozenset({"ec:62:60:c9:64:88"})))
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
    assert "country: IT" in json.loads(urllib.request.urlopen(req, timeout=3).read())["yaml"]       # editor works
    f.write_text(GOOD)                                                                              # user fixes the file
    t.join(timeout=10)
    assert result.get("ok") is True
