import json

import httpx

from conftest import FakeClock
from vpn_watchdog.config import NotifyCfg, ProbeCfg, ProbeEndpoint
from vpn_watchdog.notify import Notifier
from vpn_watchdog.probe import Prober, parse_endpoint
from vpn_watchdog.state import StateStore


def prober(responses: dict[str, tuple[int, str]], **kw):
    def handler(req):
        code, body = responses.get(str(req.url), (500, ""))
        return httpx.Response(code, text=body)
    eps = [ProbeEndpoint(name="a", url="http://a/json"),
           ProbeEndpoint(name="cf", url="http://cf/trace", format="cloudflare_trace")]
    return Prober(ProbeCfg(endpoints=eps, **kw), transport=httpx.MockTransport(handler))


A = lambda ip, c: (200, json.dumps({"ip": ip, "country": c}))
CF = lambda ip, c: (200, f"fl=1\nip={ip}\nloc={c}\n")


def test_parsers():
    ep = ProbeEndpoint(name="x", url="u", ip_field="data.ip", country_field="data.cc")
    assert parse_endpoint(ep, '{"data":{"ip":"1.1.1.1","cc":"IT"}}') == ("1.1.1.1", "IT")
    assert parse_endpoint(ProbeEndpoint(name="c", url="u", format="cloudflare_trace"), "ip=2.2.2.2\nloc=DE\n") == ("2.2.2.2", "DE")


def test_probe_ok_and_country_alias():
    p = prober({"http://a/json": A("5.5.5.5", "UK")})
    r = p.probe("GB", "92.0.0.1")
    assert r.ok and r.ip == "5.5.5.5"


def test_probe_detects_wrong_country():
    p = prober({"http://a/json": A("5.5.5.5", "AE"), "http://cf/trace": CF("5.5.5.5", "AE")})
    r = p.probe("IT", "92.0.0.1")
    assert not r.ok and "country AE != IT" in r.reason


def test_probe_detects_leak():
    p = prober({"http://a/json": A("92.0.0.1", "AE"), "http://cf/trace": CF("92.0.0.1", "AE")})
    r = p.probe("IT", "92.0.0.1")
    assert not r.ok and r.leak


def test_probe_falls_through_dead_endpoint_and_respects_require():
    resp = {"http://cf/trace": CF("5.5.5.5", "IT")}            # first endpoint 500s
    assert prober(resp).probe("IT", None).ok
    assert not prober(resp, require=2).probe("IT", None).ok


def test_notifier_filters_events_and_throttles():
    sent = []

    def handler(req):
        sent.append(json.loads(req.content)); return httpx.Response(200)

    clock = FakeClock()
    n = Notifier([NotifyCfg(type="webhook", url="http://h", events=["switch"], throttle_seconds=100)], clock,
                 transport=httpx.MockTransport(handler))
    n.emit("startup", "t", "m")           # filtered out
    n.emit("switch", "t", "m", key="k")
    n.emit("switch", "t", "m", key="k")   # throttled
    clock.t += 101
    n.emit("switch", "t", "m", key="k")
    assert len(sent) == 2 and sent[0]["event"] == "switch"
    assert len(n.history) == 4            # history keeps everything for the UI


def test_notifier_failure_never_raises_and_ntfy_headers():
    seen = {}

    def handler(req):
        seen.update(req.headers); return httpx.Response(500)

    n = Notifier([NotifyCfg(type="ntfy", url="http://n", topic="t", token="tok")], FakeClock(), transport=httpx.MockTransport(handler))
    n.emit("exhausted", "T", "body", level="critical")
    assert seen["authorization"] == "Bearer tok" and seen["priority"] == "urgent"


def test_state_roundtrip_and_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    s = StateStore(p)
    s.tunnel("t1").quarantined_until = 5.0
    s.group("g").paused = True
    s.touch(); s.save()
    s2 = StateStore(p)
    assert s2.tunnel("t1").quarantined_until == 5.0 and s2.group("g").paused
    p.write_text("{not json")
    assert StateStore(p).groups == {}


def test_check_country_can_be_disabled_for_unreliable_geo_ip():
    resp = {"http://a/json": A("5.5.5.5", "BR"), "http://cf/trace": CF("5.5.5.5", "BR")}
    assert not prober(resp).probe("IT", None).ok
    assert prober(resp, check_country=False).probe("IT", None).ok
