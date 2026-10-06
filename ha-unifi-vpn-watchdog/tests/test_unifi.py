import json

import httpx
import pytest

from vpn_watchdog.config import UnifiCfg
from vpn_watchdog.unifi import UniFiClient, UniFiError

SECRET_KEY = "PRIVATE-KEY-DO-NOT-LEAK"
API_KEY = "SECRETAPIKEY123"
NETCONF = [
    {"_id": "t1", "name": "My Tunnel #1", "purpose": "vpn-client", "enabled": True, "ip_subnet": "10.5.22.2/24",
     "wireguard_client_configuration_file": f"[Interface]\nPrivateKey={SECRET_KEY}\n"},
    {"_id": "n20", "name": "vlan20-iot", "purpose": "corporate"},
]
ROUTES = [{"_id": "r1", "description": "My Tunnel #1", "network_id": "t1", "enabled": True,
           "kill_switch_enabled": False, "target_devices": [{"network_id": "n20", "type": "NETWORK"}, {"client_mac": "AA:BB", "type": "CLIENT"}]}]


def make(fail=None):
    sent = []

    def handler(req: httpx.Request) -> httpx.Response:
        sent.append((req.method, req.url.path, json.loads(req.content) if req.content else None, req.headers.get("x-api-key")))
        if fail:
            return httpx.Response(fail)
        p = req.url.path
        if p.endswith("/rest/networkconf") and req.method == "GET":
            return httpx.Response(200, json={"data": NETCONF})
        if "/rest/networkconf/" in p and req.method == "GET":
            return httpx.Response(200, json={"data": [dict(NETCONF[0], enabled=False)]})
        if p.endswith("/vpn/connections"):
            return httpx.Response(200, json={"connections": [{"network_id": "t1", "status": "CONNECTED", "remote_ip": "1.2.3.4", "rx_rate_bps": 10, "tx_rate_bps": 20}]})
        if p.endswith("/trafficroutes"):
            return httpx.Response(200, json=ROUTES)
        if p.endswith("/stat/sta"):
            return httpx.Response(200, json={"data": [{"mac": "AA:BB", "name": "TV", "ip": "10.0.50.5", "network": "vlan50", "tx_bytes-r": 1000, "rx_bytes-r": 24000, "is_wired": True},
                                                      {"mac": "CC:DD", "hostname": "phone", "ip": "10.0.50.6", "network": "vlan50"}]})
        if p.endswith("/stat/health"):
            return httpx.Response(200, json={"data": [{"subsystem": "wan", "wan_ip": "92.0.0.1"}]})
        return httpx.Response(200, json={})

    c = UniFiClient(UnifiCfg(api_key=API_KEY, retries=0), transport=httpx.MockTransport(handler))
    return c, sent


def test_snapshot_parses_and_never_keeps_the_private_key():
    c, sent = make()
    snap = c.snapshot()
    t = snap.tunnels["t1"]
    assert (t.name, t.enabled, t.subnet) == ("My Tunnel #1", True, "10.5.22.2/24")
    assert SECRET_KEY not in repr(snap)
    assert snap.connections["t1"].connected and snap.wan_ip == "92.0.0.1"
    r = snap.routes[0]
    assert r.target_networks == {"n20"} and r.target_macs == {"aa:bb"}
    assert all(h == API_KEY for *_, h in sent)
    assert API_KEY not in repr(snap)


def test_set_route_puts_full_object_with_only_the_changes():
    c, sent = make()
    r = c.snapshot().routes[0]
    c.set_route(r, network_id="t2", description="DE__X__1__1.1.1.1")
    method, path, body, _ = sent[-1]
    assert (method, path.endswith("/trafficroutes/r1")) == ("PUT", True)
    assert body["network_id"] == "t2" and body["description"] == "DE__X__1__1.1.1.1"
    assert body["target_devices"] == ROUTES[0]["target_devices"] and body["_id"] == "r1"


def test_set_route_without_changes_sends_nothing():
    c, sent = make()
    r = c.snapshot().routes[0]
    n = len(sent)
    c.set_route(r, network_id="t1")
    assert len(sent) == n


def test_set_tunnel_enabled_does_get_then_put():
    c, sent = make()
    c.set_tunnel_enabled("t1", True)
    assert [m for m, *_ in sent[-2:]] == ["GET", "PUT"]
    assert sent[-1][2]["enabled"] is True


def test_http_errors_raise_without_leaking_the_key():
    c, _ = make(fail=403)
    with pytest.raises(UniFiError) as e:
        c.snapshot()
    assert API_KEY not in str(e.value)
    c2, _ = make(fail=503)
    with pytest.raises(UniFiError):
        c2.snapshot()


def test_snapshot_reads_devices_with_a_bit_rate():
    c, _ = make()
    cl = c.snapshot().clients
    assert cl["aa:bb"] == {"name": "TV", "ip": "10.0.50.5", "network": "vlan50", "rate_bps": 200_000, "wired": True}    # (1000+24000) bytes/s * 8
    assert cl["cc:dd"]["name"] == "phone" and cl["cc:dd"]["rate_bps"] is None
