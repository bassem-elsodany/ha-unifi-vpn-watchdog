"""Failover engine: health evaluation, quarantine with backoff, ladder-ordered switching, failback, standby."""
from __future__ import annotations

import logging
import queue
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .clock import Clock
from .config import Config, GroupCfg, GroupSettings
from .ladder import candidates, expected_country, failback_targets, resolve_ladder, step_of
from .models import ProbeResult, Route, Snapshot, Tunnel
from .notify import Notifier
from .probe import TunnelTester
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
        self._status: dict[str, Any] = {}
        self._overlap_warned: set[str] = set()
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
            return dict(self._status)

    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg

    def tick(self) -> None:
        try:
            snap = self.unifi.snapshot()
        except UniFiError as e:
            self.last_error = str(e)
            log.error("cannot read UniFi state, skipping cycle: %s", e)
            self._publish(None)
            return
        self.last_error = None
        self._snap = snap
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
            self._reconcile_standby(self.unifi.snapshot())
        except UniFiError as e:
            log.warning("standby reconcile skipped: %s", e)
        self.last_tick = self.clock.now()
        self.store.save()
        self._publish(snap)

    # ------------------------------------------------------------------ per-group loop
    def _tick_group(self, g: GroupCfg, snap: Snapshot) -> None:
        st = self.cfg.settings_for(g)
        gs = self.store.group(g.name)
        ladder = resolve_ladder(g, list(snap.tunnels.values()))
        if not ladder:
            gs.last_decision = "ladder resolved to no tunnels - check names/country codes"
            log.error("group %s: %s", g.name, gs.last_decision)
            return

        route = self._find_route(g, snap, gs)
        if route is None:
            gs.last_decision = "no managed route found; provisioning"
            self._provision(g, st, gs, ladder, snap)
            return
        gs.route_id = route.id
        self._warn_overlaps(g, route, snap)

        cur = snap.tunnels.get(route.network_id or "")
        cur_id = cur.id if cur else None
        if gs.current_id != cur_id:
            if gs.current_id is not None:
                log.warning("group %s: route target changed outside the watchdog (%s -> %s); adopting",
                            g.name, gs.current_id, cur_id)
            self._set_current(gs, cur_id)
        if gs.paused:
            gs.last_decision = "paused"
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
            gs.last_decision = "healthy"
            self._maybe_failback(g, st, gs, cur, route, snap)
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
        self._failover(g, st, gs, cur, route, hard, "; ".join(health.reasons))

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
    def _failover(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cur: Tunnel | None, route: Route,
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
                self._commit(g, st, gs, cand, route, f"failover from {cur.name if cur else 'unknown'}: {why}", "switch")
                return
            self._quarantine(cand, reason, st)
            log.warning("group %s: candidate %s rejected: %s", g.name, cand.name, reason)
        if not gs.exhausted:
            gs.exhausted = True
            self.notifier.emit("exhausted", f"VPN {g.name}: no healthy tunnel",
                               f"{cur.name if cur else 'current'} is down and {tried} candidate(s) failed or are quarantined",
                               level="critical", key=g.name, group=g.name, tunnel=cur.name if cur else "", tried=tried)
        if st.switching.on_exhausted == "kill_switch" and route is not None:
            self.unifi.set_route(route, kill_switch=True)
        gs.last_decision = f"exhausted: {why}"

    def _step_name(self, g: GroupCfg, t: Tunnel | None) -> str:
        snap = self._snap
        hit = step_of(g, list(snap.tunnels.values()), t) if snap and t else None
        return hit[1] if hit else ""

    def _expect(self, g: GroupCfg, snap: Snapshot, t: Tunnel) -> str | None:
        return expected_country(g, list(snap.tunnels.values()), t)

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
        if self.unifi.dry_run:
            if not cand.enabled:
                self.unifi.set_tunnel_enabled(cand.id, True)
            return True, "dry-run: tunnel state not verified"
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

    def _commit(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cand: Tunnel, route: Route | None,
                reason: str, event: str) -> None:
        desc = cand.name if st.switching.rename_route_to_tunnel else None
        if route is None:
            snap = self.unifi.snapshot()
            nets = [self._net_id(snap, n) for n in g.networks]
            self.unifi.create_route(cand.name, cand.id, target_networks=nets, kill_switch=bool(g.kill_switch))
        else:
            self.unifi.set_route(route, network_id=cand.id, description=desc, kill_switch=g.kill_switch)
        if not self.unifi.dry_run:
            after = self.unifi.snapshot()
            r = next((x for x in after.routes if x.id == (route.id if route else x.id) and x.network_id == cand.id), None)
            if r is None:
                raise UniFiError(f"route did not switch to {cand.name} (controller did not apply the change)")
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
                           step=self._step_name(g, cand), previous_step=self._step_name(g, old) if old else "", reason=reason)

    def _provision(self, g: GroupCfg, st: GroupSettings, gs: GroupState, ladder: list[Tunnel], snap: Snapshot) -> None:
        for cand in ladder:
            if self.store.tunnel(cand.id).quarantined_until > self.clock.now():
                continue
            ok, why = self._try(g, cand, st)
            if ok:
                self._commit(g, st, gs, cand, None, "initial provisioning", "switch")
                return
            self._quarantine(cand, why, st)

    # ------------------------------------------------------------------ failback
    def _maybe_failback(self, g: GroupCfg, st: GroupSettings, gs: GroupState, cur: Tunnel | None,
                        route: Route, snap: Snapshot) -> None:
        if not st.failback.enabled or cur is None:
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
            self._commit(g, st, gs, target, route, f"failback to preferred tunnel after {st.failback.stable_seconds}s stable", "failback")

    # ------------------------------------------------------------------ standby
    def _reconcile_standby(self, snap: Snapshot) -> None:
        keep: set[str] = set()
        managed: set[str] = set()
        now = self.clock.now()
        for g in self.cfg.groups:
            st = self.cfg.settings_for(g)
            ladder = resolve_ladder(g, list(snap.tunnels.values()))
            managed.update(t.id for t in ladder)
            gs = self.store.group(g.name)
            if gs.current_id:
                keep.add(gs.current_id)
            cur = snap.tunnels.get(gs.current_id or "")
            warm = [t for t in candidates(g, list(snap.tunnels.values()), cur)
                    if self.store.tunnel(t.id).quarantined_until <= now]
            keep.update(t.id for t in warm[: st.standby.warm])
            if gs.failback_target:
                keep.add(gs.failback_target)
        in_use = {r.network_id for r in snap.routes if r.enabled and r.network_id}
        disable_unused = any(self.cfg.settings_for(g).standby.disable_unused for g in self.cfg.groups)
        for tid in managed:
            t = snap.tunnels[tid]
            if tid in keep and not t.enabled:
                self.unifi.set_tunnel_enabled(tid, True)
            elif tid not in keep and t.enabled and disable_unused and tid not in in_use:
                self.unifi.set_tunnel_enabled(tid, False)
        cap = min(self.cfg.settings_for(g).standby.max_enabled for g in self.cfg.groups)
        if len(keep) > cap:
            log.warning("standby set (%d) exceeds max_enabled (%d); lower standby.warm", len(keep), cap)

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
    def _net_id(snap: Snapshot, ref: str) -> str:
        if ref in snap.networks:
            return ref
        for nid, name in snap.networks.items():
            if name == ref:
                return nid
        raise UniFiError(f"network {ref!r} not found in UniFi")

    def _find_route(self, g: GroupCfg, snap: Snapshot, gs: GroupState) -> Route | None:
        pin = g.route_id or gs.route_id
        if pin:
            r = next((r for r in snap.routes if r.id == pin), None)
            if r:
                return r
        want = frozenset(self._net_id(snap, n) for n in g.networks)
        cands = [r for r in snap.routes
                 if want <= r.target_networks and not r.target_macs and r.network_id in snap.tunnels]
        if not cands:
            return None
        cands.sort(key=lambda r: (not r.enabled, len(r.target_networks - want)))
        return cands[0]

    def _warn_overlaps(self, g: GroupCfg, route: Route, snap: Snapshot) -> None:
        want = frozenset(self._net_id(snap, n) for n in g.networks)
        for r in snap.routes:
            if r.id != route.id and r.enabled and not r.target_macs and (r.target_networks & want):
                key = f"{g.name}:{r.id}"
                if key not in self._overlap_warned:
                    self._overlap_warned.add(key)
                    log.warning("group %s: enabled route %r also targets the same network(s); the first route in "
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
        elif kind == "test":
            t = snap.tunnel_by_name(cmd[2])
            if t is None:
                return
            ok, why = self._try(g, t, self.cfg.settings_for(g))
            self._record("test", f"{t.name}: {'OK' if ok else 'FAILED - ' + why}", "info" if ok else "warning")
        elif kind == "switch":
            name = cmd[2]
            target = snap.tunnel_by_name(name)
            if target is None:
                log.warning("manual switch: tunnel %r not found", name)
                return
            st = self.cfg.settings_for(g)
            ok, why = self._try(g, target, st)
            if not ok:
                log.warning("manual switch to %s refused: %s", name, why)
                return
            self._commit(g, st, gs, target, self._find_route(g, snap, gs), "manual switch", "switch")

    # ------------------------------------------------------------------ status export
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
            managed = {self.store.group(g.name).route_id: g.name for g in self.cfg.groups}
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
                    "step": self._step_name(g, cur),
                    "healthy": gs.healthy is True,
                    "decision": gs.last_decision,
                    "paused": gs.paused,
                    "exhausted": gs.exhausted,
                    "status_failures": gs.status_failures,
                    "probe_failures": gs.probe_failures,
                    "last_probe": gs.last_probe,
                    "switches_last_hour": len([t for t in gs.switches if now - t < 3600]),
                    "ladder": [t.name for t in resolve_ladder(g, list(snap.tunnels.values()))],
                    "jobs": {
                        "next_probe_in": max(0, int(gs.last_probe_ts + self.cfg.settings_for(g).detection.probe_interval_seconds - now)) if self.tester.enabled else None,
                        "next_failback_check_in": max(0, int(gs.last_failback_check + self.cfg.settings_for(g).failback.check_interval_seconds - now)) if self.cfg.settings_for(g).failback.enabled else None,
                        "failback_target": (snap.tunnels[gs.failback_target].name if gs.failback_target in snap.tunnels else None),
                        "failback_stable_for": int(now - gs.failback_stable_since) if gs.failback_stable_since else None,
                    },
                }
        status = {
            "dry_run": self.cfg.dry_run, "last_tick": self.last_tick, "error": self.last_error,
            "groups": groups, "tunnels": tunnels, "direct_routes": direct,
            "events": list(getattr(self.notifier, "history", []))[:60],
            "interval_seconds": self.cfg.interval_seconds,
        }
        with self._lock:
            self._status = status
        for fn in self.listeners:
            try:
                fn(status)
            except Exception:  # noqa: BLE001
                log.exception("status listener failed")
