"""UniFi Network API client (API-key auth) for WireGuard clients and policy routes.

Endpoints used (all verified against a UDM Pro SE):
  GET  /api/s/{site}/rest/networkconf            tunnels (purpose == vpn-client) and VLANs
  GET  /api/s/{site}/rest/networkconf/{id}       one object (needed for a full-object PUT)
  PUT  /api/s/{site}/rest/networkconf/{id}       enable / disable a tunnel
  GET  /v2/api/site/{site}/vpn/connections       live tunnel status (CONNECTED / CONNECTING, rx/tx rate)
  GET|POST|PUT /v2/api/site/{site}/trafficroutes policy routes
  GET  /api/s/{site}/stat/health                 WAN IP (for leak detection)

The networkconf objects of VPN clients contain the WireGuard private key. They are only ever held in a local
variable while building a PUT body and are never logged or stored.
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any

import httpx

from .config import UnifiCfg
from .models import Connection, Route, Snapshot, parse_tunnel

log = logging.getLogger("vpn_watchdog.unifi")


class UniFiError(Exception):
    pass


class UniFiClient:
    def __init__(
        self,
        cfg: UnifiCfg,
        dry_run: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.cfg = cfg
        self.dry_run = dry_run
        self._http = httpx.Client(
            base_url=f"{cfg.url.rstrip('/')}/proxy/network",
            headers={"X-API-Key": cfg.api_key},
            verify=cfg.verify_tls,
            timeout=cfg.timeout_seconds,
            transport=transport,
        )
        # In dry-run mutations are remembered here so later ticks behave consistently.
        self._shadow_routes: dict[str, dict[str, Any]] = {}
        self._shadow_enabled: dict[str, bool] = {}

    # ------------------------------------------------------------------ plumbing
    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, json: Any = None) -> Any:
        last: Exception | None = None
        for attempt in range(self.cfg.retries + 1):
            try:
                r = self._http.request(method, path, json=json)
            except httpx.TransportError as e:
                last = e
            else:
                if r.status_code >= 500:
                    last = UniFiError(f"{method} {path} -> HTTP {r.status_code}")
                elif r.status_code >= 400:
                    raise UniFiError(f"{method} {path} -> HTTP {r.status_code}: {r.text[:160]}")
                else:
                    try:
                        return r.json() if r.content else None
                    except ValueError as e:
                        raise UniFiError(f"{method} {path} returned non-JSON") from e
            if attempt < self.cfg.retries:
                time.sleep(0.5 * (attempt + 1))
        raise UniFiError(f"{method} {path} failed: {last}")

    def _s(self, tail: str) -> str:
        return f"/api/s/{self.cfg.site}/{tail}"

    def _v2(self, tail: str) -> str:
        return f"/v2/api/site/{self.cfg.site}/{tail}"

    # ------------------------------------------------------------------ reads
    def snapshot(self) -> Snapshot:
        nets = (self._request("GET", self._s("rest/networkconf")) or {}).get("data", [])
        conns = (self._request("GET", self._v2("vpn/connections")) or {}).get("connections", [])
        routes_raw = self._request("GET", self._v2("trafficroutes")) or []

        tunnels = {}
        networks = {}
        for n in nets:
            networks[n["_id"]] = n.get("name", "")
            if n.get("purpose") == "vpn-client":
                t = parse_tunnel(n, self._shadow_enabled.get(n["_id"]))
                tunnels[t.id] = t

        connections = {
            c["network_id"]: Connection(
                network_id=c["network_id"],
                status=c.get("status", "UNKNOWN"),
                remote_ip=c.get("remote_ip"),
                rx_bps=c.get("rx_rate_bps"),
                tx_bps=c.get("tx_rate_bps"),
                notes=list(c.get("notes") or []),
            )
            for c in conns
            if c.get("network_id")
        }
        routes = [self._parse_route(r) for r in routes_raw]
        return Snapshot(tunnels, connections, routes, networks, self._wan_ip())

    def _wan_ip(self) -> str | None:
        try:
            for s in (self._request("GET", self._s("stat/health")) or {}).get("data", []):
                if s.get("subsystem") == "wan":
                    return s.get("wan_ip")
        except UniFiError as e:
            log.debug("could not read WAN IP: %s", e)
        return None

    def _parse_route(self, raw: dict[str, Any]) -> Route:
        raw = copy.deepcopy(raw)
        shadow = self._shadow_routes.get(raw["_id"])
        if shadow:
            raw.update(shadow)
        targets = raw.get("target_devices") or []
        return Route(
            id=raw["_id"],
            description=raw.get("description", ""),
            network_id=raw.get("network_id"),
            enabled=bool(raw.get("enabled")),
            kill_switch=bool(raw.get("kill_switch_enabled")),
            target_networks=frozenset(t["network_id"] for t in targets if t.get("type") == "NETWORK" and t.get("network_id")),
            target_macs=frozenset(t["client_mac"].lower() for t in targets if t.get("type") == "CLIENT" and t.get("client_mac")),
            raw=raw,
        )

    # ------------------------------------------------------------------ writes
    def set_tunnel_enabled(self, tunnel_id: str, enabled: bool) -> None:
        if self.dry_run:
            log.info("[dry-run] would set tunnel %s enabled=%s", tunnel_id, enabled)
            self._shadow_enabled[tunnel_id] = enabled
            return
        obj = ((self._request("GET", self._s(f"rest/networkconf/{tunnel_id}")) or {}).get("data") or [None])[0]
        if not obj:
            raise UniFiError(f"tunnel {tunnel_id} not found")
        if bool(obj.get("enabled")) == enabled:
            return
        obj["enabled"] = enabled
        self._request("PUT", self._s(f"rest/networkconf/{tunnel_id}"), json=obj)
        log.info("tunnel %s enabled=%s", obj.get("name"), enabled)

    def set_route(
        self,
        route: Route,
        *,
        network_id: str | None = None,
        description: str | None = None,
        kill_switch: bool | None = None,
        enabled: bool | None = None,
    ) -> None:
        changes: dict[str, Any] = {}
        if network_id is not None:
            changes["network_id"] = network_id
        if description is not None:
            changes["description"] = description
        if kill_switch is not None:
            changes["kill_switch_enabled"] = kill_switch
        if enabled is not None:
            changes["enabled"] = enabled
        changes = {k: v for k, v in changes.items() if route.raw.get(k) != v}
        if not changes:
            return
        if self.dry_run:
            log.info("[dry-run] would update route %r: %s", route.description, changes)
            self._shadow_routes.setdefault(route.id, {}).update(changes)
            return
        body = {**route.raw, **changes}
        self._request("PUT", self._v2(f"trafficroutes/{route.id}"), json=body)
        log.info("route %r updated: %s", route.description, changes)

    def create_route(
        self,
        description: str,
        network_id: str,
        target_networks: list[str] = (),  # type: ignore[assignment]
        target_macs: list[str] = (),      # type: ignore[assignment]
        kill_switch: bool = False,
    ) -> None:
        targets = [{"network_id": n, "type": "NETWORK"} for n in target_networks]
        targets += [{"client_mac": m, "type": "CLIENT"} for m in target_macs]
        body = {
            "description": description,
            "enabled": True,
            "network_id": network_id,
            "matching_target": "INTERNET",
            "domains": [],
            "ip_addresses": [],
            "ip_ranges": [],
            "regions": [],
            "next_hop": "",
            "kill_switch_enabled": kill_switch,
            "target_devices": targets,
        }
        if self.dry_run:
            log.info("[dry-run] would create route %r -> tunnel %s targets=%s", description, network_id, targets)
            return
        self._request("POST", self._v2("trafficroutes"), json=body)
        log.info("route %r created", description)
