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
    """Single entry point the engine uses to ask 'does traffic really flow through this tunnel?'.

    The only active mode is `direct`: the probe runs from this host's own egress, so it only says something when this host is routed
    through the VPN. Steering a separate test device through each tunnel would mean changing a routing policy, which the watchdog never does."""

    def __init__(self, cfg: ProbeCfg, unifi: UniFiClient, prober: Prober, clock: Clock):
        self.cfg = cfg
        self.unifi = unifi
        self.prober = prober
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return self.cfg.mode == "direct"

    @property
    def can_pretest(self) -> bool:
        return False

    def test(self, tunnel: Tunnel, snap: Snapshot, *, pre: bool, expect: str | None = None) -> ProbeResult | None:
        """Returns None when this tunnel cannot be tested in the current mode."""
        if self.cfg.mode != "direct" or pre:
            return None
        return self.prober.probe(expect, snap.wan_ip)
