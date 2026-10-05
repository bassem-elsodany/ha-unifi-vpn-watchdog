import re

import pytest

from vpn_watchdog.config import ConfigError, DEFAULT_NAMING, GroupCfg, parse_config
from vpn_watchdog.ladder import candidates, resolve_ladder
from vpn_watchdog.models import parse_tunnel

P = re.compile(DEFAULT_NAMING)


def tun(name, enabled=True):
    return parse_tunnel({"_id": "i-" + name, "name": name, "enabled": enabled}, P)


NAMES = ["IT__ROME__418__1.1.1.1", "IT__ROME__511__1.1.1.2", "IT__MILAN__244__1.1.1.3", "DE__BERLIN__1__2.2.2.1", "XX_bad_name"]


def group(ladder):
    return GroupCfg(name="g", networks=["n"], ladder=ladder)


def test_name_parsing():
    t = tun("IT__ROME__418__187.14.84.144")
    assert (t.iso, t.city, t.server_id, t.ip) == ("IT", "ROME", "418", "187.14.84.144")
    assert tun("garbage").iso is None


def test_ladder_orders_by_step_prefer_then_city_and_id():
    g = group([{"country": "IT", "prefer": ["IT__ROME__511__*"]}, {"country": "DE"}])
    names = [t.name for t in resolve_ladder(g, [tun(n) for n in NAMES])]
    assert names == ["IT__ROME__511__1.1.1.2", "IT__MILAN__244__1.1.1.3", "IT__ROME__418__1.1.1.1", "DE__BERLIN__1__2.2.2.1"]


def test_tunnel_patterns_exclude_and_dedup():
    g = group([{"tunnels": ["IT__ROME__*"], "exclude": ["*511*"]}, {"country": "IT"}])
    names = [t.name for t in resolve_ladder(g, [tun(n) for n in NAMES])]
    assert names == ["IT__ROME__418__1.1.1.1", "IT__MILAN__244__1.1.1.3", "IT__ROME__511__1.1.1.2"]


def test_candidates_exclude_current_and_prefer_other_city():
    g = group([{"country": "IT", "prefer": ["IT__ROME__418__*"]}])
    ts = [tun(n) for n in NAMES]
    cur = ts[1]  # Rome 511 is failing: ladder order is Rome 418, Milan 244, (Rome 511)
    assert [t.name for t in candidates(g, ts, cur, False)] == ["IT__ROME__418__1.1.1.1", "IT__MILAN__244__1.1.1.3"]
    # same-city Rome 418 is pushed behind the other-city Milan 244
    assert [t.name for t in candidates(g, ts, cur, True)] == ["IT__MILAN__244__1.1.1.3", "IT__ROME__418__1.1.1.1"]


BASE = "unifi: {api_key: ${K}}\ngroups:\n  - {name: a, networks: [n], ladder: [{country: it}]}\n"


def test_env_interpolation_and_default():
    cfg = parse_config(BASE, env={"K": "secret"})
    assert cfg.unifi.api_key == "secret" and cfg.groups[0].ladder[0].country == "IT"
    assert parse_config("unifi: {api_key: '${NOPE:-fallback}'}\ngroups:\n  - {name: a, networks: [n], ladder: [{country: IT}]}\n", env={}).unifi.api_key == "fallback"


def test_missing_env_var_is_an_error():
    with pytest.raises(ConfigError, match="K"):
        parse_config(BASE, env={})


def test_unknown_keys_are_rejected():
    with pytest.raises(ConfigError):
        parse_config(BASE.replace("unifi:", "dryrun: true\nunifi:"), env={"K": "x"})


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


def test_bad_naming_regex_is_rejected():
    with pytest.raises(ConfigError):
        parse_config(BASE + "naming: {pattern: '('}\n", env={"K": "x"})
