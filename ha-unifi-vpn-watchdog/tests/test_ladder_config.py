import pytest

from vpn_watchdog.config import ConfigError, GroupCfg, parse_config
from vpn_watchdog.ladder import candidates, expected_country, failback_targets, missing, position_label, position_of, resolve_order
from vpn_watchdog.models import parse_tunnel


def tun(name, enabled=True):
    return parse_tunnel({"_id": "i-" + name, "name": name, "enabled": enabled})


# Deliberately unstructured: no common scheme, spaces, slashes, mixed case.
NAMES = ["Home-Primary", "Home-Backup", "Cousin vpn 2", "Cousin vpn 10", "office/berlin", "Zeta"]
TS = [tun(n) for n in NAMES]


def group(order):
    return GroupCfg(name="g", networks=["n"], order=order)


def names(ts):
    return [t.name for t in ts]


def test_a_tunnel_is_just_its_name_nothing_is_parsed():
    t = tun("IT__ROME__418__187.14.84.144")
    assert (t.name, t.enabled) == ("IT__ROME__418__187.14.84.144", True)
    assert not hasattr(t, "iso") and not hasattr(t, "city")


def test_the_order_is_exactly_the_sequence_the_user_wrote():
    g = group(["Zeta", "Cousin vpn 10", "Home-Primary", "office/berlin"])
    assert names(resolve_order(g, TS)) == ["Zeta", "Cousin vpn 10", "Home-Primary", "office/berlin"]   # no sorting, no patterns


def test_tunnels_not_in_the_order_are_never_used_and_unknown_names_are_skipped_and_reported():
    g = group(["Zeta", "Does-not-exist", "Home-Primary"])
    assert names(resolve_order(g, TS)) == ["Zeta", "Home-Primary"]
    assert missing(g, TS) == ["Does-not-exist"]
    assert "Cousin vpn 2" not in names(candidates(g, TS, None))


def test_candidates_are_the_sequence_from_the_top_without_the_current_tunnel():
    g = group(["Home-Primary", "Home-Backup", "Zeta"])
    assert names(candidates(g, TS, TS[1])) == ["Home-Primary", "Zeta"]


def test_failback_only_moves_up_the_list():
    g = group(["Home-Primary", "Home-Backup", "Zeta"])
    assert names(failback_targets(g, TS, TS[5])) == ["Home-Primary", "Home-Backup"]      # Zeta is #3
    assert names(failback_targets(g, TS, TS[1])) == ["Home-Primary"]
    assert failback_targets(g, TS, TS[0]) == []                                         # already #1
    assert failback_targets(g, TS, TS[2]) == []                                         # not in the order: nothing to go back to


def test_positions_and_expected_country_come_only_from_the_config():
    g = group(["Home-Primary", {"tunnel": "Zeta", "expect_country": "it"}])
    assert position_of(g, TS[5]) == 2 and position_label(g, TS[5]) == "#2" and position_label(g, TS[2]) == ""
    assert expected_country(g, TS[5]) == "IT" and expected_country(g, TS[0]) is None
    assert expected_country(g, tun("IT__ROME__1__1.1.1.1")) is None                    # nothing guessed from a name


BASE = "unifi: {api_key: ${K}}\ngroups:\n  - {name: a, networks: [n], order: [T1]}\n"


def test_env_interpolation_and_default():
    cfg = parse_config(BASE, env={"K": "secret"})
    assert cfg.unifi.api_key == "secret" and cfg.groups[0].order[0].tunnel == "T1"
    assert parse_config("unifi: {api_key: '${NOPE:-fallback}'}\ngroups:\n  - {name: a, networks: [n]}\n", env={}).unifi.api_key == "fallback"


def test_an_empty_order_is_allowed_so_a_fresh_install_starts():
    assert parse_config("unifi: {api_key: k}\ngroups:\n  - {name: a, networks: [n]}\n", env={}).groups[0].order == []


def test_duplicate_tunnel_in_the_order_is_rejected():
    with pytest.raises(ConfigError, match="twice"):
        parse_config(BASE.replace("order: [T1]", "order: [T1, T2, T1]"), env={"K": "x"})


def test_missing_env_var_is_an_error():
    with pytest.raises(ConfigError, match="K"):
        parse_config(BASE, env={})


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError):
        parse_config(BASE.replace("unifi:", "dryrun: true\nunifi:"), env={"K": "x"})


def test_removed_settings_are_reported_with_instructions_never_converted():
    with pytest.raises(ConfigError, match="ladder was replaced by `order`") as e:
        parse_config(BASE.replace("order: [T1]", "ladder: [{country: IT}]"), env={"K": "x"})
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
