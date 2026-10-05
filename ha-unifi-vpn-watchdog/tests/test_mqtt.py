from vpn_watchdog import ha_mqtt
from vpn_watchdog.config import MqttCfg


class FakeClient:
    instances = []

    def __init__(self, *a, **k):
        self.auth = None
        self.connected_to = None
        FakeClient.instances.append(self)

    def username_pw_set(self, u, p): self.auth = (u, p)
    def will_set(self, *a, **k): pass
    def connect_async(self, host, port): self.connected_to = (host, port)
    def loop_start(self): pass


def make(monkeypatch, cfg, supervisor):
    FakeClient.instances.clear()
    monkeypatch.setattr(ha_mqtt.mqtt, "Client", FakeClient)
    monkeypatch.setattr(ha_mqtt, "supervisor_mqtt", supervisor)
    ha_mqtt.MqttPublisher(cfg, lambda: None)
    return FakeClient.instances[0]


def no_service():
    raise RuntimeError("400 Bad Request")


def test_supervisor_broker_is_preferred(monkeypatch):
    c = make(monkeypatch, MqttCfg(enabled=True, supervisor=True),
             lambda: {"host": "core-mosquitto", "port": 1883, "username": "u", "password": "p"})
    assert c.connected_to == ("core-mosquitto", 1883) and c.auth == ("u", "p")


def test_falls_back_to_addon_options_when_supervisor_has_no_broker(monkeypatch):
    monkeypatch.setenv("MQTT_HOST", "10.0.10.100")
    monkeypatch.setenv("MQTT_USERNAME", "ha")
    monkeypatch.setenv("MQTT_PASSWORD", "secret")
    c = make(monkeypatch, MqttCfg(enabled=True, supervisor=True), no_service)
    assert c.connected_to == ("10.0.10.100", 1883) and c.auth == ("ha", "secret")


def test_falls_back_to_config_yaml_host(monkeypatch):
    monkeypatch.delenv("MQTT_HOST", raising=False)
    c = make(monkeypatch, MqttCfg(enabled=True, supervisor=True, host="broker.lan", port=1884), no_service)
    assert c.connected_to == ("broker.lan", 1884)


def test_no_broker_anywhere_still_raises_so_the_app_can_warn(monkeypatch):
    import pytest
    monkeypatch.delenv("MQTT_HOST", raising=False)
    with pytest.raises(RuntimeError):
        make(monkeypatch, MqttCfg(enabled=True, supervisor=True), no_service)


def test_supervisor_error_body_is_surfaced_not_hidden():
    import httpx
    import pytest

    t = httpx.MockTransport(lambda req: httpx.Response(400, json={"result": "error", "message": "MQTT not enabled"}))
    with pytest.raises(RuntimeError, match="MQTT not enabled"):
        ha_mqtt.supervisor_mqtt(transport=t)
    ok = httpx.MockTransport(lambda req: httpx.Response(200, json={"result": "ok", "data": {"host": "core-mosquitto", "port": 1883}}))
    assert ha_mqtt.supervisor_mqtt(transport=ok)["host"] == "core-mosquitto"
