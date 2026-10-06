"""Failover engine: health evaluation, quarantine with backoff, ordered switching, failback.

The only thing it ever changes in UniFi is whether a VPN client is switched on or off. Routing policies (which VLANs and devices go
through which client) are UniFi's own business: the engine reads them for the Status page and never writes them."""
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
from .config import Config, GroupCfg, GroupSettings, JobCfg
from .ladder import candidates, expected_country, failback_targets, missing, position_label, position_of, resolve_order
from .models import ProbeResult, Route, Snapshot, Tunnel
from .notify import Notifier
from .probe import TunnelTester
from .refs import item_for, normalize
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
        self._owners: dict[str, str] = {}           # VPN client id -> the group it belongs to (a client belongs to one group)
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
        self._owners = self._claims(snap)
        if self._drain_commands(snap):                     # a command switched clients on or off: look again before deciding anything
            snap = self.unifi.snapshot()
            self._snap = snap
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
    def _claims(self, snap: Snapshot) -> dict[str, str]:
        """Which group owns which VPN client. A client belongs to one group: if two groups list it, the first group keeps it."""
        owners: dict[str, str] = {}
        for g in self.cfg.groups:
            for t in resolve_order(g, list(snap.tunnels.values())):
                if t.id in owners and owners[t.id] != g.name:
                    key = f"dup:{t.id}:{g.name}"
                    if key not in self._overlap_warned:
                        self._overlap_warned.add(key)
                        log.warning("VPN client %r is in groups %r and %r; it stays with %r", t.name, owners[t.id], g.name, owners[t.id])
                    continue
                owners[t.id] = g.name
        return owners

    def _pool(self, g: GroupCfg, snap: Snapshot) -> list[Tunnel]:
        """The group's VPN clients in order, minus any that another group already owns."""
        return [t for t in resolve_order(g, list(snap.tunnels.values())) if self._owners.get(t.id) in (None, g.name)]

    def _tick_group(self, g: GroupCfg, snap: Snapshot) -> None:
        st = self.cfg.settings_for(g)
        gs = self.store.group(g.name)
        ladder = self._pool(g, snap)
        watch_only = not ladder
        note = ""
        if watch_only:
            gone = missing(g, list(snap.tunnels.values()))
            note = ("no fallback order set: choose the tunnels and their order in Settings > Fallback order" if not g.order
                    else f"none of the tunnels in the fallback order exist in UniFi: {', '.join(gone)}")
            if self._noted.get(g.name) != note:       # say it once, not every cycle
                self._noted[g.name] = note
                log.warning("group %s: %s", g.name, note)

        on = [t for t in ladder if t.enabled]                        # the group's VPN clients that are switched on in UniFi
        if not on:
            if watch_only:
                gs.last_decision, gs.healthy = f"no VPN client is switched on; {note}", None
                return
            gs.last_decision = "none of this group's VPN clients is switched on; choosing one"
            self._provision(g, st, gs, ladder, snap)
            return
        # the one in use: the one this group chose last if it is still on, otherwise the highest one in the order
        cur = next((t for t in on if t.id == gs.current_id), None) or on[0]
        cur_id = cur.id
        if gs.current_id != cur_id:
            if gs.current_id is not None:
                log.warning("group %s: the VPN client in use changed outside the watchdog (%s -> %s); adopting", g.name, gs.current_id, cur_id)
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
        """Make `cand` the group's VPN client in use. `cand` is already switched on and connected (_try did that); the client it
        replaces is switched off at the end of the cycle. No routing policy is touched: UniFi's own policies send the traffic
        through whichever client is up."""
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
                self._commit(g, st, gs, cand, "initial choice: none of the group's VPN clients was switched on", "switch")
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
            managed.update(t.id for t in self._pool(g, snap))
            gs = self.store.group(g.name)
            if gs.current_id:
                keep.add(gs.current_id)
            if gs.failback_target:
                keep.add(gs.failback_target)
        in_use = {r.network_id for r in snap.routes if r.enabled and r.target_macs and r.network_id}   # clients that carry a device's own route (and the exit-IP test device)
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

    def _drain_commands(self, snap: Snapshot) -> bool:
        did = False
        while True:
            try:
                cmd = self._commands.get_nowait()
            except queue.Empty:
                return did
            did = True
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

        def live(r: Route) -> bool:
            """A policy UniFi really applies: switched on, catching all internet traffic, and (if it goes to a VPN client) that client is on."""
            t = snap.tunnels.get(r.network_id or "")
            return r.enabled and r.matching == "INTERNET" and (t is None or t.enabled)

        def winner(mac: str, nid: str | None) -> int | None:
            return next((i for i, r in enumerate(snap.routes) if live(r) and (mac in r.target_macs or covers(r, nid))), None)

        eff: dict[str, dict[str, Any]] = {}          # per device that has its own policy: what applies, and whether its own policy is the one
        for mac in sorted({m for r in snap.routes if live(r) for m in r.target_macs}):
            c = snap.clients.get(mac, {})
            win = winner(mac, snap.client_network(c))
            mine = next(i for i, r in enumerate(snap.routes) if live(r) and mac in r.target_macs)
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
                    "policy": pol.description if pol else None,
                    "quarantined_for": max(0, int((ts.quarantined_until if ts else 0) - now)),
                    "last_reason": ts.last_reason if ts else "", "expect_country": item.expect_country if item else None,
                    "carries": carriers.get(t.name, [])}

        used: set[str] = set()
        out_groups: list[dict[str, Any]] = []
        whole = [r for r in snap.routes if live(r) and not r.target_macs]      # in UniFi's list order

        def applied(n: str) -> Route | None:
            """The policy UniFi applies to a VLAN: the first live one in its list that covers it."""
            return next((r for r in whole if covers(r, n)), None)

        vpn_pols = [r for r in snap.routes if not r.target_macs and r.network_id in snap.tunnels]
        pol_of: dict[str, Route] = {}                                          # a routing policy of each VPN client (a switched-on one first)
        for r in sorted(vpn_pols, key=lambda r: not r.enabled):
            pol_of.setdefault(r.network_id or "", r)
        by_tunnel: dict[str | None, list[str]] = {}                            # VLANs grouped by the VPN client UniFi sends them through
        for n in lans:
            w = applied(n)
            if w is not None and w.network_id in snap.tunnels:
                by_tunnel.setdefault(w.network_id, []).append(n)
            elif w is None and any(covers(r, n) for r in vpn_pols):
                by_tunnel.setdefault(None, []).append(n)           # VPN policies exist for it, but none of them is live
        vlan_of = lambda n: (snap.network_info.get(n, {}).get("vlan") is None, snap.network_info.get(n, {}).get("vlan") or 0, snap.networks[n].lower())

        declared: dict[str, list[str]] = {}          # the VLANs each group is for, as picked in Settings (a VLAN belongs to the first group that picked it)
        claimed: set[str] = set()
        for g in self.cfg.groups:
            ids = [n.id for n in g.networks if n.id in lans and n.id not in claimed]
            declared[g.name] = sorted(ids, key=vlan_of)
            claimed.update(ids)
        for lst in by_tunnel.values():
            lst[:] = [n for n in lst if n not in claimed]
        used.update(claimed)
        plan: dict[str, tuple[list[Tunnel], Tunnel | None, list[str], list[str]]] = {}
        for g in self.cfg.groups:                                              # the VLANs UniFi really sends through each group's switched-on client
            order = self._pool(g, snap)
            gs = self.store.group(g.name)
            act = snap.tunnels.get(gs.current_id or "")
            if act is not None and (not act.enabled or act.id not in {t.id for t in order}):
                act = None
            decl = declared[g.name]
            if decl:
                live_nets = [n for n in decl if act and (w := applied(n)) is not None and w.network_id == act.id]
                plan[g.name] = (order, act, live_nets, [n for n in decl if n not in live_nets])
                continue
            live_nets = sorted(by_tunnel.pop(act.id, []), key=vlan_of) if act else []
            plan[g.name] = (order, act, live_nets, [])
            used.update(live_nets)
        for g in self.cfg.groups:                                              # a group that picked no VLAN: the VLANs only a policy of one of its clients names
            order, act, live_nets, _ = plan[g.name]
            if declared[g.name]:
                continue
            ids = {t.id for t in order}
            elsewhere = {n for k, v in by_tunnel.items() if k is not None for n in v}      # UniFi really sends these through another client
            extra = [n for n in lans if n not in used and n not in elsewhere and any(r.network_id in ids and covers(r, n) for r in vpn_pols)]
            for n in extra:
                for lst in by_tunnel.values():
                    if n in lst:
                        lst.remove(n)
            used.update(extra)
            plan[g.name] = (order, act, live_nets, sorted(extra, key=vlan_of))
        for g in self.cfg.groups:                                              # a group is drawn around its VLANs
            order, act, live_nets, extra_nets = plan[g.name]
            in_order = {t.id for t in order}
            gs = self.store.group(g.name)
            active_id = act.id if act else None
            nets = live_nets + extra_nets
            pool = sorted((t for t in snap.tunnels.values() if t.id not in in_order and self._owners.get(t.id) in (None, g.name)), key=lambda t: t.name.lower())
            gstat = groups.get(g.name, {})
            ac = snap.connections.get(active_id or "")
            probe = gstat.get("last_probe") or {}
            gaps = []
            for n in declared[g.name]:
                w = applied(n)
                if w is None or w.network_id not in snap.tunnels:
                    gaps.append(f"{snap.networks[n]} is not routed through any VPN client right now, so it goes straight to the internet")
                elif w.network_id not in in_order:
                    gaps.append(f"{snap.networks[n]} is routed through {snap.tunnels[w.network_id].name}, which is not in this group")
            gaps += [f"{t.name} has no routing policy for {snap.networks[n]}, so a failover to it would leave that VLAN on the normal internet"
                     for t in order for n in nets if not any(r.network_id == t.id and covers(r, n) for r in vpn_pols)]
            out_groups.append({
                "gaps": gaps, "declared": bool(declared[g.name]), "name": g.name, "paused": gstat.get("paused", False), "healthy": gstat.get("healthy"), "decision": gstat.get("decision", ""),
                "exhausted": gstat.get("exhausted", False), "jobs": gstat.get("jobs", {}), "has_order": bool(order), "rotation": gstat.get("rotation"),
                "conflict": "No routing policy in UniFi sends traffic through this VPN client right now, so nothing is using it" if act and not live_nets else None,
                "active": act.name if act else None, "networks": [card(n) for n in nets],
                "lane": [tinfo(g, t, active_id, pol_of) for t in order],
                "pool": [{"id": t.id, "name": t.name, "status": (snap.connections.get(t.id).status if snap.connections.get(t.id) else None),
                          "enabled": t.enabled} for t in pool],
                "exit": {"ip": probe.get("ip") if probe.get("ok") else None, "country": probe.get("country") if probe.get("ok") else None,
                         "server": ac.remote_ip if ac else None, "age": gstat.get("last_probe_age") if probe.get("ok") else None},
            })
        # Every other VLAN that a VPN client carries is drawn the same way, whether or not a group manages that client.
        for key, nets in sorted(((k, v) for k, v in by_tunnel.items() if v), key=lambda kv: (kv[0] is None, min(vlan_of(n) for n in kv[1]))):
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
                    lane.append(tinfo(None, tun, None, pol_of))
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
            canary = self.cfg.probe.canary.route_description

            def describe(r) -> dict[str, Any]:
                return {"description": r.description, "enabled": r.enabled, "kill_switch": r.kill_switch,
                        "networks": sorted(snap.networks.get(n, n) for n in r.target_networks),
                        "clients": sorted(r.target_macs), "managed_by": None,
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
