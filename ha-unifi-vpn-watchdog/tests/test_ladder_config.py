import pytest

from vpn_watchdog.config import ConfigError, GroupCfg, parse_config
from vpn_watchdog.ladder import candidates, expected_country, resolve_ladder, step_of, suggest_groups
from vpn_watchdog.models import parse_tunnel


def tun(name, enabled=True):
    return parse_tunnel({"_id": "i-" + name, "name": name, "enabled": enabled})


# Deliberately unstructured: no common scheme, spaces, slashes, mixed case.
NAMES = ["Home-Primary", "Home-Backup", "Cousin vpn 2", "Cousin vpn 10", "office/berlin", "Zeta"]


def group(ladder):
    return GroupCfg(name="g", networks=["n"], ladder=ladder)


def names(g, ts):
    return [t.name for t in resolve_ladder(g, ts)]


def test_a_tunnel_is_just_its_name_nothing_is_parsed():
    t = tun("IT__ROME__418__187.14.84.144")
    assert (t.name, t.enabled) == ("IT__ROME__418__187.14.84.144", True)
    assert not hasattr(t, "iso") and not hasattr(t, "city")


def test_steps_pick_tunnels_by_exact_name_or_glob_in_the_order_written():
    g = group([{"name": "A", "tunnels": ["Zeta", "Home-*"]}, {"name": "B", "tunnels": ["Cousin*", "office/berlin"]}])
    assert names(g, [tun(n) for n in NAMES]) == ["Zeta", "Home-Backup", "Home-Primary", "Cousin vpn 2", "Cousin vpn 10", "office/berlin"]


def test_globs_expand_in_natural_order_not_text_order():
    g = group([{"tunnels": ["Cousin*"]}])
    assert names(g, [tun(n) for n in NAMES]) == ["Cousin vpn 2", "Cousin vpn 10"]       # 2 before 10


def test_prefer_goes_first_exclude_removes_and_a_tunnel_belongs_to_one_step():
    g = group([{"tunnels": ["Home-*", "Cousin*"], "prefer": ["Cousin vpn 10"], "exclude": ["Home-Backup"]}, {"tunnels": ["*"]}])
    assert names(g, [tun(n) for n in NAMES]) == ["Cousin vpn 10", "Home-Primary", "Cousin vpn 2", "Home-Backup", "office/berlin", "Zeta"]


def test_candidates_never_include_the_current_tunnel():
    g = group([{"tunnels": ["Home-*"]}, {"tunnels": ["Zeta"]}])
    ts = [tun(n) for n in NAMES]
    assert [t.name for t in candidates(g, ts, ts[0])] == ["Home-Backup", "Zeta"]


def test_step_label_and_expected_country_come_only_from_the_config():
    g = group([{"name": "Cousin", "tunnels": ["Cousin*"], "expect_country": "it"}, {"tunnels": ["Zeta"]}])
    ts = [tun(n) for n in NAMES]
    assert step_of(g, ts, ts[2])[1] == "Cousin" and step_of(g, ts, ts[5])[1] == "Step 2"
    assert expected_country(g, ts, ts[2]) == "IT"          # typed by the user
    assert expected_country(g, ts, ts[5]) is None          # nothing is ever guessed from the name
    assert expected_country(g, ts, tun("IT__ROME__1__1.1.1.1")) is None


def test_suggestions_group_by_a_shared_first_word_or_fall_back_to_one_list():
    s = suggest_groups([tun(n) for n in ["Home-A", "Home-B", "Cousin x", "Cousin y", "Solo"]])
    assert [x["label"] for x in s] == ["Cousin", "Home", "Others"] and s[1]["tunnels"] == ["Home-A", "Home-B"]
    one = suggest_groups([tun(n) for n in ["a", "b", "c"]])
    assert len(one) == 1 and one[0]["label"] == "All tunnels"
    assert all(x["expect_country"] is None for x in s + one)


BASE = "unifi: {api_key: ${K}}\ngroups:\n  - {name: a, networks: [n], ladder: [{tunnels: ['*']}]}\n"


def test_env_interpolation_and_default():
    cfg = parse_config(BASE, env={"K": "secret"})
    assert cfg.unifi.api_key == "secret"
    assert parse_config("unifi: {api_key: '${NOPE:-fallback}'}\ngroups:\n  - {name: a, networks: [n], ladder: [{tunnels: ['*']}]}\n", env={}).unifi.api_key == "fallback"


def test_missing_env_var_is_an_error():
    with pytest.raises(ConfigError, match="K"):
        parse_config(BASE, env={})


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError):
        parse_config(BASE.replace("unifi:", "dryrun: true\nunifi:"), env={"K": "x"})


def test_a_step_without_tunnels_is_rejected():
    with pytest.raises(ConfigError, match="tunnels"):
        parse_config(BASE.replace("{tunnels: ['*']}", "{name: empty}"), env={"K": "x"})


def test_removed_settings_are_reported_with_instructions_never_converted():
    with pytest.raises(ConfigError, match="`country` was removed.*tunnels") as e:
        parse_config(BASE.replace("{tunnels: ['*']}", "{country: IT}"), env={"K": "x"})
    assert "became" not in str(e.value)
    with pytest.raises(ConfigError, match="naming"):
        parse_config(BASE + "naming: {pattern: '(?P<iso>..)'}\n", env={"K": "x"})
    with pytest.raises(ConfigError, match="prefer_different_city"):
        parse_config(BASE + "switching: {prefer_different_city: true}\n", env={"K": "x"})


def test_dry_run_is_the_default():
    assert parse_config(BASE, env={"K": "x"}).dry_run is True


def test_canary_requires_mac_and_remote_requires_url():
    with pytest.raises(ConfigError):
        parse_config(BASE + "probe: {mode: canary}\n", env={"K": "x"})
    with pytest.raises(ConfigError):
        parse_config(BASE + "probe: {mode: remote, canary: {mac: 'aa:bb'}}\n", env={"K": "x"})


def test_group_overrides_merge_over_globals():
    cfg = parse_config(BASE.replace("{name: a,", "{name: a, overrides: {detection: {failure_threshold: 9}},") + "detection: {probe_interval_seconds: 5}\n", env={"K": "x"})
    s = cfg.settings_for(cfg.groups[0])
    assert (s.detection.failure_threshold, s.detection.probe_interval_seconds) == (9, 5)


def test_bad_override_is_rejected_at_load():
    with pytest.raises(ConfigError):
        parse_config(BASE.replace("{name: a,", "{name: a, overrides: {detection: {nope: 1}},"), env={"K": "x"})
