"""UniFi Network API client (API-key auth) for WireGuard clients and policy routes.

Endpoints used (all verified against a UDM Pro SE):
  GET  /api/s/{site}/rest/networkconf            tunnels (purpose == vpn-client) and VLANs
  GET  /api/s/{site}/rest/networkconf/{id}       one object (needed for a full-object PUT)
  PUT  /api/s/{site}/rest/networkconf/{id}       enable / disable a tunnel
  GET  /v2/api/site/{site}/vpn/connections       live tunnel status (CONNECTED / CONNECTING, rx/tx rate)
  GET  /v2/api/site/{site}/trafficroutes         policy routes (read). Written only when a group has routing management switched
  POST/PUT/DELETE .../trafficroutes[/{id}]       on, and only for the watchdog's own policies (name starts with `vpnwd:`; see routing.py)
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
from .routing import PREFIX

log = logging.getLogger("vpn_watchdog.unifi")


class UniFiError(Exception):
    pass


class UniFiClient:
    def __init__(
        self,
        cfg: UnifiCfg,
        transport: httpx.BaseTransport | None = None,
    ):
        self.cfg = cfg
        self._known_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._http = httpx.Client(
            base_url=f"{cfg.url.rstrip('/')}/proxy/network",
            headers={"X-API-Key": cfg.api_key},
            verify=cfg.verify_tls,
            timeout=cfg.timeout_seconds,
            transport=transport,
        )

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
        network_info = {}
        for n in nets:
            networks[n["_id"]] = n.get("name", "")
            network_info[n["_id"]] = {"name": n.get("name", ""), "vlan": n.get("vlan"), "subnet": n.get("ip_subnet"), "purpose": n.get("purpose")}
            if n.get("purpose") == "vpn-client":
                t = parse_tunnel(n)
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
        return Snapshot(tunnels, connections, routes, networks, self._wan_ip(), network_info, self._clients(), self._known())

    def _known(self) -> dict[str, dict[str, Any]]:
        """Every device UniFi has seen (for picking a device in a group), read at most every two minutes. Failure is not fatal."""
        if self._known_cache and time.time() - self._known_cache[0] < 120:
            return self._known_cache[1]
        try:
            raw = (self._request("GET", self._s("rest/user")) or {}).get("data", [])
        except UniFiError as e:
            log.debug("could not read known clients: %s", e)
            return self._known_cache[1] if self._known_cache else {}
        out = {c["mac"].lower(): {"name": c.get("name") or c.get("hostname") or "", "ip": c.get("last_ip") or c.get("fixed_ip"),
                                  "network": c.get("last_connection_network_name"), "network_id": c.get("last_connection_network_id")}
               for c in raw if c.get("mac")}
        self._known_cache = (time.time(), out)
        return out

    def _clients(self) -> dict[str, dict[str, Any]]:
        """Connected devices: only what the network map needs (name, ip, network). Failure is not fatal."""
        try:
            raw = (self._request("GET", self._s("stat/sta")) or {}).get("data", [])
        except UniFiError as e:
            log.debug("could not read clients: %s", e)
            return {}
        out = {}
        for c in raw:
            if not c.get("mac"):
                continue
            rate = None
            if c.get("tx_bytes-r") is not None or c.get("rx_bytes-r") is not None:
                rate = int(((c.get("tx_bytes-r") or 0) + (c.get("rx_bytes-r") or 0)) * 8)      # bytes/s -> bit/s
            out[c["mac"].lower()] = {"name": c.get("name") or c.get("hostname") or "", "ip": c.get("ip"),
                                     "network": c.get("network"), "network_id": c.get("network_id"), "rate_bps": rate, "wired": bool(c.get("is_wired"))}
        return out

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
        targets = raw.get("target_devices") or []
        return Route(
            id=raw["_id"],
            description=raw.get("description", ""),
            network_id=raw.get("network_id"),
            enabled=bool(raw.get("enabled")),
            kill_switch=bool(raw.get("kill_switch_enabled")),
            target_networks=frozenset(t["network_id"] for t in targets if t.get("type") == "NETWORK" and t.get("network_id")),
            target_macs=frozenset(t["client_mac"].lower() for t in targets if t.get("type") == "CLIENT" and t.get("client_mac")),
            all_clients=any(t.get("type") == "ALL_CLIENTS" for t in targets),
            raw=raw,
            matching=raw.get("matching_target") or "INTERNET",
        )

    # ------------------------------------------------------------------ writes
    def set_tunnel_enabled(self, tunnel_id: str, enabled: bool) -> None:
        obj = ((self._request("GET", self._s(f"rest/networkconf/{tunnel_id}")) or {}).get("data") or [None])[0]
        if not obj:
            raise UniFiError(f"tunnel {tunnel_id} not found")
        if bool(obj.get("enabled")) == enabled:
            return
        obj["enabled"] = enabled
        self._request("PUT", self._s(f"rest/networkconf/{tunnel_id}"), json=obj)
        log.info("tunnel %s enabled=%s", obj.get("name"), enabled)

    # ------------------------------------------------ the watchdog's own routing policies (never anyone else's)
    def _own_route_raw(self, route_id: str) -> dict[str, Any]:
        raw = next((r for r in (self._request("GET", self._v2("trafficroutes")) or []) if r.get("_id") == route_id), None)
        if raw is None:
            raise UniFiError(f"routing policy {route_id} not found")
        if not str(raw.get("description", "")).startswith(PREFIX):
            raise UniFiError(f"refusing to change routing policy {raw.get('description')!r}: it is not one of the watchdog's own")
        return raw

    def create_own_route(self, body: dict[str, Any]) -> None:
        if not str(body.get("description", "")).startswith(PREFIX):
            raise UniFiError("refusing to create a routing policy that is not named like the watchdog's own")
        self._request("POST", self._v2("trafficroutes"), json=body)
        log.info("routing policy created: %s", body["description"])

    def update_own_route(self, route_id: str, network_id: str, enabled: bool = True, target_devices: list | None = None) -> None:
        raw = self._own_route_raw(route_id)
        raw["network_id"], raw["enabled"] = network_id, enabled
        if target_devices is not None:
            raw["target_devices"] = target_devices
        self._request("PUT", self._v2(f"trafficroutes/{route_id}"), json=raw)
        log.info("routing policy %s now goes through %s", raw.get("description"), network_id)

    def delete_own_route(self, route_id: str) -> None:
        raw = self._own_route_raw(route_id)
        self._request("DELETE", self._v2(f"trafficroutes/{route_id}"))
        log.info("routing policy deleted: %s", raw.get("description"))

    def set_route_enabled(self, route_id: str, enabled: bool) -> str:
        """Switch one routing policy on or off (never anything else about it). Only used after you confirmed it in the web UI
        and the engine checked that the policy really blocks a group's VLAN (or is one you let the watchdog switch off). Returns its name."""
        raw = next((r for r in (self._request("GET", self._v2("trafficroutes")) or []) if r.get("_id") == route_id), None)
        if raw is None:
            raise UniFiError(f"routing policy {route_id} not found")
        if bool(raw.get("enabled")) != enabled:
            raw["enabled"] = enabled
            self._request("PUT", self._v2(f"trafficroutes/{route_id}"), json=raw)
            log.info("routing policy %s enabled=%s (confirmed by the user)", raw.get("description"), enabled)
        return str(raw.get("description", ""))
