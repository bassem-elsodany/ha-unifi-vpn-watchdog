"""Failover engine: health evaluation, quarantine with backoff, ordered switching, failback."""
from __future__ import annotations

import logging
import queue
import random
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .clock import Clock
from .config import Config, GroupCfg, GroupSettings, JobCfg, NetRef
from .ladder import candidates, expected_country, failback_targets, missing, position_label, position_of, resolve_order
from .models import ProbeResult, Route, Snapshot, Tunnel
from .notify import Notifier
from .probe import TunnelTester
from .refs import item_for, net_id, normalize
from .schedule import next_run, signature, summary
from .state import GroupState, StateStore
from .unifi import UniFiClient, UniFiError

log = logging.getLogger("vpn_watchdog.engine")


@dataclass
class Health:
    ok: bool
    hard: bool = False
    reasons: list[str] = field(default_factory=list)


class Engine:
    def __init__(
        self,
        cfg: Config,
        unifi: UniFiClient,
        tester: TunnelTester,
        notifier: Notifier,
        store: StateStore,
        clock: Clock,
    ):
        self.cfg = cfg
        self.unifi = unifi
        self.tester = tester
        self.notifier = notifier
        self.store = store
        self.clock = clock
        self.wake = threading.Event()
        self.last_tick: float | None = None
        self._snap: Snapshot | None = None
        self.last_error: str | None = None
        self._commands: queue.Queue[tuple] = queue.Queue()
        self._samples: dict[str, deque[tuple[int, int]]] = {}
        self._lock = threading.RLock()
        self._rng = random.Random()
        self.refs_changed = False        # ids or labels in the config were brought in line with UniFi: the app writes them back to config.yaml
        self._status: dict[str, Any] = {}
        self._overlap_warned: set[str] = set()
        self._noted: dict[str, str] = {}
        self._busy = False
        self._blocked_notified: dict[str, float] = {}
        self.listeners: list = []   # callables(status_dict) invoked after each tick (MQTT publisher)

    # ------------------------------------------------------------------ public API
    def submit(self, *command: Any) -> None:
        self._commands.put(command)
        self.wake.set()

    def last_snapshot(self) -> Snapshot | None:
        return self._snap

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._status)
        st["loading"] = self._busy or self.last_tick is None      # reading UniFi right now, or nothing read yet
        return st

    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg

    def tick(self) -> None:
        self._busy = True
        try:
            self._tick()
        finally:
            self._busy = False

    def _log_check(self, snap: Snapshot) -> None:
        parts = [f"read UniFi: {len(snap.tunnels)} VPN clients, {len(snap.routes)} policies, {len(snap.clients)} devices"]
        for g in self.cfg.groups:
            gs = self.store.group(g.name)
            cur = snap.tunnels.get(gs.current_id or "")
            parts.append(f"{g.name}: {gs.last_decision or 'no decision yet'}" + (f" (active {cur.name})" if cur else ""))
        msg = "; ".join(parts)
        log.debug("check: %s", msg)
        self.notifier.record_check(msg)

    def _tick(self) -> None:
        try:
            snap = self.unifi.snapshot()
        except UniFiError as e:
            self.last_error = str(e)
            log.error("cannot read UniFi state, skipping cycle: %s", e)
            self._publish(None)
            return
        self.last_error = None
        self._snap = snap
        if normalize(self.cfg, snap):                     # a rename in UniFi, or a config that only had names: follow the ids
            self.refs_changed = True
        self._drain_commands(snap)
        for g in self.cfg.groups:
            try:
                self._tick_group(g, snap)
            except UniFiError as e:
                self.last_error = str(e)
                log.error("group %s: UniFi error: %s", g.name, e)
            except Exception:  # noqa: BLE001 - one broken group must not stop the others
                log.exception("group %s: unexpected error", g.name)
        try:
            self._disconnect_unused(self.unifi.snapshot())
        except UniFiError as e:
            log.warning("disconnecting unused tunnels skipped: %s", e)
        if log.isEnabledFor(logging.DEBUG):       # DEBUG log level: every check cycle also shows up in the Events tab
            self._log_check(snap)
        self.last_tick = self.clock.now()
        self.store.save()
        self._publish(snap)

    # ------------------------------------------------------------------ per-group loop
    def _tick_group(self, g: GroupCfg, snap: Snapshot) -> None:
        st = self.cfg.settings_for(g)
        gs = self.store.group(g.name)
        ladder = resolve_order(g, list(snap.tunnels.values()))
        watch_only = not ladder
        note = ""
        if watch_only:
            gone = missing(g, list(snap.tunnels.values()))
            note = ("no fallback order set: choose the tunnels and their order in Settings > Fallback order" if not g.order
                    else f"none of the tunnels in the fallback order exist in UniFi: {', '.join(gone)}")
            if self._noted.get(g.name) != note:       # say it once, not every cycle
                self._noted[g.name] = note
                log.warning("group %s: %s", g.name, note)

        routes = self._group_routes(g, snap)
        active = next((r for r in routes if r.enabled), None)      # UniFi applies the first enabled policy in its list
        if active is None:
            if watch_only:
                gs.last_decision, gs.healthy = f"no routing policy is switched on for these networks; {note}", None
                return
            gs.last_decision = "no routing policy is switched on for these networks; choosing a tunnel"
            self._provision(g, st, gs, ladder, snap)
            return
        self._warn_overlaps(g, routes, snap)

        cur = snap.tunnels.get(active.network_id or "")
        cur_id = cur.id if cur else None
        if gs.current_id != cur_id:
            if gs.current_id is not None:
                log.warning("group %s: the active routing policy changed outside the watchdog (%s -> %s); adopting",
                            g.name, gs.current_id, cur_id)
            self._set_current(gs, cur_id)
        if gs.paused:
            gs.last_decision = "paused"
            self._maybe_rotate(g, st, gs, cur, ladder, None)       # the rotation job is its own job: it does not depend on failover being on
            return

        health = self._evaluate(g, st, gs, cur, snap)
        gs.healthy = health.ok
        now = self.clock.now()
        if health.ok:
            if gs.exhausted:
                gs.exhausted = False
                self.notifier.emit("recovered", f"VPN {g.name} recovered", f"{cur.name if cur else '?'} is healthy again", key=g.name,
                                   group=g.name, tunnel=cur.name if cur else "")
            ts = self.store.tunnel(cur_id) if cur_id else None
            if ts:
                ts.last_ok_ts = now
                if gs.since and now - gs.since >= st.failback.stable_seconds and ts.fail_streak:
                    ts.fail_streak = 0
                    self.store.touch()
            gs.last_decision = "healthy" if not watch_only else f"healthy (watching only) - {note}"
            if not watch_only:
                if not self._maybe_rotate(g, st, gs, cur, ladder, True):
                    self._maybe_failback(g, st, gs, cur, snap)
            return

        gs.last_decision = "unhealthy: " + "; ".join(health.reasons)
        down = (
            gs.status_failures >= st.detection.failure_threshold
            or gs.probe_failures >= st.detection.probe_failure_threshold
        )
        if not down:
            log.info("group %s: %s (status %d/%d, probe %d/%d)", g.name, gs.last_decision,
                     gs.status_failures, st.detection.failure_threshold,
                     gs.probe_failures, st.detection.probe_failure_threshold)
            return
        hard = gs.status_failures >= st.detection.failure_threshold
        log.warning("group %s: %s is DOWN: %s", g.name, cur.name if cur else "?", "; ".join(health.reasons))
        if watch_only:
            gs.last_decision = f"{cur.name if cur else 'the active tunnel'} is DOWN ({'; '.join(health.reasons)}) but nothing was switched - {note}"
            if not gs.exhausted:
                gs.exhausted = True
                self.notifier.emit("exhausted", f"VPN {g.name}: no working tunnel", gs.last_decision, level="critical", key=g.name,
                                   group=g.name, tunnel=cur.name if cur else "", tried=0)
            return
        self._failover(g, st, gs, cur, hard, "; ".join(health.reasons))

    # ------------------------------------------------------------------ health
    def _evaluate(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cur: Tunnel | None, snap: Snapshot) -> Health:
        if cur is None:
            gs.status_failures += 1
            return Health(False, True, ["route does not point at a known VPN tunnel"])
        reasons: list[str] = []
        conn = snap.connections.get(cur.id)
        if not cur.enabled:
            reasons.append("tunnel disabled")
        elif conn is None or not conn.connected:
            reasons.append(f"status={conn.status if conn else 'missing'}" + (f" {conn.notes}" if conn and conn.notes else ""))
        bh = st.detection.blackhole
        if not reasons and bh.enabled and conn is not None:
            dq = self._samples.setdefault(cur.id, deque(maxlen=bh.samples))
            dq.append((conn.rx_bps or 0, conn.tx_bps or 0))
            if dq.maxlen == len(dq) and all(rx == 0 and tx >= bh.min_tx_bps for rx, tx in dq):
                reasons.append(f"black hole: tx>={bh.min_tx_bps}bps but rx=0 for {bh.samples} polls")
        if reasons:
            gs.status_failures += 1
            return Health(False, False, reasons)
        gs.status_failures = 0

        now = self.clock.now()
        if self.tester.enabled and now - gs.last_probe_ts >= st.detection.probe_interval_seconds:
            gs.last_probe_ts = now
            res = self.tester.test(cur, snap, pre=False, expect=self._expect(g, snap, cur))
            if res is not None:
                gs.last_probe = res.as_dict()
                if res.ok:
                    gs.probe_failures = 0
                else:
                    gs.probe_failures += 1
                    if res.leak:
                        gs.probe_failures = max(gs.probe_failures, st.detection.probe_failure_threshold)
                        self.notifier.emit("leak", f"VPN LEAK on {g.name}", f"exit IP equals the WAN IP through {cur.name}", level="critical", key=g.name,
                                       group=g.name, tunnel=cur.name)
        if gs.probe_failures:
            last = (gs.last_probe or {}).get("reason", "probe failed")
            return Health(False, False, [f"probe: {last}"])
        return Health(True)

    # ------------------------------------------------------------------ switching
    def _failover(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cur: Tunnel | None,
                  hard: bool, why: str) -> None:
        if not self._rate_ok(g, st, gs, hard):
            return
        now = self.clock.now()
        if cur:
            ts = self.store.tunnel(cur.id)
            if ts.quarantined_until <= now:
                self._quarantine(cur, why, st)
        snap = self.unifi.snapshot()
        cands = candidates(g, list(snap.tunnels.values()), cur)
        tried = 0
        for cand in cands:
            if self.store.tunnel(cand.id).quarantined_until > now:
                continue
            tried += 1
            ok, reason = self._try(g, cand, st)
            if ok:
                self._commit(g, st, gs, cand, f"failover from {cur.name if cur else 'unknown'}: {why}", "switch")
                return
            self._quarantine(cand, reason, st)
            log.warning("group %s: candidate %s rejected: %s", g.name, cand.name, reason)
        if not gs.exhausted:
            gs.exhausted = True
            self.notifier.emit("exhausted", f"VPN {g.name}: no healthy tunnel",
                               f"{cur.name if cur else 'current'} is down and {tried} candidate(s) failed or are quarantined",
                               level="critical", key=g.name, group=g.name, tunnel=cur.name if cur else "", tried=tried)
        if st.switching.on_exhausted == "kill_switch":
            act = next((r for r in self._group_routes(g, self.unifi.snapshot()) if r.enabled), None)
            if act is not None:
                self.unifi.set_route(act, kill_switch=True)
        gs.last_decision = f"exhausted: {why}"

    def _position(self, g: GroupCfg, t: Tunnel | None) -> str:
        return position_label(g, t)

    def _expect(self, g: GroupCfg, snap: Snapshot, t: Tunnel) -> str | None:
        return expected_country(g, t)

    def _try(self, g: GroupCfg, cand: Tunnel, st: GroupSettings) -> tuple[bool, str]:
        ok, why = self._prepare(cand, st)
        if not ok:
            return False, why
        if self.tester.can_pretest:
            snap = self.unifi.snapshot()
            res = self.tester.test(cand, snap, pre=True, expect=self._expect(g, snap, cand))
            if res is not None and not res.ok:
                return False, f"pre-test failed: {res.reason}"
        return True, "ok"

    def _prepare(self, cand: Tunnel, st: GroupSettings) -> tuple[bool, str]:
        snap = self.unifi.snapshot()
        t = snap.tunnels.get(cand.id)
        if t is None:
            return False, "tunnel vanished"
        if not t.enabled:
            self.unifi.set_tunnel_enabled(cand.id, True)
        waited = 0.0
        while waited <= st.switching.connect_timeout_seconds:
            conn = self.unifi.snapshot().connections.get(cand.id)
            if conn and conn.connected:
                return True, "connected"
            self.clock.sleep(2)
            waited += 2
        return False, f"did not connect within {st.switching.connect_timeout_seconds:.0f}s"

    def _commit(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cand: Tunnel,
                reason: str, event: str) -> None:
        """Switch to `cand` by turning ITS routing policy on and then the others of this group off (make before break).
        Every tunnel keeps its own policy; nothing is renamed or re-pointed. A tunnel without one gets one created."""
        snap = self.unifi.snapshot()
        mine = next((r for r in self._group_routes(g, snap) if r.network_id == cand.id), None)
        if mine is None:
            nets = self._group_nets(g, snap)
            self.unifi.create_route(cand.name, cand.id, target_networks=nets, kill_switch=bool(g.kill_switch), enabled=True)
        else:
            self.unifi.set_route(mine, enabled=True, kill_switch=g.kill_switch)
        for r in self._group_routes(g, self.unifi.snapshot()):
            if r.network_id != cand.id and r.enabled:
                self.unifi.set_route(r, enabled=False)
        on = [r for r in self._group_routes(g, self.unifi.snapshot()) if r.enabled]
        if [r.network_id for r in on] != [cand.id]:
            raise UniFiError(f"the routing policy did not switch to {cand.name} (the controller did not apply the change)")
        now = self.clock.now()
        prev = gs.current_id
        self._set_current(gs, cand.id)
        gs.last_switch_ts = now
        gs.switches = [t for t in gs.switches if now - t < 3600] + [now]
        self.store.tunnel(cand.id).fail_streak = 0
        gs.exhausted = False
        gs.last_decision = f"switched to {cand.name}: {reason}"
        self.store.touch()
        old = self._snap.tunnels.get(prev or "") if self._snap else None
        self.notifier.emit(event, f"VPN {g.name} -> {cand.name}", reason, key=f"{g.name}:{cand.name}",
                           group=g.name, tunnel=cand.name, previous=old.name if old else (prev or ""),
                           position=self._position(g, cand), previous_position=self._position(g, old) if old else "", reason=reason)

    def _provision(self, g: GroupCfg, st: GroupSettings, gs: GroupState, ladder: list[Tunnel], snap: Snapshot) -> None:
        for cand in ladder:
            if self.store.tunnel(cand.id).quarantined_until > self.clock.now():
                continue
            ok, why = self._try(g, cand, st)
            if ok:
                self._commit(g, st, gs, cand, "initial choice: no routing policy was on", "switch")
                return
            self._quarantine(cand, why, st)

    # ------------------------------------------------------------------ rotation job
    def _rotation_on(self, g: GroupCfg) -> bool:
        job = self.cfg.job_for(g.name)
        return bool(job and job.enabled and not self.store.group(g.name).rotation_paused)

    def _maybe_rotate(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cur: Tunnel | None, ladder: list[Tunnel],
                      healthy: bool | None) -> bool:
        """Run the group's rotation job when it is due. Returns True when a rotation was attempted this cycle."""
        job = self.cfg.job_for(g.name)
        if job is None or not job.enabled:
            if gs.rotation_next_ts:
                gs.rotation_next_ts, gs.rotation_sig = 0.0, ""
                self.store.touch()
            return False
        now = self.clock.now()
        if gs.rotation_sig != signature(job) or not gs.rotation_next_ts:
            gs.rotation_sig, gs.rotation_next_ts = signature(job), next_run(job, gs.rotation_last_ts, now)
            self.store.touch()
        if gs.rotation_paused or cur is None or not ladder or healthy is False or now < gs.rotation_next_ts:
            return False
        self._rotate(g, st, gs, job, cur, ladder, "scheduled rotation")
        return True

    def _rotate(self, g: GroupCfg, st: GroupSettings, gs: GroupState, job: JobCfg, cur: Tunnel, ladder: list[Tunnel], why: str) -> bool:
        """Move to another tunnel of the group's order: the next one after the active tunnel, or a random one. Each candidate
        is connected and tested first; one that fails is skipped. If none passes, nothing moves."""
        now = self.clock.now()
        ids = [t.id for t in ladder]
        others = ladder[ids.index(cur.id) + 1:] + ladder[:ids.index(cur.id)] if cur.id in ids else list(ladder)
        if job.go_to == "random":
            self._rng.shuffle(others)
        skipped: list[str] = []
        for cand in others:
            if self.store.tunnel(cand.id).quarantined_until > now:
                skipped.append(f"{cand.name} (cooling down)")
                continue
            ok, reason = self._try(g, cand, st)
            if ok:
                msg = f"{why}, {summary(job).lower()}" + (f"; skipped {', '.join(skipped)}" if skipped else "")
                self._commit(g, st, gs, cand, msg, "rotation")
                self._rotation_done(gs, job, f"moved to {cand.name}")
                return True
            self._quarantine(cand, reason, st)
            skipped.append(f"{cand.name} ({reason})")
        reason = ("no other tunnel in the order: " + ("; ".join(skipped) if skipped else "the order has only this tunnel")) + "; nothing was moved"
        self.notifier.emit("rotation_failed", f"VPN {g.name}: rotation skipped", reason, key=f"{g.name}:rotation", group=g.name,
                           tunnel=cur.name, reason=reason)
        gs.last_decision = f"rotation skipped: {reason}"
        self._rotation_done(gs, job, "nothing moved: " + reason)
        return False

    def _rotation_done(self, gs: GroupState, job: JobCfg, result: str) -> None:
        now = self.clock.now()
        gs.rotation_last_ts, gs.rotation_last = now, result
        gs.rotation_next_ts = next_run(job, now, now)
        self.store.touch()

    def _rotation_info(self, g: GroupCfg, gs: GroupState, snap: Snapshot) -> dict[str, Any] | None:
        job = self.cfg.job_for(g.name)
        if job is None:
            return None
        now = self.clock.now()
        ladder = resolve_order(g, list(snap.tunnels.values()))
        cur = snap.tunnels.get(gs.current_id or "")
        nxt = None
        if job.go_to == "random":
            nxt = "a random one from the order"
        elif ladder:
            ids = [t.id for t in ladder]
            i = ids.index(cur.id) if cur and cur.id in ids else -1
            t = ladder[(i + 1) % len(ladder)]
            nxt = f"{position_label(g, t)} {t.name}"
        return {"enabled": job.enabled, "paused": gs.rotation_paused, "summary": summary(job), "go_to": job.go_to,
                "next_in": max(0, int(gs.rotation_next_ts - now)) if gs.rotation_next_ts else None, "next_ts": gs.rotation_next_ts or None,
                "next_to": nxt, "last_ts": gs.rotation_last_ts or None, "last": gs.rotation_last}

    # ------------------------------------------------------------------ failback
    def _maybe_failback(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cur: Tunnel | None,
                        snap: Snapshot) -> None:
        if not st.failback.enabled or cur is None or self._rotation_on(g):      # while a rotation job is on, it decides when to move
            return
        now = self.clock.now()
        better = [t for t in failback_targets(g, list(snap.tunnels.values()), cur)
                  if self.store.tunnel(t.id).quarantined_until <= now]
        if not better:
            gs.failback_target = None
            gs.failback_stable_since = None
            return
        if now - gs.last_failback_check < st.failback.check_interval_seconds:
            return
        gs.last_failback_check = now
        target = better[0]
        if gs.failback_target != target.id:
            gs.failback_target, gs.failback_stable_since = target.id, None
        ok, why = self._try(g, target, st)
        if not ok:
            gs.failback_stable_since = None
            self._quarantine(target, f"failback test: {why}", st)
            return
        if gs.failback_stable_since is None:
            gs.failback_stable_since = now
        if now - gs.failback_stable_since >= st.failback.stable_seconds and self._rate_ok(g, st, gs, hard=False):
            self._commit(g, st, gs, target, f"failback to a higher position after {st.failback.stable_seconds}s stable", "failback")

    # ------------------------------------------------------------------ one connected tunnel
    def _disconnect_unused(self, snap: Snapshot) -> None:
        """Only the tunnel in use stays connected. Every other tunnel in a fallback order is disconnected, because several
        tunnels up at once for the same VLAN lets traffic leave through different exit IPs. The only exception is the
        higher-up tunnel being tested for failback, for as long as that test runs."""
        keep: set[str] = set()
        managed: set[str] = set()
        for g in self.cfg.groups:
            managed.update(t.id for t in resolve_order(g, list(snap.tunnels.values())))
            gs = self.store.group(g.name)
            if gs.current_id:
                keep.add(gs.current_id)
            if gs.failback_target:
                keep.add(gs.failback_target)
        in_use = {r.network_id for r in snap.routes if r.enabled and r.network_id}   # e.g. the exit-IP test client's policy
        for tid in managed:
            t = snap.tunnels[tid]
            if t.enabled and tid not in keep and tid not in in_use:
                self.unifi.set_tunnel_enabled(tid, False)

    # ------------------------------------------------------------------ helpers
    def _quarantine(self, t: Tunnel, reason: str, st: GroupSettings) -> None:
        ts = self.store.tunnel(t.id)
        ts.fail_streak += 1
        q = st.switching.quarantine
        secs = min(q.max_seconds, q.base_seconds * (q.factor ** (ts.fail_streak - 1)))
        ts.quarantined_until = self.clock.now() + secs
        ts.last_reason = reason[:200]
        self.store.touch()
        log.info("tunnel %s quarantined for %ds: %s", t.name, secs, reason)

    def _rate_ok(self, g: GroupCfg, st: GroupSettings, gs: GroupState, hard: bool) -> bool:
        now = self.clock.now()
        recent = [t for t in gs.switches if now - t < 3600]
        reason = None
        if len(recent) >= st.switching.max_switches_per_hour:
            reason = f"already switched {len(recent)}x in the last hour"
        elif not hard and now - gs.last_switch_ts < st.switching.min_hold_seconds:
            reason = f"last switch was {now - gs.last_switch_ts:.0f}s ago (min hold {st.switching.min_hold_seconds}s)"
        if reason:
            gs.last_decision = f"switch blocked: {reason}"
            if now - self._blocked_notified.get(g.name, -1e12) > 600:
                self._blocked_notified[g.name] = now
                self.notifier.emit("blocked", f"VPN {g.name}: switch blocked", reason, level="warning", key=g.name,
                                   group=g.name, reason=reason)
            return False
        return True

    def _set_current(self, gs: GroupState, tid: str | None) -> None:
        gs.current_id = tid
        gs.since = self.clock.now()
        gs.status_failures = 0
        gs.probe_failures = 0
        gs.last_probe = None
        gs.last_probe_ts = 0.0
        gs.failback_stable_since = None
        self.store.touch()

    @staticmethod
    def _net_id(snap: Snapshot, ref: NetRef) -> str:
        nid = net_id(snap.networks, ref)
        if nid is None:
            raise UniFiError(f"network {ref.name or ref.id!r} not found in UniFi")
        return nid

    def _group_nets(self, g: GroupCfg, snap: Snapshot) -> list[str]:
        """The ids of the group's VLANs that UniFi still has. A deleted VLAN is dropped (and said once); none left is an error."""
        ids = []
        for ref in g.networks:
            nid = net_id(snap.networks, ref)
            if nid is not None:
                ids.append(nid)
            elif f"{g.name}:net:{ref.id or ref.name}" not in self._overlap_warned:
                self._overlap_warned.add(f"{g.name}:net:{ref.id or ref.name}")
                log.warning("group %s: the VLAN %r no longer exists in UniFi; ignoring it", g.name, ref.name or ref.id)
        if not ids:
            raise UniFiError(f"group {g.name!r}: none of its VLANs exist in UniFi")
        return ids

    def _group_routes(self, g: GroupCfg, snap: Snapshot) -> list[Route]:
        """The group's routing policies: one per tunnel, targeting at least the group's networks. The watchdog only turns
        them on and off (and creates a missing one); it never renames or re-points them."""
        want = frozenset(self._group_nets(g, snap))
        return [r for r in snap.routes if want <= r.target_networks and not r.target_macs and r.network_id in snap.tunnels]

    def _warn_overlaps(self, g: GroupCfg, routes: list[Route], snap: Snapshot) -> None:
        want = frozenset(self._group_nets(g, snap))
        mine = {r.id for r in routes}
        on = [r for r in routes if r.enabled]
        if len(on) > 1 and f"{g.name}:multi" not in self._overlap_warned:
            self._overlap_warned.add(f"{g.name}:multi")
            log.warning("group %s: %d routing policies are on at once (%s); UniFi applies the first in its list", g.name, len(on),
                        ", ".join(r.description for r in on))
        for r in snap.routes:
            if r.id not in mine and r.enabled and not r.target_macs and (r.target_networks & want):
                key = f"{g.name}:{r.id}"
                if key not in self._overlap_warned:
                    self._overlap_warned.add(key)
                    log.warning("group %s: enabled policy %r also targets the same network(s); the first policy in "
                                "UniFi's list wins, so one of them is being ignored", g.name, r.description)

    def _drain_commands(self, snap: Snapshot) -> None:
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle(cmd, snap)
            except Exception:  # noqa: BLE001
                log.exception("command %s failed", cmd)

    def _handle(self, cmd: tuple, snap: Snapshot) -> None:
        kind, group = cmd[0], cmd[1] if len(cmd) > 1 else None
        if group == "*" and kind in ("pause", "resume"):
            for x in self.cfg.groups:
                self._handle((kind, x.name), snap)
                self.store.group(x.name).rotation_paused = kind == "pause"       # "all" stops and starts every job
            return
        g = next((x for x in self.cfg.groups if x.name == group), None)
        if g is None:
            log.warning("command %s: unknown group %r", kind, group)
            return
        gs = self.store.group(g.name)
        if kind == "pause":
            gs.paused = True
            self.store.touch()
            log.info("group %s paused", g.name)
        elif kind == "resume":
            gs.paused = False
            self.store.touch()
            log.info("group %s resumed", g.name)
        elif kind in ("rotation-pause", "rotation-resume"):
            gs.rotation_paused = kind == "rotation-pause"
            self.store.touch()
            log.info("group %s: rotation %s", g.name, "paused" if gs.rotation_paused else "resumed")
        elif kind == "rotate":
            job, cur = self.cfg.job_for(g.name), snap.tunnels.get(gs.current_id or "")
            ladder = resolve_order(g, list(snap.tunnels.values()))
            if job is None or cur is None or not ladder:
                log.warning("group %s: rotate now needs a rotation job, an active tunnel and a fallback order", g.name)
                return
            self._rotate(g, self.cfg.settings_for(g), gs, job, cur, ladder, "rotate now")
        elif kind == "test":
            t = snap.tunnel_by_ref(cmd[2])
            if t is None:
                return
            ok, why = self._try(g, t, self.cfg.settings_for(g))
            self._record("test", f"{t.name}: {'OK' if ok else 'FAILED - ' + why}", "info" if ok else "warning")
        elif kind == "switch":
            name = cmd[2]
            target = snap.tunnel_by_ref(name)
            if target is None:
                log.warning("manual switch: tunnel %r not found", name)
                return
            st = self.cfg.settings_for(g)
            ok, why = self._try(g, target, st)
            if not ok:
                log.warning("manual switch to %s refused: %s", name, why)
                return
            self._commit(g, st, gs, target, "manual switch", "switch")

    # ------------------------------------------------------------------ status export
    def _map(self, snap: Snapshot, groups: dict[str, Any]) -> dict[str, Any]:
        """What the Status page draws: per group its VLANs (with their devices), the fallback lane (the user's order, with
        live status), the tunnels not in the order, and the VPN exit; plus the VLANs that have no VPN policy."""
        vpn_ids = set(snap.tunnels)
        lans = {nid for nid, i in snap.network_info.items() if i.get("purpose") in ("corporate", "guest")}      # UniFi's own type: LANs/VLANs only, never WAN, VPN or remote-user networks
        now = self.clock.now()
        # Which policy applies to a device: UniFi reads its policy list from the top and the first enabled one that catches
        # all of the device's internet traffic wins, whether it targets the device or the device's VLAN.
        total = len(snap.routes)

        def covers(r: Route, nid: str | None) -> bool:
            return nid is not None and (nid in r.target_networks or (r.all_clients and nid in lans))

        def dest(r: Route) -> dict[str, Any]:
            tun = snap.tunnels.get(r.network_id or "")
            return {"kind": "vpn" if tun else "normal", "tunnel": tun.name if tun else None, "tunnel_id": tun.id if tun else None, "policy": r.description,
                    "goes_to": snap.networks.get(r.network_id or "", "the normal internet connection")}

        def winner(mac: str, nid: str | None) -> int | None:
            return next((i for i, r in enumerate(snap.routes)
                         if r.enabled and r.matching == "INTERNET" and (mac in r.target_macs or covers(r, nid))), None)

        eff: dict[str, dict[str, Any]] = {}          # per device that has its own policy: what applies, and whether its own policy is the one
        for mac in sorted({m for r in snap.routes if r.enabled and r.matching == "INTERNET" for m in r.target_macs}):
            c = snap.clients.get(mac, {})
            win = winner(mac, snap.client_network(c))
            mine = next(i for i, r in enumerate(snap.routes) if r.enabled and r.matching == "INTERNET" and mac in r.target_macs)
            eff[mac] = {"own": mine, "win": win, "applied": win == mine}
        devs: dict[str, list[dict[str, Any]]] = {}
        for mac, c in snap.clients.items():
            e = eff.get(mac)
            b = dest(snap.routes[e["win"]]) | {"position": e["win"] + 1, "total": total} if e and e["applied"] else None
            devs.setdefault(snap.client_network(c) or "", []).append({
                "mac": mac, "name": c.get("name") or "", "ip": c.get("ip"), "rate_bps": c.get("rate_bps"),
                "active": (c.get("rate_bps") or 0) > 800, "wired": c.get("wired", False),
                "bypass": b, "overridden": bool(e and not e["applied"]),
            })

        own: list[dict[str, Any]] = []
        own_off: list[dict[str, Any]] = []
        carriers: dict[str, list[str]] = {}
        for mac, e in eff.items():
            c = snap.clients.get(mac, {})
            r = snap.routes[e["own"]]
            row = {"mac": mac, "name": c.get("name") or "", "ip": c.get("ip"), "network": snap.networks.get(snap.client_network(c) or "", c.get("network") or ""),
                   "position": e["own"] + 1, "total": total, **dest(r)}
            if e["applied"]:
                own.append(row)
                if row["tunnel"]:
                    carriers.setdefault(row["tunnel"], []).append(row["name"] or mac)
            else:
                w = snap.routes[e["win"]]
                own_off.append(row | {"applied": dest(w) | {"position": e["win"] + 1}})
        own_off.sort(key=lambda d: (d["name"] or d["mac"]).lower())
        own.sort(key=lambda d: (d["kind"] != "vpn", d["tunnel"] or "", (d["name"] or d["mac"]).lower()))

        def card(nid: str) -> dict[str, Any]:
            name = snap.networks.get(nid, nid)
            info = snap.network_info.get(nid, {})
            lst = sorted(devs.get(nid, []), key=lambda d: (d["bypass"] is None, -(d["rate_bps"] or 0), (d["name"] or d["ip"] or d["mac"]).lower()))
            return {"id": nid, "name": name, "vlan": info.get("vlan"), "subnet": info.get("subnet"), "count": len(lst),
                    "bypass_count": sum(1 for d in lst if d["bypass"]), "devices": lst}

        def tinfo(g: GroupCfg | None, t: Tunnel, active_id: str | None, policies: dict[str, Route]) -> dict[str, Any]:
            c = snap.connections.get(t.id)
            ts = self.store.tunnels.get(t.id)
            pol = policies.get(t.id)
            item = item_for(g, t.id, t.name) if g else None
            return {"id": t.id, "name": t.name, "position": position_of(g, t) if g else None, "active": t.id == active_id, "enabled": t.enabled,
                    "status": c.status if c else None, "rx_bps": c.rx_bps if c else None, "tx_bps": c.tx_bps if c else None,
                    "server": c.remote_ip if c else None, "has_policy": pol is not None, "policy_on": bool(pol and pol.enabled),
                    "policy": pol.description if pol else None, "kill_switch": bool(pol and pol.kill_switch),
                    "quarantined_for": max(0, int((ts.quarantined_until if ts else 0) - now)),
                    "last_reason": ts.last_reason if ts else "", "expect_country": item.expect_country if item else None,
                    "carries": carriers.get(t.name, [])}

        used: set[str] = set()
        out_groups: list[dict[str, Any]] = []
        whole = [r for r in snap.routes if r.enabled and r.matching == "INTERNET" and not r.target_macs]      # in UniFi's list order

        def applied(n: str) -> Route | None:
            """The policy UniFi applies to a VLAN: the first enabled one in its list that covers it."""
            return next((r for r in whole if covers(r, n)), None)

        for g in self.cfg.groups:
            try:
                nets = self._group_nets(g, snap)
                routes = self._group_routes(g, snap)
            except UniFiError:
                nets, routes = [], []
            policies = {r.network_id: r for r in routes if r.network_id}
            active_route = next((r for r in routes if r.enabled), None)
            active_id = active_route.network_id if active_route else None
            # What UniFi does wins over what the group lists: a VLAN of the group that UniFi routes elsewhere right now is drawn where it goes.
            nets = [n for n in nets if active_id is None or (applied(n) is not None and applied(n).network_id == active_id)]
            used.update(nets)
            order = resolve_order(g, list(snap.tunnels.values()))
            in_order = {t.id for t in order}
            pool = sorted((t for t in snap.tunnels.values() if t.id not in in_order), key=lambda t: t.name.lower())
            gstat = groups.get(g.name, {})
            act = snap.tunnels.get(active_id or "")
            ac = snap.connections.get(active_id or "")
            probe = gstat.get("last_probe") or {}
            out_groups.append({
                "name": g.name, "paused": gstat.get("paused", False), "healthy": gstat.get("healthy"), "decision": gstat.get("decision", ""),
                "exhausted": gstat.get("exhausted", False), "jobs": gstat.get("jobs", {}), "has_order": bool(g.order), "rotation": gstat.get("rotation"),
                "active": act.name if act else None, "networks": [card(n) for n in nets],
                "lane": ([tinfo(g, act, active_id, policies)] if act and act.id not in in_order else []) + [tinfo(g, t, active_id, policies) for t in order],
                "pool": [{"id": t.id, "name": t.name, "status": (snap.connections.get(t.id).status if snap.connections.get(t.id) else None),
                          "enabled": t.enabled} for t in pool],
                "exit": {"ip": probe.get("ip") if probe.get("ok") else None, "country": probe.get("country") if probe.get("ok") else None,
                         "server": ac.remote_ip if ac else None, "age": gstat.get("last_probe_age") if probe.get("ok") else None},
            })
        # What UniFi itself does is shown whether or not the watchdog manages it. For every VLAN that a VPN policy covers, the policy
        # that applies is the first enabled one in UniFi's list that covers it; VLANs that end up on the same VPN client share a block.
        vpn_pols = [r for r in snap.routes if not r.target_macs and r.network_id in snap.tunnels]
        by_tunnel: dict[str | None, list[str]] = {}
        for n in lans:
            if n in used:
                continue
            w = applied(n)
            if w is not None and w.network_id in snap.tunnels:
                by_tunnel.setdefault(w.network_id, []).append(n)
            elif w is None and any(covers(r, n) for r in vpn_pols):
                by_tunnel.setdefault(None, []).append(n)           # VPN policies exist for it, but none is switched on
        vlan_of = lambda n: (snap.network_info.get(n, {}).get("vlan") is None, snap.network_info.get(n, {}).get("vlan") or 0, snap.networks[n].lower())
        for key, nets in sorted(by_tunnel.items(), key=lambda kv: (kv[0] is None, min(vlan_of(n) for n in kv[1]))):
            nets.sort(key=vlan_of)
            used.update(nets)
            rs = [r for r in vpn_pols if any(covers(r, n) for n in nets)]
            policies = {r.network_id: r for r in rs}
            act = snap.tunnels.get(key or "")
            ac = snap.connections.get(act.id) if act else None
            rest = sorted((snap.tunnels[r.network_id] for r in rs if r.network_id and (not act or r.network_id != act.id)), key=lambda t: t.name.lower())
            out_groups.append({
                "name": " + ".join(snap.networks[n] for n in nets), "unmanaged": True, "paused": False,
                "healthy": bool(ac and ac.connected) if act else None, "decision": "", "exhausted": act is None, "jobs": {}, "has_order": False,
                "active": act.name if act else None, "networks": [card(n) for n in nets],
                "lane": [tinfo(None, act, act.id, policies)] if act else [],
                "pool": [{"id": t.id, "name": t.name, "status": (snap.connections.get(t.id).status if snap.connections.get(t.id) else None), "enabled": t.enabled} for t in rest],
                "exit": {"ip": None, "country": None, "server": ac.remote_ip if ac else None, "age": None},
            })
        if out_groups:                       # a tunnel that only carries devices with their own route still needs a card to draw the line to
            lane = out_groups[-1]["lane"]
            for name in carriers:
                tun = snap.tunnel_by_name(name)
                if tun and all(t["name"] != name for t in lane):
                    lane.append(tinfo(None, tun, None, {}))
        direct = [card(nid) for nid, name in snap.networks.items()
                  if nid in lans and nid not in used]
        direct.sort(key=lambda n: (n["vlan"] is None, n["vlan"] if n["vlan"] is not None else 0, n["name"].lower()))
        return {"groups": out_groups, "direct": direct, "own": own, "own_off": own_off, "wan_ip": snap.wan_ip}

    def _record(self, event: str, message: str, level: str = "info") -> None:
        rec = getattr(self.notifier, "record", None)
        if rec:
            rec(event, message, level)

    def _publish(self, snap: Snapshot | None) -> None:
        now = self.clock.now()
        groups: dict[str, Any] = {}
        tunnels: dict[str, Any] = {}
        direct: list[dict[str, Any]] = []
        if snap is not None:
            managed = {r.id: g.name for g in self.cfg.groups for r in self._group_routes(g, snap)}
            canary = self.cfg.probe.canary.route_description

            def describe(r) -> dict[str, Any]:
                return {"description": r.description, "enabled": r.enabled, "kill_switch": r.kill_switch,
                        "networks": sorted(snap.networks.get(n, n) for n in r.target_networks),
                        "clients": sorted(r.target_macs), "managed_by": managed.get(r.id),
                        "canary": r.description == canary}

            by_tunnel: dict[str, list[dict[str, Any]]] = {}
            for r in snap.routes:
                if r.network_id in snap.tunnels:
                    by_tunnel.setdefault(r.network_id, []).append(describe(r))
                else:
                    d = describe(r)
                    d["goes_to"] = snap.networks.get(r.network_id or "", r.network_id or "unknown")
                    direct.append(d)
            for t in snap.tunnels.values():
                c = snap.connections.get(t.id)
                ts = self.store.tunnels.get(t.id)
                tunnels[t.name] = {"routes": by_tunnel.get(t.id, []),
                    "enabled": t.enabled,
                    "status": c.status if c else None, "rx_bps": c.rx_bps if c else None, "tx_bps": c.tx_bps if c else None,
                    "quarantined_for": max(0, int((ts.quarantined_until if ts else 0) - now)),
                    "last_reason": ts.last_reason if ts else "",
                }
            for g in self.cfg.groups:
                gs = self.store.group(g.name)
                cur = snap.tunnels.get(gs.current_id or "")
                groups[g.name] = {
                    "active": cur.name if cur else None,
                    "position": self._position(g, cur),
                    "healthy": gs.healthy is True,
                    "decision": gs.last_decision,
                    "paused": gs.paused,
                    "rotation": self._rotation_info(g, gs, snap),
                    "exhausted": gs.exhausted,
                    "status_failures": gs.status_failures,
                    "probe_failures": gs.probe_failures,
                    "last_probe": gs.last_probe,
                    "last_probe_age": int(now - gs.last_probe_ts) if gs.last_probe_ts else None,
                    "switches_last_hour": len([t for t in gs.switches if now - t < 3600]),
                    "order": [t.name for t in resolve_order(g, list(snap.tunnels.values()))],
                    "jobs": {
                        "next_probe_in": max(0, int(gs.last_probe_ts + self.cfg.settings_for(g).detection.probe_interval_seconds - now)) if self.tester.enabled else None,
                        "next_failback_check_in": max(0, int(gs.last_failback_check + self.cfg.settings_for(g).failback.check_interval_seconds - now)) if (self.cfg.settings_for(g).failback.enabled and g.order) else None,
                        "failback_target": (snap.tunnels[gs.failback_target].name if gs.failback_target in snap.tunnels else None),
                        "failback_stable_for": int(now - gs.failback_stable_since) if gs.failback_stable_since else None,
                    },
                }
        status = {
            "last_tick": self.last_tick, "error": self.last_error,
            "groups": groups, "tunnels": tunnels, "direct_routes": direct,
            "map": self._map(snap, groups) if snap is not None else {"groups": [], "direct": [], "own": [], "own_off": [], "wan_ip": None},
            "events": sorted(list(getattr(self.notifier, "history", []))[:60] + list(getattr(self.notifier, "checks", [])), key=lambda e: -e["ts"])[:100],
            "interval_seconds": self.cfg.interval_seconds,
            "version": __version__,
        }
        with self._lock:
            self._status = status
        for fn in self.listeners:
            try:
                fn(status)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")
