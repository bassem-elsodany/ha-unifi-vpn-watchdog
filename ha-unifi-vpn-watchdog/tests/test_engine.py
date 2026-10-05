from conftest import active_name, run, tid


def test_healthy_tunnel_causes_no_switch(make_engine):
    eng, un, _, notes, clock = make_engine()
    run(eng, clock, 6)
    assert active_name(un) == "IT__ROME__418__1.1.1.1"
    assert "switch" not in notes.kinds()


def test_failure_below_threshold_does_not_switch(make_engine):
    eng, un, _, notes, clock = make_engine()
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 2)
    assert active_name(un) == "IT__ROME__418__1.1.1.1"


def test_switches_to_other_city_in_same_country_first(make_engine):
    eng, un, _, notes, clock = make_engine()
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 3)
    # Milan (different city) is preferred over Rome 511 (same city) when failing over inside Italy.
    assert active_name(un) == "IT__MILAN__244__1.1.1.3"
    assert notes.kinds().count("switch") == 1


def test_whole_country_down_falls_to_next_country(make_engine):
    eng, un, _, notes, clock = make_engine()
    for n in ("IT__ROME__418__1.1.1.1", "IT__ROME__511__1.1.1.2", "IT__MILAN__244__1.1.1.3"):
        un.dead.add(tid(n))
    run(eng, clock, 3)
    assert active_name(un).startswith("DE__")


def test_candidate_that_never_connects_is_skipped_and_quarantined(make_engine):
    eng, un, _, _, clock = make_engine()
    un.dead.update({tid("IT__ROME__418__1.1.1.1"), tid("IT__MILAN__244__1.1.1.3")})
    run(eng, clock, 3)
    assert active_name(un) == "IT__ROME__511__1.1.1.2"
    assert eng.store.tunnel(tid("IT__MILAN__244__1.1.1.3")).quarantined_until > clock.now()


def test_pretest_failure_skips_candidate(make_engine):
    eng, un, tester, _, clock = make_engine()
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    tester.bad.add(tid("IT__MILAN__244__1.1.1.3"))
    run(eng, clock, 3)
    assert active_name(un) == "IT__ROME__511__1.1.1.2"
    assert ("IT__MILAN__244__1.1.1.3", True) in tester.tested


def test_quarantine_backoff_grows(make_engine):
    eng, un, _, _, clock = make_engine()
    t = tid("IT__ROME__511__1.1.1.2")
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
    un.muted.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 6 + 3)   # 6 samples to fill the window, then 3 failing polls
    assert active_name(un) != "IT__ROME__418__1.1.1.1"


def test_probe_failure_triggers_failover(make_engine):
    eng, un, tester, _, clock = make_engine()
    tester.bad.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 4)
    assert active_name(un) != "IT__ROME__418__1.1.1.1"


def test_leak_fails_over_immediately_and_alerts(make_engine):
    eng, un, tester, notes, clock = make_engine(extra_switching=", min_hold_seconds: 0")
    tester.leak.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 1)
    assert "leak" in notes.kinds()
    assert active_name(un) != "IT__ROME__418__1.1.1.1"


def test_failback_waits_for_stability(make_engine):
    eng, un, _, notes, clock = make_engine()
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 3)
    assert active_name(un) == "IT__MILAN__244__1.1.1.3"
    un.dead.clear()                       # Rome recovers
    run(eng, clock, 6, step=15)            # quarantine (120s) still running -> no failback
    assert active_name(un) == "IT__MILAN__244__1.1.1.3"
    run(eng, clock, 20, step=15)           # quarantine over, then 60s stable
    assert active_name(un) == "IT__ROME__418__1.1.1.1"
    assert "failback" in notes.kinds()


def test_failback_disabled(make_engine):
    eng, un, _, _, clock = make_engine(failback=False)
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 3)
    un.dead.clear()
    run(eng, clock, 40)
    assert active_name(un) == "IT__MILAN__244__1.1.1.3"


def test_max_switches_per_hour_blocks_flapping(make_engine):
    eng, un, _, notes, clock = make_engine(extra_switching=", max_switches_per_hour: 1, min_hold_seconds: 0")
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 3)
    first = active_name(un)
    un.dead.add(tid(first))
    run(eng, clock, 6)
    assert active_name(un) == first          # blocked: it must not switch a second time this hour
    assert "blocked" in notes.kinds()


def test_min_hold_blocks_soft_failures_but_not_hard(make_engine):
    eng, un, tester, _, clock = make_engine()
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
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
    for n in ("IT__ROME__418__1.1.1.1", "IT__ROME__511__1.1.1.2", "IT__MILAN__244__1.1.1.3",
              "DE__BERLIN__1552__2.2.2.1", "DE__FRANKFURT__1287__2.2.2.2", "FR__PARIS__953__3.3.3.1"):
        un.dead.add(tid(n))
    run(eng, clock, 8)
    assert notes.kinds().count("exhausted") == 1
    assert un.route.kill_switch is True
    assert active_name(un) == "IT__ROME__418__1.1.1.1"       # nothing better to move to


def test_pause_and_manual_switch(make_engine):
    eng, un, _, _, clock = make_engine()
    eng.submit("pause", "g1")
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 5)
    assert active_name(un) == "IT__ROME__418__1.1.1.1"      # paused: no automatic action
    eng.submit("resume", "g1")
    eng.submit("switch", "g1", "FR__PARIS__953__3.3.3.1")
    run(eng, clock, 1)
    assert active_name(un) == "FR__PARIS__953__3.3.3.1"


def test_external_route_change_is_adopted(make_engine):
    eng, un, _, _, clock = make_engine()
    run(eng, clock, 2)
    un.route = type(un.route)(**{**un.route.__dict__, "network_id": tid("DE__BERLIN__1552__2.2.2.1"),
                                 "description": "DE__BERLIN__1552__2.2.2.1"})
    un.enabled.add(tid("DE__BERLIN__1552__2.2.2.1"))
    run(eng, clock, 2)
    assert eng.store.group("g1").current_id == tid("DE__BERLIN__1552__2.2.2.1")


def test_standby_keeps_active_plus_warm_and_disables_the_rest(make_engine):
    eng, un, _, _, clock = make_engine(enabled=set(__import__("conftest").TUNNELS))
    run(eng, clock, 2)
    on = {i for i in un.enabled}
    assert tid("IT__ROME__418__1.1.1.1") in on                 # active
    assert len(on) == 2                                        # active + 1 warm
    assert tid("FR__PARIS__953__3.3.3.1") not in on


def test_no_probe_mode_still_fails_over_on_status(make_engine):
    eng, un, _, _, clock = make_engine(tester_enabled=False, pretest=False)
    un.dead.add(tid("IT__ROME__418__1.1.1.1"))
    run(eng, clock, 3)
    assert active_name(un) != "IT__ROME__418__1.1.1.1"


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
