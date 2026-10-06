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


def test_exhausted_alerts_once_and_can_engage_kill_switch(make_engine):
    eng, un, _, notes, clock = make_engine(extra_switching=", on_exhausted: kill_switch")
    for n in ("Home-Primary", "Home-Backup", "Cousin vpn 2",
              "office/berlin", "office/frankfurt", "zzz-last-resort"):
        un.dead.add(tid(n))
    run(eng, clock, 8)
    assert notes.kinds().count("exhausted") == 1
    assert un.active_route().kill_switch is True
    assert active_name(un) == "Home-Primary"       # nothing better to move to


def test_pause_and_manual_switch(make_engine):
    eng, un, _, _, clock = make_engine()
    eng.submit("pause", "g1")
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 5)
    assert active_name(un) == "Home-Primary"      # paused: no automatic action
    eng.submit("resume", "g1")
    eng.submit("switch", "g1", "zzz-last-resort")
    run(eng, clock, 1)
    assert active_name(un) == "zzz-last-resort"


def test_external_route_change_is_adopted(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 2)
    import dataclasses
    un.routes = [dataclasses.replace(r, enabled=(r.description == "office/berlin")) for r in un.routes]   # someone flips policies in UniFi
    un.enabled.add(tid("office/berlin"))
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


def test_watch_only_still_tracks_the_active_tunnel_and_reports_health(make_engine):
    eng, un, _, notes, clock = make_engine(order="[]")
    run(eng, clock, 2)
    g = eng.status()["groups"]["g1"]
    assert g["active"] == "Home-Primary" and g["healthy"] is True and "no fallback order set" in g["decision"]
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 4)
    g = eng.status()["groups"]["g1"]
    assert "nothing was switched" in g["decision"] and "exhausted" in notes.kinds() and un.calls == []


def test_switching_only_turns_each_tunnels_own_policy_on_and_off(make_engine):
    """Regression: the watchdog re-pointed and renamed ONE policy, so the previous tunnel's policy 'disappeared'."""
    eng, un, _, _, clock = make_engine()
    before = sorted((r.id, r.description, r.network_id) for r in un.routes)
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    assert active_name(un) == "Home-Backup"
    assert sorted((r.id, r.description, r.network_id) for r in un.routes) == before      # same policies, same names, same targets
    assert len(un.routes) == 6 and not any(c[0] == "create" for c in un.calls)
    primary = next(r for r in un.routes if r.description == "Home-Primary")
    assert primary.enabled is False                                                      # still there, just off


def test_make_before_break_new_policy_is_on_before_the_old_one_is_turned_off(make_engine):
    eng, un, _, _, clock = make_engine()
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 3)
    seq = [(c[1], c[2]) for c in un.calls if c[0] == "route" and c[2] is not None]
    assert seq.index(("Home-Backup", True)) < seq.index(("Home-Primary", False))


def test_a_tunnel_without_a_policy_gets_one_created_named_after_it(make_engine):
    eng, un, _, notes, clock = make_engine(with_policies=False)
    run(eng, clock, 2)
    assert [c for c in un.calls if c[0] == "create"] == [("create", "Home-Primary", True)]
    assert active_name(un) == "Home-Primary"


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
