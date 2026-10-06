import pytest
import yaml

from vpn_watchdog import settings
from vpn_watchdog.config import ConfigError, load_raw, parse_config
from vpn_watchdog.models import Snapshot, Tunnel

RAW = """
dry_run: true
unifi: {api_key: ${UNIFI_API_KEY}, url: 'https://10.0.1.1'}
detection: {failure_threshold: 3}
groups:
  - name: g1
    route_id: keep-me
    networks: [vlan20-iot, vlan50-vpn]
    overrides: {detection: {failure_threshold: 9}}
    order:
      - Home-Primary
      - {tunnel: Office Berlin, expect_country: de}
      - Last resort
"""
ENV = {"UNIFI_API_KEY": "k"}


def cfg_of(raw_text):
    return parse_config(raw_text, ENV)


def test_extract_reads_effective_values_including_defaults():
    v = settings.extract(cfg_of(RAW))
    assert v["interval_seconds"] == 15 and v["detection"]["failure_threshold"] == 3
    assert v["switching"]["on_exhausted"] == "keep" and v["failback"]["enabled"] is True
    assert "prefer_different_city" not in v["switching"]
    assert v["groups"][0]["order"] == [{"tunnel": "Home-Primary", "expect_country": ""},
                                       {"tunnel": "Office Berlin", "expect_country": "DE"},
                                       {"tunnel": "Last resort", "expect_country": ""}]


def test_apply_changes_values_and_preserves_what_the_form_does_not_own():
    raw = load_raw(RAW)
    form = settings.extract(cfg_of(RAW))
    form["interval_seconds"] = 30
    form["detection"]["failure_threshold"] = 5
    form["failback"]["enabled"] = False
    form["groups"][0]["kill_switch"] = True
    new = settings.apply(raw, form)
    assert new["interval_seconds"] == 30 and new["detection"]["failure_threshold"] == 5 and new["failback"]["enabled"] is False
    g = new["groups"][0]
    assert g["route_id"] == "keep-me" and g["overrides"] == {"detection": {"failure_threshold": 9}}   # preserved
    assert g["kill_switch"] is True
    assert new["unifi"]["api_key"] == "${UNIFI_API_KEY}"                                              # secret stays a reference
    assert g["order"] == ["Home-Primary", {"tunnel": "Office Berlin", "expect_country": "DE"}, "Last resort"]
    cfg_of(yaml.safe_dump(new))


def test_reordering_in_the_form_is_exactly_what_gets_saved():
    raw = load_raw(RAW)
    form = settings.extract(cfg_of(RAW))
    a, b, c = form["groups"][0]["order"]
    form["groups"][0]["order"] = [c, a, b, {"tunnel": "Brand new", "expect_country": "fr"}, {"tunnel": "   ", "expect_country": ""}]
    new = settings.apply(raw, form)
    assert new["groups"][0]["order"] == ["Last resort", "Home-Primary", {"tunnel": "Office Berlin", "expect_country": "DE"},
                                         {"tunnel": "Brand new", "expect_country": "FR"}]            # blank row dropped
    assert [i.tunnel for i in cfg_of(yaml.safe_dump(new)).groups[0].order] == ["Last resort", "Home-Primary", "Office Berlin", "Brand new"]


def test_the_same_tunnel_twice_is_rejected_by_validation():
    raw = load_raw(RAW)
    form = settings.extract(cfg_of(RAW))
    form["groups"][0]["order"].append({"tunnel": "Home-Primary", "expect_country": ""})
    with pytest.raises(ConfigError, match="twice"):
        cfg_of(yaml.safe_dump(settings.apply(raw, form)))


def test_bad_values_are_rejected_by_validation_not_silently_saved():
    raw = load_raw(RAW)
    form = settings.extract(cfg_of(RAW))
    form["interval_seconds"] = 1
    with pytest.raises(ConfigError, match="interval_seconds"):
        cfg_of(yaml.safe_dump(settings.apply(raw, form)))
    form["interval_seconds"] = "abc"
    with pytest.raises(ValueError):
        settings.apply(raw, form)
    form["interval_seconds"] = 15
    form["switching"]["on_exhausted"] = "explode"
    with pytest.raises(ValueError):
        settings.apply(raw, form)


def test_probe_fields_and_clearing_them():
    raw = load_raw(RAW)
    form = settings.extract(cfg_of(RAW))
    form["probe"] = {"mode": "remote", "check_country": False, "canary_mac": "02:42:0a:00:3c:c8", "remote_url": "http://10.0.60.200:8081"}
    new = settings.apply(raw, form)
    c = cfg_of(yaml.safe_dump(new))
    assert c.probe.mode == "remote" and c.probe.canary.mac and c.probe.check_country is False
    form["probe"] = {"mode": "none", "canary_mac": "", "remote_url": ""}
    c2 = cfg_of(yaml.safe_dump(settings.apply(new, form)))
    assert c2.probe.mode == "none" and c2.probe.remote_url is None and c2.probe.canary.mac is None


def test_meta_lists_networks_and_tunnels_without_any_interpretation():
    t = lambda n: Tunnel("id-" + n, n, True)
    snap = Snapshot({"id-a": t("Home 1"), "id-b": t("Home 2"), "id-c": t("zeta")}, {}, [],
                    {"id-a": "Home 1", "id-b": "Home 2", "id-c": "zeta", "n1": "vlan20-iot", "n2": "Internet 1", "n3": "One-Click VPN"})
    m = settings.meta(snap)
    assert m["networks"] == ["vlan20-iot"]
    assert [x["name"] for x in m["tunnels"]] == ["Home 1", "Home 2", "zeta"]
    assert "countries" not in m
    assert settings.meta(None)["ready"] is False


def test_save_settings_works_when_the_file_uses_inline_env_references(tmp_path):
    """Regression: plain yaml.safe_load cannot read `{api_key: ${VAR}}`, so saving from the form failed."""
    from vpn_watchdog.app import App

    f = tmp_path / "c.yaml"
    f.write_text("unifi: {api_key: ${UNIFI_API_KEY}}\nstate_file: " + str(tmp_path / "s.json") +
                 "\ngroups:\n  - {name: g, networks: [n], order: [T1]}\n")
    a = App(str(f), env={"UNIFI_API_KEY": "k"})
    form = settings.extract(a.cfg)
    form["interval_seconds"] = 45
    assert a.save_settings(form) is None
    saved = f.read_text()
    assert "${UNIFI_API_KEY}" in saved and "k\n" not in saved.split("api_key")[1][:25]
    assert parse_config(saved, {"UNIFI_API_KEY": "k"}).interval_seconds == 45
    assert (tmp_path / "c.yaml.bak").exists()
    form["interval_seconds"] = 1
    assert "interval_seconds" in a.save_settings(form)             # rejected, file untouched
    assert parse_config(f.read_text(), {"UNIFI_API_KEY": "k"}).interval_seconds == 45
