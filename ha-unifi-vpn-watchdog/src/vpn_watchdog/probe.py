"""Exit-IP probing and the canary that lets us test any tunnel before production traffic is moved to it.

A tunnel can report CONNECTED while passing no traffic, so the decisive check is: fetch our public IP through it
and require (a) the request works, (b) the country matches the tunnel's ISO code, (c) it is not the WAN IP (leak).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from .clock import Clock
from .config import ProbeCfg, ProbeEndpoint
from .models import ProbeResult, Snapshot, Tunnel
from .unifi import UniFiClient

log = logging.getLogger("vpn_watchdog.probe")


def _dig(data: Any, path: str | None) -> Any:
    if not path:
        return None
    for part in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


def parse_endpoint(ep: ProbeEndpoint, body: str) -> tuple[str | None, str | None]:
    if ep.format == "text":
        return body.strip() or None, None
    if ep.format == "cloudflare_trace":
        kv = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
        return kv.get("ip"), kv.get("loc")
    import json

    data = json.loads(body)
    ip, country = _dig(data, ep.ip_field), _dig(data, ep.country_field)
    return (str(ip) if ip else None), (str(country) if country else None)


class Prober:
    def __init__(self, cfg: ProbeCfg, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        if transport is None and cfg.source_address:
            transport = httpx.HTTPTransport(local_address=cfg.source_address)
        self._http = httpx.Client(timeout=cfg.timeout_seconds, transport=transport, headers={"User-Agent": "ha-unifi-vpn-watchdog/0.1"})

    def _accepted(self, expected_iso: str | None) -> set[str]:
        if not expected_iso:
            return set()
        iso = expected_iso.upper()
        return {iso, *[a.upper() for a in self.cfg.country_aliases.get(iso, [])]}

    def probe(self, expected_iso: str | None, wan_ip: str | None) -> ProbeResult:
        wan_ip = self.cfg.wan_ip or wan_ip
        accepted = self._accepted(expected_iso)
        good: list[ProbeResult] = []
        bad: list[str] = []
        leak = False
        for ep in self.cfg.endpoints:
            try:
                r = self._http.get(ep.url)
                r.raise_for_status()
                ip, country = parse_endpoint(ep, r.text)
            except (httpx.HTTPError, ValueError) as e:
                bad.append(f"{ep.name}: {type(e).__name__}")
                continue
            if not ip:
                bad.append(f"{ep.name}: no ip in response")
                continue
            if wan_ip and ip == wan_ip:
                leak = True
                bad.append(f"{ep.name}: LEAK exit ip equals WAN ip {ip}")
                continue
            if self.cfg.check_country and accepted and country and country.upper() not in accepted:
                bad.append(f"{ep.name}: country {country} != {expected_iso}")
                continue
            good.append(ProbeResult(True, ip=ip, country=country))
            if len(good) >= self.cfg.require:
                break
        if len(good) >= self.cfg.require:
            return ProbeResult(True, "ok", ip=good[0].ip, country=good[0].country, details=bad)
        return ProbeResult(False, "; ".join(bad) or "no probe endpoints", leak=leak, details=bad)


class TunnelTester:
    """Single entry point the engine uses to ask 'does traffic really flow through this tunnel?'."""

    def __init__(self, cfg: ProbeCfg, unifi: UniFiClient, prober: Prober, clock: Clock, kill_switch_canary: bool = True):
        self.cfg = cfg
        self.unifi = unifi
        self.prober = prober
        self.clock = clock
        self._kill_switch_canary = kill_switch_canary

    @property
    def enabled(self) -> bool:
        return self.cfg.mode != "none"

    @property
    def can_pretest(self) -> bool:
        return self.cfg.mode in ("canary", "remote") and not self.unifi.dry_run

    def test(self, tunnel: Tunnel, snap: Snapshot, *, pre: bool, expect: str | None = None) -> ProbeResult | None:
        """Returns None when this tunnel cannot be tested in the current mode."""
        mode = self.cfg.mode
        iso = expect
        if mode == "none":
            return None
        if self.unifi.dry_run:
            # Route changes are simulated in dry-run, so a real probe would test the wrong tunnel.
            res = self.prober.probe(None, snap.wan_ip)
            log.info("[dry-run] informational probe from this host: %s ip=%s country=%s", res.reason, res.ip, res.country)
            return None
        if mode == "direct":
            return None if pre else self.prober.probe(iso, snap.wan_ip)
        # canary / remote: point the canary client at the tunnel first
        err = self._point_canary(tunnel, snap)
        if err:
            return ProbeResult(False, err)
        self.clock.sleep(self.cfg.canary.settle_seconds)
        if mode == "remote":
            return self._remote_probe(iso, snap.wan_ip)
        return self.prober.probe(iso, snap.wan_ip)

    def _remote_probe(self, iso: str | None, wan_ip: str | None) -> ProbeResult:
        headers = {"Authorization": f"Bearer {self.cfg.remote_token}"} if self.cfg.remote_token else {}
        try:
            r = self.prober._http.get(
                f"{self.cfg.remote_url.rstrip('/')}/probe",
                params={"expect": iso or "", "wan_ip": wan_ip or ""},
                headers=headers,
                timeout=self.cfg.timeout_seconds * (len(self.cfg.endpoints) + 1),
            )
            r.raise_for_status()
            d = r.json()
            return ProbeResult(bool(d.get("ok")), d.get("reason", ""), d.get("ip"), d.get("country"), bool(d.get("leak")))
        except (httpx.HTTPError, ValueError) as e:
            return ProbeResult(False, f"probe agent unreachable: {type(e).__name__}")

    def _point_canary(self, tunnel: Tunnel, snap: Snapshot) -> str | None:
        mac = (self.cfg.canary.mac or "").lower()
        desc = self.cfg.canary.route_description
        route = next((r for r in snap.routes if r.description == desc and mac in r.target_macs), None)
        if route is None:
            route = next((r for r in snap.routes if mac in r.target_macs and r.id and not r.target_networks), None)
        try:
            if route is None:
                self.unifi.create_route(desc, tunnel.id, target_macs=[mac], kill_switch=self._kill_switch_canary)
            elif route.network_id != tunnel.id or not route.enabled:
                self.unifi.set_route(route, network_id=tunnel.id, enabled=True, kill_switch=self._kill_switch_canary)
            else:
                return None
        except Exception as e:  # noqa: BLE001 - surfaced to the engine as a failed test
            return f"canary route update failed: {e}"
        # Let the controller apply the route before probing.
        return None
