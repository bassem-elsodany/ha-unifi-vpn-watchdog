"""Probe agent: runs on a host whose traffic is steered by the canary route (own MAC, e.g. a macvlan container).

The watchdog (for example inside a Home Assistant add-on, which cannot have its own MAC) points the canary client
at a tunnel, then asks this agent GET /probe?expect=IT to report the exit IP/country it sees.
"""
from __future__ import annotations

import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import ProbeCfg
from .probe import Prober

log = logging.getLogger("vpn_watchdog.agent")


def serve(cfg: ProbeCfg, host: str, port: int, token: str | None) -> None:
    prober = Prober(cfg)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, obj: dict) -> None:
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/healthz":
                return self._send(200, {"ok": True})
            if u.path != "/probe":
                return self._send(404, {"error": "not found"})
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").removeprefix("Bearer ").strip(), token):
                return self._send(401, {"error": "unauthorized"})
            q = parse_qs(u.query)
            res = prober.probe((q.get("expect") or [""])[0] or None, (q.get("wan_ip") or [""])[0] or None)
            self._send(200, res.as_dict())

    log.info("probe agent listening on %s:%d", host, port)
    ThreadingHTTPServer((host, port), H).serve_forever()
