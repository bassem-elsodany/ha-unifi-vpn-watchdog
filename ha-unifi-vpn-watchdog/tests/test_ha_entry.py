import json

from vpn_watchdog import ha_entry


def test_options_become_env_and_config_is_seeded(tmp_path, monkeypatch):
    opts = tmp_path / "options.json"
    opts.write_text(json.dumps({"unifi_api_key": "abc", "unifi_url": "https://u", "notify_service": "notify.mobile_app_x", "log_level": "DEBUG"}))
    cfg = tmp_path / "addon_config" / "config.yaml"
    monkeypatch.setattr(ha_entry, "OPTIONS", opts)
    monkeypatch.setattr(ha_entry, "CONFIG", cfg)
    for k in ("UNIFI_API_KEY", "UNIFI_URL", "NOTIFY_SERVICE", "LOG_LEVEL"):
        monkeypatch.delenv(k, raising=False)
    captured = {}
    monkeypatch.setattr("vpn_watchdog.cli.main", lambda argv: captured.setdefault("argv", argv) and 0)
    assert ha_entry.main() == 0
    assert captured["argv"] == ["run", "--config", str(cfg)]
    assert cfg.exists() and "order: []" in cfg.read_text()
    import os
    from vpn_watchdog.config import parse_config
    c = parse_config(cfg.read_text(), dict(os.environ))
    assert c.unifi.api_key == "abc" and c.server.trust_ingress and c.mqtt.supervisor
    assert c.notifications[0].service == "notify.mobile_app_x"


def test_refuses_to_start_without_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(ha_entry, "OPTIONS", tmp_path / "missing.json")
    monkeypatch.delenv("UNIFI_API_KEY", raising=False)
    assert ha_entry.main() == 1
