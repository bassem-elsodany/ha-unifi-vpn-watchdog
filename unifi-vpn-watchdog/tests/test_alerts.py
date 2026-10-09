import pytest

from conftest import FakeClock
from vpn_watchdog import alerts, settings
from vpn_watchdog.config import ConfigError, load_raw, parse_config
from vpn_watchdog.notify import Notifier

BASE = "unifi: {api_key: k}\ngroups:\n  - {name: g, networks: [n], order: [T1]}\n"


def cfg(extra=""):
    return parse_config(BASE + extra, env={})


def test_defaults_cover_every_event_and_startup_is_quiet():
    c = cfg()
    assert set(c.alerts) == set(alerts.EVENTS)
    assert c.alerts["switch"].enabled and not c.alerts["startup"].enabled


def test_partial_override_keeps_the_other_defaults():
    c = cfg("alerts: {switch: {message: 'moved to {tunnel}'}, leak: {enabled: false}}\n")
    assert c.alerts["switch"].message == "moved to {tunnel}" and c.alerts["switch"].title == alerts.EVENTS["switch"]["title"]
    assert c.alerts["leak"].enabled is False


def test_unknown_placeholder_or_event_is_rejected():
    with pytest.raises(ConfigError, match="placeholder"):
        cfg("alerts: {switch: {title: 'x {nope}'}}\n")
    with pytest.raises(ConfigError, match="placeholder"):
        cfg("alerts: {switch: {title: 'moved to {country} {city}'}}\n")      # country/city placeholders no longer exist
    with pytest.raises(ConfigError, match="unknown alert"):
        cfg("alerts: {swich: {enabled: true}}\n")


def test_render_fills_missing_fields_with_blank():
    assert alerts.render("{group}: {previous} -> {tunnel}", {"group": "g", "tunnel": "IT__A"}) == "g:  -> IT__A"


def test_notifier_uses_templates_and_respects_enabled_flag():
    sent = []
    import httpx, json
    from vpn_watchdog.config import NotifyCfg

    def handler(req):
        sent.append(json.loads(req.content)); return httpx.Response(200)

    c = cfg("alerts: {switch: {title: 'Moved {group}', message: '{previous} to {tunnel} ({position})'}, recovered: {enabled: false}}\n")
    n = Notifier([NotifyCfg(type="webhook", url="http://h")], FakeClock(), transport=httpx.MockTransport(handler), alerts=c.alerts)
    n.emit("switch", group="iot", previous="Alpha one", tunnel="Beta 2", position="#2")
    n.emit("recovered", group="iot", tunnel="DE__B")
    assert len(sent) == 1 and sent[0]["title"] == "Moved iot" and sent[0]["message"] == "Alpha one to Beta 2 (#2)"
    assert sent[0]["level"] == "warning"                                  # level comes from the event definition
    assert [h["event"] for h in n.history] == ["recovered", "switch"]     # disabled alerts still appear in the Events tab


def test_form_saves_alert_edits_and_validates_them():
    raw = load_raw(BASE)
    form = settings.extract(cfg())
    form["alerts"]["switch"] = {"enabled": False, "title": "T {group}", "message": "M {reason}"}
    new = settings.apply(raw, form)
    c = parse_config(__import__("yaml").safe_dump(new), env={})
    assert c.alerts["switch"].enabled is False and c.alerts["switch"].title == "T {group}"
    form["alerts"]["switch"]["title"] = "{bogus}"
    with pytest.raises(ConfigError):
        parse_config(__import__("yaml").safe_dump(settings.apply(raw, form)), env={})
