from conftest import active_name, run, tid


def test_healthy_tunnel_causes_no_switch(make_engine):
    eng, un, _, notes, clock = make_engine()
    run(eng, clock, 6)
    assert active_name(un) == "Home-Primary"
    assert "switch" not in notes.kinds()


def test_failure_below_threshold_does_not_switch(make_engine):
    eng, un, _, notes, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 2)
    assert active_name(un) == "Home-Primary"


def test_switches_to_next_tunnel_of_the_same_step_first(make_engine):
    eng, un, _, notes, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    # The step lists Home-* then Cousin*: the next tunnel of the same step is used before any other step.
    assert active_name(un) == "Home-Backup"
    assert notes.kinds().count("switch") == 1


def test_whole_step_down_falls_to_next_step(make_engine):
    eng, un, _, notes, clock = make_engine()
    for n in ("Home-Primary", "Home-Backup", "Cousin vpn 2"):
        un.dead.add(tid(n))
    run(eng, clock, 3)
    assert active_name(un).startswith("office/")


def test_candidate_that_never_connects_is_skipped_and_quarantined(make_engine):
    eng, un, _, _, clock = make_engine()
    un.dead.update({tid("Home-Primary"), tid("Home-Backup")})
    run(eng, clock, 3)
    assert active_name(un) == "Cousin vpn 2"
    assert eng.store.tunnel(tid("Home-Backup")).quarantined_until > clock.now()


def test_pretest_failure_skips_candidate(make_engine):
    eng, un, tester, _, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    tester.bad.add(tid("Home-Backup"))
    run(eng, clock, 3)
    assert active_name(un) == "Cousin vpn 2"
    assert ("Home-Backup", True) in tester.tested


def test_quarantine_backoff_grows(make_engine):
    eng, un, _, _, clock = make_engine()
    t = tid("Home-Backup")
    from vpn_watchdog.models import Tunnel
    tun = Tunnel(t, "x", True)
    st = eng.cfg.settings_for(eng.cfg.groups[0])
    eng._quarantine(tun, "a", st)
    first = eng.store.tunnel(t).quarantined_until - clock.now()
    eng._quarantine(tun, "b", st)
    second = eng.store.tunnel(t).quarantined_until - clock.now()
    assert (first, second) == (120, 240)


def test_blackhole_detected_as_down(make_engine):
    eng, un, _, _, clock = make_engine()
    un.muted.add(tid("Home-Primary"))
    run(eng, clock, 6 + 3)   # 6 samples to fill the window, then 3 failing polls
    assert active_name(un) != "Home-Primary"


def test_probe_failure_triggers_failover(make_engine):
    eng, un, tester, _, clock = make_engine()
    tester.bad.add(tid("Home-Primary"))
    run(eng, clock, 4)
    assert active_name(un) != "Home-Primary"


def test_leak_fails_over_immediately_and_alerts(make_engine):
    eng, un, tester, notes, clock = make_engine(extra_switching=", min_hold_seconds: 0")
    tester.leak.add(tid("Home-Primary"))
    run(eng, clock, 1)
    assert "leak" in notes.kinds()
    assert active_name(un) != "Home-Primary"


def test_failback_waits_for_stability(make_engine):
    eng, un, _, notes, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    assert active_name(un) == "Home-Backup"
    un.dead.clear()                       # the primary recovers
    run(eng, clock, 6, step=15)            # quarantine (120s) still running -> no failback
    assert active_name(un) == "Home-Backup"
    run(eng, clock, 20, step=15)           # quarantine over, then 60s stable
    assert active_name(un) == "Home-Primary"
    assert "failback" in notes.kinds()


def test_failback_disabled(make_engine):
    eng, un, _, _, clock = make_engine(failback=False)
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    un.dead.clear()
    run(eng, clock, 40)
    assert active_name(un) == "Home-Backup"


def test_max_switches_per_hour_blocks_flapping(make_engine):
    eng, un, _, notes, clock = make_engine(extra_switching=", max_switches_per_hour: 1, min_hold_seconds: 0")
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    first = active_name(un)
    un.dead.add(tid(first))
    run(eng, clock, 6)
    assert active_name(un) == first          # blocked: it must not switch a second time this hour
    assert "blocked" in notes.kinds()


def test_min_hold_blocks_soft_failures_but_not_hard(make_engine):
    eng, un, tester, _, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    first = active_name(un)
    tester.bad.add(tid(first))               # soft failure (probe) right after a switch
    run(eng, clock, 3, step=5)
    assert active_name(un) == first
    un.dead.add(tid(first))                  # hard failure bypasses min hold
    run(eng, clock, 3, step=5)
    assert active_name(un) != first


def test_exhausted_alerts_once_and_stays_on_the_current_client(make_engine):
    eng, un, _, notes, clock = make_engine()
    for n in ("Home-Primary", "Home-Backup", "Cousin vpn 2",
              "office/berlin", "office/frankfurt", "zzz-last-resort"):
        un.dead.add(tid(n))
    run(eng, clock, 8)
    assert notes.kinds().count("exhausted") == 1
    assert active_name(un) == "Home-Primary"       # nothing better to move to


def test_pause_and_manual_switch(make_engine):
    eng, un, _, _, clock = make_engine()
    eng.submit("pause", "g1")
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 5)
    assert active_name(un) == "Home-Primary"      # paused: no automatic action
    eng.submit("resume", "g1")
    eng.submit("switch", "g1", "zzz-last-resort")
    run(eng, clock, 3)
    assert eng.store.group("g1").current_id == tid("zzz-last-resort") and tid("zzz-last-resort") in un.enabled


def test_a_change_made_in_unifi_is_adopted(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 2)
    un.enabled = {tid("office/berlin")}                    # someone switches Home-Primary off and another client on in UniFi
    run(eng, clock, 2)
    assert eng.store.group("g1").current_id == tid("office/berlin")


def test_only_the_tunnel_in_use_stays_connected(make_engine):
    eng, un, _, _, clock = make_engine(enabled=set(__import__("conftest").TUNNELS))      # every tunnel connected to start with
    run(eng, clock, 2)
    assert un.enabled == {tid("Home-Primary")}                                         # everything else was disconnected


def test_no_probe_mode_still_fails_over_on_status(make_engine):
    eng, un, _, _, clock = make_engine(tester_enabled=False, pretest=False)
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    assert active_name(un) != "Home-Primary"


def test_status_health_flag_survives_pause(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 2)
    assert eng.status()["groups"]["g1"]["healthy"] is True
    eng.submit("pause", "g1")
    run(eng, clock, 2)
    st = eng.status()["groups"]["g1"]
    assert st["paused"] is True and st["healthy"] is True      # pausing is not a health problem


def test_status_exposes_jobs_and_events(make_engine):
    eng, un, _, notes, clock = make_engine()
    run(eng, clock, 1)
    st = eng.status()
    assert "next_failback_check_in" in st["groups"]["g1"]["jobs"] and st["interval_seconds"] == 15


def test_a_healthy_active_tunnel_is_never_left(make_engine):
    eng, un, _, notes, clock = make_engine()
    run(eng, clock, 80, step=15)
    assert active_name(un) == "Home-Primary" and notes.kinds() == []


def test_failback_goes_up_the_list_to_the_highest_recovered_position(make_engine):
    eng, un, _, notes, clock = make_engine()
    for n in ("Home-Primary", "Home-Backup", "Cousin vpn 2"):
        un.dead.add(tid(n))
    run(eng, clock, 3)
    assert active_name(un) == "office/berlin"                                   # #4: the first position that works
    un.dead.clear()
    run(eng, clock, 40, step=15)
    assert active_name(un) == "Home-Primary" and "failback" in notes.kinds()      # back to #1


def test_it_never_moves_down_or_sideways_while_the_active_one_is_healthy(make_engine):
    eng, un, _, notes, clock = make_engine(active="Cousin vpn 2", enabled={"Cousin vpn 2"})
    un.dead.update({tid("Home-Primary"), tid("Home-Backup")})                    # higher positions are broken
    run(eng, clock, 80, step=15)
    assert active_name(un) == "Cousin vpn 2" and "switch" not in notes.kinds()


def test_a_tunnel_missing_from_the_order_is_never_chosen(make_engine):
    order = "[Home-Primary, Home-Backup]"
    eng, un, _, notes, clock = make_engine(order=order)
    for n in ("Home-Primary", "Home-Backup"):
        un.dead.add(tid(n))
    run(eng, clock, 12)
    assert active_name(un) == "Home-Primary" and "exhausted" in notes.kinds()    # nothing else is allowed, however healthy


def test_empty_order_means_watch_only_and_says_so(make_engine):
    eng, un, _, notes, clock = make_engine(order="[]")
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 6)
    assert active_name(un) == "Home-Primary" and un.calls == []
    assert "no fallback order set" in eng.status()["groups"]["g1"]["decision"]


def test_a_group_without_a_fallback_order_watches_nothing_and_changes_nothing(make_engine):
    eng, un, _, notes, clock = make_engine(order="[]")
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 4)
    g = eng.status()["groups"]["g1"]
    assert g["active"] is None and "no fallback order set" in g["decision"] and un.calls == [] and "switch" not in notes.kinds()


def test_failover_only_switches_vpn_clients_on_and_off_and_never_touches_a_routing_policy(make_engine):
    """The watchdog's only write to UniFi is a VPN client's switch."""
    eng, un, _, _, clock = make_engine()
    before = [(r.id, r.description, r.network_id, r.enabled, r.target_networks) for r in un.routes]
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 4)
    assert active_name(un) == "Home-Backup"
    assert [(r.id, r.description, r.network_id, r.enabled, r.target_networks) for r in un.routes] == before
    assert un.calls and all(c[0] == "enable" for c in un.calls)


def test_the_new_client_is_switched_on_before_the_old_one_is_switched_off(make_engine):
    eng, un, _, _, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 4)
    seq = [(c[1], c[2]) for c in un.calls if c[0] == "enable"]
    assert seq.index((tid("Home-Backup"), True)) < seq.index((tid("Home-Primary"), False))


def test_a_group_with_no_client_switched_on_gets_its_first_working_one_switched_on(make_engine):
    eng, un, _, notes, clock = make_engine(enabled=set())
    run(eng, clock, 2)
    assert active_name(un) == "Home-Primary" and not any(c[0] != "enable" for c in un.calls)


def test_the_standby_section_is_gone_and_rejected_with_a_reason():
    import pytest
    from vpn_watchdog.config import ConfigError, parse_config
    with pytest.raises(ConfigError, match="standby. was removed.*different exit IPs"):
        parse_config("unifi: {api_key: k}\nstandby: {warm: 2}\ngroups: []\n", env={})


def test_status_says_loading_until_unifi_has_been_read_once(make_engine):
    eng, un, _, _, clock = make_engine()
    assert eng.status()["loading"] is True            # nothing read yet: the UI shows its spinner
    run(eng, clock, 1)
    assert eng.status()["loading"] is False


def test_loading_is_true_while_a_cycle_is_running_and_cleared_after_even_on_error(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 1)
    seen = {}
    orig = un.snapshot

    def spy():
        seen["during"] = eng.status()["loading"]
        raise RuntimeError("UniFi exploded")

    un.snapshot = spy
    try:
        eng.tick()
    except RuntimeError:
        pass
    assert seen["during"] is True and eng.status()["loading"] is False


def test_status_map_shows_unifi_routing_without_any_group(make_engine):
    """The VLAN map is UniFi's picture, not the watchdog's: it must draw with no group configured at all."""
    eng, un, *_ = make_engine(active="Home-Backup")
    eng.cfg = eng.cfg.model_copy(update={"groups": []})
    eng.tick()
    m = eng.status()["map"]
    assert len(m["groups"]) == 1
    g = m["groups"][0]
    assert g["unmanaged"] and g["active"] == "Home-Backup"
    assert [n["name"] for n in g["networks"]] == ["vlan20-iot", "vlan50-vpn"]
    assert [t["name"] for t in g["lane"]] == ["Home-Backup"] and g["lane"][0]["position"] is None
    assert len(g["pool"]) == 5                       # every other tunnel that has a policy for these networks


def test_status_map_lists_only_unifi_vlans(make_engine):
    """Only networks UniFi types as LAN/VLAN are drawn on the left; WAN and VPN-type networks never are, whatever they are called."""
    eng, un, *_ = make_engine()
    eng.cfg = eng.cfg.model_copy(update={"groups": []})
    orig = un.snapshot

    def snap():
        sn = orig()
        sn.networks.update({"net-wan": "Home-Primary", "net-ru": "Anything"})
        sn.network_info.update({"net-wan": {"name": "Home-Primary", "purpose": "wan"}, "net-ru": {"name": "Anything", "purpose": "remote-user-vpn"}})
        for r in sn.routes:
            r.target_networks = r.target_networks | {"net-wan", "net-ru"}
        return sn
    un.snapshot = snap
    eng.tick()
    m = eng.status()["map"]
    shown = [n["name"] for g in m["groups"] for n in g["networks"]] + [n["name"] for n in m["direct"]]
    assert sorted(shown) == ["vlan20-iot", "vlan50-vpn"]


def test_status_map_lists_devices_with_their_own_route(make_engine):
    """A device-targeted policy that is switched on is listed on its own, with where it goes; its tunnel gets a lane card."""
    from vpn_watchdog.models import Route
    eng, un, *_ = make_engine(active="Home-Primary")
    orig = un.snapshot

    def snap():
        sn = orig()
        sn.clients = {"aa:aa": {"name": "tv-bedroom", "ip": "10.0.20.77", "network": "vlan20-iot", "rate_bps": 820_000, "wired": False},
                      "bb:bb": {"name": "ac_energy", "ip": "10.0.20.142", "network": "vlan20-iot", "rate_bps": 9_000, "wired": False}}
        sn.networks["net-wan"] = "Internet 1"
        return sn
    un.snapshot = snap
    un.routes.insert(0, Route("r-ac", "ac", "net-wan", True, False, frozenset(), frozenset({"bb:bb"}), {}))      # listed above the VLAN policies, so they win
    un.routes.insert(0, Route("r-tv", "tv", tid("zzz-last-resort"), True, False, frozenset(), frozenset({"aa:aa"}), {}))
    un.enabled.add(tid("zzz-last-resort"))                  # the device's client is switched on, so its policy really applies
    eng.tick()
    m = eng.status()["map"]
    assert [(d["name"], d["kind"], d["tunnel"]) for d in m["own"]] == [("tv-bedroom", "vpn", "zzz-last-resort"), ("ac_energy", "normal", None)]
    lane = {t["name"]: t for t in m["groups"][0]["lane"]}
    assert lane["zzz-last-resort"]["carries"] == ["tv-bedroom"] and lane["Home-Primary"]["carries"] == []
    iot = next(n for n in m["groups"][0]["networks"] if n["name"] == "vlan20-iot")
    assert {d["name"]: d["bypass"]["kind"] for d in iot["devices"] if d["bypass"]} == {"tv-bedroom": "vpn", "ac_energy": "normal"}


def test_a_device_policy_below_its_vlan_policy_is_overridden_and_says_which_policy_applies(make_engine):
    """UniFi reads its policies top-down and the first enabled one that catches all the device's traffic wins."""
    from vpn_watchdog.models import Route
    eng, un, *_ = make_engine(active="Home-Primary")
    orig = un.snapshot

    def snap():
        sn = orig()
        sn.clients = {"aa:aa": {"name": "washer", "ip": "10.0.20.88", "network": "vlan20-iot", "rate_bps": 3_000, "wired": False},
                      "bb:bb": {"name": "ac_energy", "ip": "10.0.20.142", "network": "vlan20-iot", "rate_bps": 9_000, "wired": False}}
        sn.networks["net-wan"] = "Internet 1"
        return sn
    un.snapshot = snap
    n_vlan = len(un.routes)
    un.routes.insert(0, Route("r-ac", "ac", "net-wan", True, False, frozenset(), frozenset({"bb:bb"}), {}))
    un.routes.append(Route("r-w", "washer direct", "net-wan", True, False, frozenset(), frozenset({"aa:aa"}), {}))
    un.routes.append(Route("r-dom", "only a domain", "net-wan", True, False, frozenset(), frozenset({"bb:bb"}), {}, matching="DOMAIN"))
    eng.tick()
    m = eng.status()["map"]
    assert [d["name"] for d in m["own"]] == ["ac_energy"] and m["own"][0]["position"] == 1 and m["own"][0]["total"] == n_vlan + 3
    off = m["own_off"]
    assert [d["name"] for d in off] == ["washer"] and off[0]["kind"] == "normal" and off[0]["position"] == n_vlan + 2
    assert off[0]["applied"]["policy"] and off[0]["applied"]["kind"] == "vpn" and off[0]["applied"]["position"] <= n_vlan
    iot = next(n for n in m["groups"][0]["networks"] if n["name"] == "vlan20-iot")
    dv = {d["name"]: d for d in iot["devices"]}
    assert dv["washer"]["overridden"] and not dv["washer"]["bypass"] and dv["ac_energy"]["bypass"]["position"] == 1 and not dv["ac_energy"]["overridden"]


def test_debug_log_level_adds_every_check_cycle_to_the_events_list(make_engine):
    import logging
    eng, *_ = make_engine(active="Home-Primary")
    root = logging.getLogger("vpn_watchdog")
    old = root.level
    root.setLevel(logging.INFO)
    try:
        eng.tick()
        assert not [e for e in eng.status()["events"] if e["event"] == "check"]
        root.setLevel(logging.DEBUG)
        eng.tick()
        eng.tick()
    finally:
        root.setLevel(old)
    checks = [e for e in eng.status()["events"] if e["event"] == "check"]
    assert len(checks) == 2 and "read UniFi" in checks[0]["message"] and "g1" in checks[0]["message"]


def test_status_map_follows_what_unifi_applies_per_vlan_when_policies_were_split(make_engine):
    import dataclasses
    """vlan20 on one VPN client and vlan50 on another: every other policy still lists both VLANs (switched off), which must not
    glue the two VLANs together or put both under whichever client is first in the list."""
    eng, un, *_ = make_engine(active="Home-Primary")
    eng.cfg = eng.cfg.model_copy(update={"groups": []})
    un.routes = [dataclasses.replace(r, target_networks=frozenset({"net-vpn"})) if r.description == "Home-Primary" else r for r in un.routes]
    un.routes = [dataclasses.replace(r, enabled=True, target_networks=frozenset({"net-iot"})) if r.description == "Home-Backup" else r for r in un.routes]
    un.enabled.add(tid("Home-Backup"))                      # both clients are switched on, each carrying one VLAN
    eng.tick()
    blocks = {b["name"]: b for b in eng.status()["map"]["groups"]}
    assert set(blocks) == {"vlan20-iot", "vlan50-vpn"}
    assert blocks["vlan20-iot"]["active"] == "Home-Backup" and blocks["vlan50-vpn"]["active"] == "Home-Primary"
    assert [n["name"] for n in blocks["vlan20-iot"]["networks"]] == ["vlan20-iot"]
