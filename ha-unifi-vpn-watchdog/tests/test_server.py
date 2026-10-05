import json
import time
import urllib.error
import urllib.request

import pytest

from vpn_watchdog.config import ServerCfg
from vpn_watchdog.server import StatusServer, render_metrics


class StubEngine:
    def __init__(self):
        self.last_tick = time.time()
        self.last_error = None
        self.submitted = []
        self.wake = type("W", (), {"set": lambda s: None})()

    def status(self):
        return {"dry_run": True, "last_tick": self.last_tick, "groups": {"g": {"healthy": True, "switches_last_hour": 0, "active": "IT__A", "country": "IT"}},
                "tunnels": {"IT__A": {"status": "CONNECTED", "quarantined_for": 0}}, "events": []}

    def submit(self, *c):
        self.submitted.append(c)


class StubApp:
    def __init__(self):
        self.engine = StubEngine()
        self.saved = None

    def config_text(self): return "dry_run: true\n"
    def validate_text(self, t): return None if "bad" not in t else "unifi: field required"
    def save_text(self, t):
        err = self.validate_text(t)
        self.saved = None if err else t
        return err
    def set_dry_run(self, v): return None
    def ha_notify_services(self): return {"available": True, "current": "notify.a", "services": [{"service": "notify.a", "label": "A"}], "error": None}
    def set_notify_service(self, s): self.chosen = s; return None
    def test_notify(self, s=None): return None


@pytest.fixture
def srv():
    app = StubApp()
    s = StatusServer(ServerCfg(host="127.0.0.1", port=0, control_token="tok"), app, stale_after=60)
    s.start()
    yield app, f"http://127.0.0.1:{s.port}"
    s.stop()


def call(url, method="GET", body=None, token=None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_health_status_metrics_and_ui_are_readable_without_token(srv):
    app, base = srv
    assert call(base + "/healthz")[0] == 200
    code, body = call(base + "/api/status")
    assert code == 200 and json.loads(body)["can_control"] is False
    assert 'vpn_watchdog_group_healthy{group="g"} 1' in call(base + "/metrics")[1]
    code, html = call(base + "/")
    assert code == 200 and "VPN Watchdog" in html
    app.engine.last_tick = time.time() - 3600
    assert call(base + "/healthz")[0] == 503


def test_control_needs_the_token(srv):
    app, base = srv
    assert call(base + "/api/groups/g/pause", "POST", {})[0] == 401
    assert call(base + "/api/groups/g/pause", "POST", {}, token="wrong")[0] == 401
    assert call(base + "/api/config")[0] == 401
    assert call(base + "/api/groups/g/pause", "POST", {}, token="tok")[0] == 202
    assert call(base + "/api/groups/g/switch", "POST", {"tunnel": "IT__B"}, token="tok")[0] == 202
    assert app.engine.submitted == [("pause", "g"), ("switch", "g", "IT__B")]


def test_config_validate_and_save_flow(srv):
    app, base = srv
    assert json.loads(call(base + "/api/config", token="tok")[1])["yaml"] == "dry_run: true\n"
    assert json.loads(call(base + "/api/config/validate", "POST", {"yaml": "bad"}, token="tok")[1])["error"]
    assert call(base + "/api/config", "POST", {"yaml": "bad"}, token="tok")[0] == 400 and app.saved is None
    assert call(base + "/api/config", "POST", {"yaml": "ok: 1"}, token="tok")[0] == 200 and app.saved == "ok: 1"


def test_no_token_configured_means_read_only():
    app = StubApp()
    s = StatusServer(ServerCfg(host="127.0.0.1", port=0), app, stale_after=60)
    s.start()
    try:
        assert call(f"http://127.0.0.1:{s.port}/api/groups/g/pause", "POST", {}, token="")[0] == 401
    finally:
        s.stop()


def test_render_metrics_labels():
    assert 'tunnel="IT__A"' in render_metrics(StubEngine().status())


def test_mqtt_failure_is_not_fatal(tmp_path, monkeypatch):
    """Regression: Supervisor answered 400 for /services/mqtt and the whole add-on crashed."""
    from vpn_watchdog import app as app_mod

    cfg = tmp_path / "c.yaml"
    cfg.write_text("unifi: {api_key: k}\nstate_file: " + str(tmp_path / "s.json") + "\nmqtt: {enabled: true, supervisor: true}\n"
                   "groups:\n  - {name: g, networks: [n], ladder: [{country: IT}]}\n")

    def boom(*a, **k):
        raise RuntimeError("Client error '400 Bad Request' for url 'http://supervisor/services/mqtt'")

    monkeypatch.setattr(app_mod, "MqttPublisher", boom)
    a = app_mod.App(str(cfg), env={})
    a._start_mqtt()                      # must not raise
    assert a.mqtt is None


def test_notify_endpoints_need_the_token_and_forward_the_choice(srv):
    app, base = srv
    assert call(base + "/api/ha/notify-services")[0] == 401
    assert json.loads(call(base + "/api/ha/notify-services", token="tok")[1])["services"][0]["service"] == "notify.a"
    assert call(base + "/api/notify/service", "POST", {"service": "notify.b"})[0] == 401
    assert call(base + "/api/notify/service", "POST", {"service": "notify.b"}, token="tok")[0] == 200 and app.chosen == "notify.b"
    assert call(base + "/api/notify/test", "POST", {}, token="tok")[0] == 200
