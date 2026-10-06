"""Stdlib HTTP server: web UI, JSON API, /healthz, /metrics. Works standalone (bearer token) and behind Home
Assistant ingress (the Supervisor authenticates the user; requests then come from 172.30.32.2)."""
from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlparse

from .config import ServerCfg

log = logging.getLogger("vpn_watchdog.server")
INGRESS_PEER = "172.30.32.2"


def render_metrics(status: dict) -> str:
    out = ["# TYPE vpn_watchdog_group_healthy gauge", "# TYPE vpn_watchdog_group_active gauge",
           "# TYPE vpn_watchdog_switches_last_hour gauge", "# TYPE vpn_watchdog_tunnel_connected gauge",
           "# TYPE vpn_watchdog_tunnel_quarantined_seconds gauge", "# TYPE vpn_watchdog_last_tick_timestamp gauge"]
    for g, d in status.get("groups", {}).items():
        out.append(f'vpn_watchdog_group_healthy{{group="{g}"}} {int(bool(d["healthy"]))}')
        out.append(f'vpn_watchdog_switches_last_hour{{group="{g}"}} {d["switches_last_hour"]}')
        if d["active"]:
            out.append(f'vpn_watchdog_group_active{{group="{g}",tunnel="{d["active"]}",position="{d["position"]}"}} 1')
    for t, d in status.get("tunnels", {}).items():
        out.append(f'vpn_watchdog_tunnel_connected{{tunnel="{t}"}} {int(d["status"] == "CONNECTED")}')
        out.append(f'vpn_watchdog_tunnel_quarantined_seconds{{tunnel="{t}"}} {d["quarantined_for"]}')
    out.append(f'vpn_watchdog_last_tick_timestamp {status.get("last_tick") or 0}')
    return "\n".join(out) + "\n"


def _index_html() -> bytes:
    return resources.files("vpn_watchdog").joinpath("ui/index.html").read_bytes()


class StatusServer:
    def __init__(self, cfg: ServerCfg, app, stale_after: float):
        self.cfg = cfg
        self.app = app
        self.stale_after = stale_after
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body, ctype: str = "application/json") -> None:
                data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, default=str)).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _can_control(self) -> bool:
                if outer.cfg.trust_ingress and self.client_address[0] == INGRESS_PEER:
                    return True
                tok = outer.cfg.control_token
                given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                return bool(tok) and hmac.compare_digest(given, tok)

            def _json_body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n) or b"{}") if n else {}

            # ---------------------------------------------------------------- reads (status is read-only, no auth)
            def do_GET(self):  # noqa: N802
                eng = outer.app.engine
                path = urlparse(self.path).path.rstrip("/") or "/"
                if path == "/healthz":
                    fresh = eng.last_tick is not None and time.time() - eng.last_tick < outer.stale_after
                    self._send(200 if fresh else 503, {"ok": fresh, "error": eng.last_error})
                elif path in ("/status", "/api/status"):
                    st = eng.status()
                    st["can_control"] = self._can_control()
                    self._send(200, st)
                elif path == "/metrics":
                    self._send(200, render_metrics(eng.status()), "text/plain; version=0.0.4")
                elif path == "/api/config":
                    if not self._can_control():
                        return self._send(401, {"error": "unauthorized"})
                    self._send(200, {"yaml": outer.app.config_text()})
                elif path == "/api/settings":
                    if not self._can_control():
                        return self._send(401, {"error": "unauthorized"})
                    self._send(200, outer.app.get_settings())
                elif path == "/api/ha/notify-services":
                    if not self._can_control():
                        return self._send(401, {"error": "unauthorized"})
                    self._send(200, outer.app.ha_notify_services())
                elif path == "/":
                    self._send(200, _index_html(), "text/html; charset=utf-8")
                else:
                    self._send(404, {"error": "not found"})

            # ---------------------------------------------------------------- writes (auth required)
            def do_POST(self):  # noqa: N802
                if not self._can_control():
                    return self._send(401, {"error": "control needs Authorization: Bearer <control_token> (or HA ingress)"})
                eng = outer.app.engine
                parts = [p for p in urlparse(self.path).path.split("/") if p]
                if parts[:1] == ["api"]:
                    parts = parts[1:]
                try:
                    body = self._json_body()
                except ValueError:
                    return self._send(400, {"error": "invalid JSON"})
                if len(parts) == 3 and parts[0] == "groups" and parts[2] in ("pause", "resume"):
                    eng.submit(parts[2], parts[1])
                elif len(parts) == 3 and parts[0] == "groups" and parts[2] in ("switch", "test") and body.get("tunnel"):
                    eng.submit(parts[2], parts[1], body["tunnel"])
                elif len(parts) == 3 and parts[0] == "groups" and parts[2] == "order" and isinstance(body.get("order"), list):
                    err = outer.app.set_group_order(parts[1], body["order"])
                    return self._send(400 if err else 200, {"error": err})
                elif parts == ["check-now"]:
                    eng.wake.set()
                elif parts == ["config", "validate"]:
                    return self._send(200, {"error": outer.app.validate_text(body.get("yaml", ""))})
                elif parts == ["config"]:
                    err = outer.app.save_text(body.get("yaml", ""))
                    return self._send(400 if err else 200, {"error": err, "saved": not err})
                elif parts == ["settings"]:
                    err = outer.app.save_settings(body.get("values", {}))
                    return self._send(400 if err else 200, {"error": err, "saved": not err})
                elif parts == ["notify", "service"]:
                    err = outer.app.set_notify_service(body.get("service", ""))
                    return self._send(400 if err else 200, {"error": err})
                elif parts == ["notify", "test"]:
                    err = outer.app.test_notify(body.get("service"))
                    return self._send(502 if err else 200, {"error": err})
                else:
                    return self._send(404, {"error": "unknown endpoint"})
                self._send(202, {"accepted": True})

        self._srv = ThreadingHTTPServer((cfg.host, cfg.port), H)

    @property
    def port(self) -> int:
        return self._srv.server_address[1]

    def start(self) -> None:
        threading.Thread(target=self._srv.serve_forever, daemon=True, name="status-server").start()
        log.info("web UI / API on %s:%d", self.cfg.host, self.port)

    def stop(self) -> None:
        self._srv.shutdown()
