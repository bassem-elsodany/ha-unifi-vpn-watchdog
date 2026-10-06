"""keep_ready: the active VPN client plus (keep_ready - 1) warm standbys, the next ones in the fallback order."""
from __future__ import annotations

from conftest import run, tid

ORDER3 = '[Home-Primary, Home-Backup, "Cousin vpn 2", office/frankfurt]'


def ready(eng, n):
    eng.cfg = eng.cfg.model_copy(update={"groups": [eng.cfg.groups[0].model_copy(update={"keep_ready": n})]})


def on(un):
    return set(un.active_names())


def test_by_default_only_the_active_client_is_on(make_engine):
    eng, un, _, _, clock = make_engine(order=ORDER3)
    run(eng, clock, 4)
    assert on(un) == {"Home-Primary"}
    assert eng.status()["map"]["groups"][0]["warm"] == []


def test_keep_ready_keeps_the_next_clients_on_and_connected(make_engine):
    eng, un, _, _, clock = make_engine(order=ORDER3)
    ready(eng, 3)
    run(eng, clock, 4)
    assert on(un) == {"Home-Primary", "Home-Backup", "Cousin vpn 2"}
    g = eng.status()["map"]["groups"][0]
    assert g["active"] == "Home-Primary" and [w["name"] for w in g["warm"]] == ["Home-Backup", "Cousin vpn 2"] and all(w["connected"] for w in g["warm"])
    tags = {t["name"]: (t["standby"], t["standby_ready"]) for t in g["lane"]}              # what the Status page tags in the fallback order
    assert tags["Home-Backup"] == (True, True) and tags["Cousin vpn 2"] == (True, True) and tags["Home-Primary"][0] is False and tags["office/frankfurt"][0] is False


def test_a_failover_goes_to_a_standby_at_once_and_a_new_standby_is_brought_up(make_engine):
    eng, un, _, _, clock = make_engine(order=ORDER3)
    ready(eng, 2)
    run(eng, clock, 3)
    assert on(un) == {"Home-Primary", "Home-Backup"}
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 8)
    g = eng.status()["map"]["groups"][0]
    assert g["active"] == "Home-Backup"
    assert on(un) == {"Home-Backup", "Cousin vpn 2"}                       # the failed one is off, the next standby is on
    assert [w["name"] for w in g["warm"]] == ["Cousin vpn 2"]


def test_a_standby_that_never_connects_is_given_up_and_the_next_one_takes_its_place(make_engine):
    eng, un, _, _, clock = make_engine(order=ORDER3)
    un.dead.add(tid("Home-Backup"))
    ready(eng, 2)
    run(eng, clock, 7)                                                      # inside its quarantine (it is retried after the back-off)
    assert on(un) == {"Home-Primary", "Cousin vpn 2"}
    assert [w["name"] for w in eng.status()["map"]["groups"][0]["warm"]] == ["Cousin vpn 2"]


def test_a_paused_group_keeps_its_standbys_as_they_are(make_engine):
    eng, un, _, _, clock = make_engine(order=ORDER3)
    ready(eng, 2)
    run(eng, clock, 3)
    eng.submit("pause", "g1")
    run(eng, clock, 4)
    assert on(un) == {"Home-Primary", "Home-Backup"}


def test_the_setting_round_trips_through_the_settings_form():
    from vpn_watchdog import settings
    from vpn_watchdog.config import parse_config
    raw = {"unifi": {"api_key": "x"}, "groups": [{"name": "g", "order": ["a", "b", "c"]}]}
    form = settings.extract(parse_config("unifi: {api_key: x}\ngroups: [{name: g, order: [a, b, c]}]", env={}))
    assert form["groups"][0]["keep_ready"] == 1
    form["groups"][0]["keep_ready"] = 3
    assert settings.apply(raw, form)["groups"][0]["keep_ready"] == 3
    form["groups"][0]["keep_ready"] = 1
    assert "keep_ready" not in settings.apply(raw, form)["groups"][0]
