import httpx
import pytest

from vpn_watchdog.app import App
from vpn_watchdog.config import parse_config
from vpn_watchdog.ha_api import HaApi, label

SERVICES = [
    {"domain": "notify", "services": {"mobile_app_bassems_pixel": {}, "notify": {}, "send_message": {}}},
    {"domain": "persistent_notification", "services": {"create": {}, "dismiss": {}}},
    {"domain": "light", "services": {"turn_on": {}}},
]


def api(handler):
    return HaApi("http://ha", "tok", transport=httpx.MockTransport(handler))


def test_lists_only_usable_notify_services_with_friendly_labels():
    got = api(lambda req: httpx.Response(200, json=SERVICES)).notify_services()
    assert [s["service"] for s in got] == ["notify.mobile_app_bassems_pixel", "notify.notify", "persistent_notification.create"]
    assert got[0]["label"] == "Phone: bassems pixel"
    assert label("persistent_notification.create").startswith("Persistent")


def test_call_posts_title_and_message_and_rejects_bad_names():
    seen = {}

    def handler(req):
        seen["path"], seen["auth"] = req.url.path, req.headers["authorization"]
        seen["body"] = req.content
        return httpx.Response(200, json=[])

    a = api(handler)
    a.call("notify.mobile_app_x", "T", "M")
    assert seen["path"] == "/api/services/notify/mobile_app_x" and seen["auth"] == "Bearer tok" and b'"title"' in seen["body"]
    with pytest.raises(ValueError):
        a.call("../etc/passwd", "T", "M")


CFG = ("unifi: {api_key: k}\nstate_file: %s\ngroups:\n  - {name: g, networks: [n], order: [T1]}\n"
       "notifications:\n  - {type: home_assistant, supervisor: true, service: persistent_notification.create}\n")


def test_ui_choice_overrides_config_and_survives_restart(tmp_path, monkeypatch):
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(CFG % (tmp_path / "s.json"))
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    a = App(str(cfgp), env={})
    assert a.current_notify_service() == "persistent_notification.create"
    assert a.set_notify_service("notify.mobile_app_pixel") is None
    a.reload_if_changed()                                    # picks up the forced reload
    assert a.current_notify_service() == "notify.mobile_app_pixel"
    b = App(str(cfgp), env={})                               # new process, same state volume
    assert b.current_notify_service() == "notify.mobile_app_pixel"
    assert a.set_notify_service("bad name;") == "invalid service name"


def test_standalone_without_ha_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\ngroups:\n  - {name: g, networks: [n], order: [T1]}\n")
    out = App(str(cfgp), env={}).ha_notify_services()
    assert out["available"] is False and out["services"] == []


def test_set_group_order_writes_the_sequence_and_keeps_expect_country(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\ngroups:\n  - name: g\n    networks: [n]\n"
                 "    order: [A, {tunnel: B, expect_country: DE}, C]\n")
    a = App(str(f), env={})
    assert a.set_group_order("g", ["C", "B", "A", "D"]) is None
    g = parse_config(f.read_text(), {}).groups[0]
    assert [(i.tunnel, i.expect_country) for i in g.order] == [("C", None), ("B", "DE"), ("A", None), ("D", None)]
    assert (tmp_path / "c.yaml.bak").exists()
    assert a.set_group_order("g", ["A", "A"]) == "a tunnel is listed twice"
    assert "unknown group" in a.set_group_order("nope", ["A"])
    assert a.set_group_order("g", "A") == "order must be a list of tunnel names"
    assert [i.tunnel for i in parse_config(f.read_text(), {}).groups[0].order] == ["C", "B", "A", "D"]       # refusals changed nothing


def test_set_group_order_refuses_tunnels_unifi_does_not_have(tmp_path):
    from vpn_watchdog.models import Snapshot, Tunnel
    f = tmp_path / "c.yaml"
    f.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\ngroups:\n  - {name: g, networks: [n], order: [A]}\n")
    a = App(str(f), env={})
    a.engine._snap = Snapshot({"i": Tunnel("i", "A", True)}, {}, [], {})
    assert "unknown tunnel(s): Z" in a.set_group_order("g", ["A", "Z"])
    assert a.set_group_order("g", ["A"]) is None
