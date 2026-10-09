"""The rotation job: every so often move a group to another tunnel of its order, testing it first."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import ORDER, active_name, run, tid
from vpn_watchdog.config import ConfigError, JobCfg, parse_config
from vpn_watchdog.schedule import next_run, summary
from vpn_watchdog import settings as settings_mod


def hours(n=1, go_to="next", **kw):
    return JobCfg(group="g1", every=n, unit="hours", go_to=go_to, **kw)


def test_summary_and_next_run():
    assert summary(JobCfg(group="g", every=1, unit="days", at="3:00")) == "Every 1 day at 03:00"
    assert summary(JobCfg(group="g", every=2, unit="hours")) == "Every 2 hours"
    assert summary(JobCfg(group="g", every=1, unit="weeks", at="04:30")) == "Every 1 week at 04:30"
    # hours count from the start, or from the last run
    assert next_run(hours(2), 0, 1000) == 1000 + 7200 and next_run(hours(2), 5000, 9999) == 5000 + 7200
    # days: today at the time if it is still ahead, otherwise tomorrow; then one period after each run
    now = datetime(2026, 10, 6, 12, 0).timestamp()
    job = JobCfg(group="g", every=1, unit="days", at="03:00")
    assert datetime.fromtimestamp(next_run(job, 0, now)) == datetime(2026, 10, 7, 3, 0)
    assert datetime.fromtimestamp(next_run(JobCfg(group="g", every=1, unit="days", at="18:00"), 0, now)) == datetime(2026, 10, 6, 18, 0)
    assert datetime.fromtimestamp(next_run(job, datetime(2026, 10, 7, 3, 0, 5).timestamp(), now)) == datetime(2026, 10, 8, 3, 0)
    week = JobCfg(group="g", every=2, unit="weeks", at="03:00")
    assert datetime.fromtimestamp(next_run(week, 0, now)) == datetime(2026, 10, 7, 3, 0) + timedelta(days=13)


def test_job_validation():
    base = "interval_seconds: 15\nstate_file: /tmp/x\nunifi: {api_key: x}\ngroups:\n  - {name: g1, networks: [a]}\n"
    assert parse_config(base + "jobs:\n  - {group: g1, every: 2, unit: days, at: '4:05'}\n", env={}).jobs[0].at == "04:05"
    for bad in ("jobs:\n  - {group: nope}\n", "jobs:\n  - {group: g1}\n  - {group: g1}\n", "jobs:\n  - {group: g1, at: '25:00'}\n",
                "jobs:\n  - {group: g1, every: 0}\n", "jobs:\n  - {group: g1, unit: minutes}\n"):
        with pytest.raises(ConfigError):
            parse_config(base + bad, env={})


def test_rotation_moves_to_the_next_tunnel_when_due_and_not_before(make_engine):
    eng, un, tester, notes, clock = make_engine()
    eng.cfg.jobs.append(hours(1))
    run(eng, clock, 1)
    assert active_name(un) == "Home-Primary" and "rotation" not in notes.kinds()
    clock.t += 3000
    run(eng, clock, 1)
    assert active_name(un) == "Home-Primary"                    # not due yet
    clock.t += 700
    run(eng, clock, 1)
    assert active_name(un) == "Home-Backup" and notes.kinds().count("rotation") == 1
    assert ("Home-Backup", True) in tester.tested               # the target was tested before it carried traffic
    gs = eng.store.group("g1")
    assert gs.rotation_next_ts == pytest.approx(gs.rotation_last_ts + 3600)
    clock.t += 3601
    run(eng, clock, 1)
    assert active_name(un) == "Cousin vpn 2"                    # then the next one again, following the order


def test_rotation_wraps_around_to_the_first_and_skips_a_tunnel_that_fails_its_test(make_engine):
    eng, un, tester, notes, clock = make_engine(active="zzz-last-resort", order="[Home-Primary, Home-Backup, zzz-last-resort]")
    eng.cfg.jobs.append(hours(1))
    tester.bad.add(tid("Home-Primary"))
    run(eng, clock, 1)
    clock.t += 3700
    run(eng, clock, 1)
    assert active_name(un) == "Home-Backup"                     # wrapped around, skipped the failing #1
    assert any("skipped Home-Primary" in m for k, m in notes.events if k == "rotation") or notes.kinds().count("rotation") == 1


def test_rotation_with_no_working_tunnel_moves_nothing_and_says_so(make_engine):
    eng, un, tester, notes, clock = make_engine(order="[Home-Primary, Home-Backup]")
    eng.cfg.jobs.append(hours(1))
    tester.bad.add(tid("Home-Backup"))
    run(eng, clock, 1)
    clock.t += 3700
    run(eng, clock, 1)
    assert active_name(un) == "Home-Primary" and "rotation_failed" in notes.kinds() and "rotation" not in notes.kinds()
    assert eng.store.group("g1").rotation_next_ts > clock.t     # tries again at the next time, not every cycle


def test_failback_is_off_while_rotation_is_on(make_engine):
    eng, un, tester, notes, clock = make_engine(active="Home-Backup", enabled={"Home-Backup"})
    run(eng, clock, 12)
    assert active_name(un) == "Home-Primary"                    # without a rotation job it fails back up (control)
    eng2, un2, _, notes2, clock2 = make_engine(active="Home-Backup", enabled={"Home-Backup"})
    eng2.cfg.jobs.append(hours(24))
    run(eng2, clock2, 12)
    assert active_name(un2) == "Home-Backup" and "failback" not in notes2.kinds()


def test_failover_still_works_with_a_rotation_job(make_engine):
    eng, un, tester, notes, clock = make_engine()
    eng.cfg.jobs.append(hours(24))
    run(eng, clock, 2)
    un.dead.add(tid("Home-Primary"))
    run(eng, clock, 6)
    assert active_name(un) == "Home-Backup" and "switch" in notes.kinds()


def test_stop_rotation_pauses_only_this_job_and_rotate_now_runs_it(make_engine):
    eng, un, tester, notes, clock = make_engine()
    eng.cfg.jobs.append(hours(1))
    run(eng, clock, 1)
    eng.submit("rotation-pause", "g1")
    clock.t += 4000
    run(eng, clock, 1)
    assert active_name(un) == "Home-Primary" and eng.store.group("g1").paused is False
    eng.submit("rotation-resume", "g1")
    eng.submit("rotate", "g1")
    run(eng, clock, 1)
    assert active_name(un) == "Home-Backup"
    info = eng.status()["groups"]["g1"]["rotation"]
    assert info["summary"] == "Every 1 hour" and info["next_in"] is not None and info["last_ts"]
    assert eng.status()["map"]["groups"][0]["rotation"]["paused"] is False


def test_random_rotation_picks_another_tunnel_of_the_order(make_engine):
    eng, un, tester, notes, clock = make_engine(order="[Home-Primary, Home-Backup, zzz-last-resort]")
    eng.cfg.jobs.append(hours(1, go_to="random"))
    run(eng, clock, 1)
    clock.t += 3700
    run(eng, clock, 1)
    assert active_name(un) in ("Home-Backup", "zzz-last-resort")
    assert eng.status()["groups"]["g1"]["rotation"]["next_to"] == "a random one from the order"


def test_rotation_runs_while_failover_is_stopped(make_engine):
    eng, un, tester, notes, clock = make_engine()
    eng.cfg.jobs.append(hours(1))
    eng.submit("pause", "g1")
    run(eng, clock, 1)
    clock.t += 3700
    run(eng, clock, 1)
    assert active_name(un) == "Home-Backup"


def test_settings_form_round_trip_for_jobs():
    raw = {"groups": [{"name": "g1", "networks": ["a"]}, {"name": "g2", "networks": ["b"]}], "jobs": []}
    form = {"groups": [{"name": "g1", "_orig": "g1", "networks": ["a"], "order": []}, {"name": "renamed", "_orig": "g2", "networks": ["b"], "order": []}],
            "jobs": [{"kind": "rotation", "group": "g2", "enabled": True, "every": 2, "unit": "days", "at": "04:00", "go_to": "random"},
                     {"kind": "rotation", "group": "gone", "enabled": True, "every": 1, "unit": "hours", "at": "03:00", "go_to": "next"}]}
    out = settings_mod.apply(raw, form)
    assert out["jobs"] == [{"kind": "rotation", "group": "renamed", "enabled": True, "every": 2, "unit": "days", "at": "04:00", "go_to": "random"}]
