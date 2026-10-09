"""Talks to Home Assistant's REST API: lists notify services (so the UI can offer a dropdown) and sends test messages.

Inside the add-on it uses the Supervisor proxy + SUPERVISOR_TOKEN. Standalone it reuses the url/token of a
`home_assistant` notifier from the config.
"""
from __future__ import annotations

import os
import re
from typing import Any

import httpx

from .config import Config

SERVICE_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_SKIP = {"send_message"}      # entity-based notify action: needs an entity_id, not title/message


def label(service: str) -> str:
    domain, name = service.split(".", 1)
    if name.startswith("mobile_app_"):
        return "Phone: " + name.removeprefix("mobile_app_").replace("_", " ")
    if service == "persistent_notification.create":
        return "Persistent notification (HA sidebar)"
    return service


class HaApi:
    def __init__(self, base: str, token: str, transport: httpx.BaseTransport | None = None):
        self.base = base.rstrip("/")
        self._http = httpx.Client(timeout=8, headers={"Authorization": f"Bearer {token}"}, transport=transport)

    @classmethod
    def from_config(cls, cfg: Config, transport: httpx.BaseTransport | None = None) -> "HaApi | None":
        tok = os.environ.get("SUPERVISOR_TOKEN")
        if tok:
            return cls("http://supervisor/core", tok, transport)
        for n in cfg.notifications:
            if n.type == "home_assistant" and n.url and n.token:
                return cls(n.url, n.token, transport)
        return None

    def notify_services(self) -> list[dict[str, str]]:
        r = self._http.get(f"{self.base}/api/services")
        r.raise_for_status()
        out: list[dict[str, str]] = []
        for dom in r.json():
            if dom.get("domain") == "notify":
                out += [f"notify.{k}" for k in sorted(dom.get("services", {})) if k not in _SKIP]
            elif dom.get("domain") == "persistent_notification" and "create" in dom.get("services", {}):
                out.append("persistent_notification.create")
        return [{"service": s, "label": label(s)} for s in out]

    def call(self, service: str, title: str, message: str) -> None:
        if not SERVICE_RE.match(service):
            raise ValueError(f"invalid service name {service!r}")
        domain, name = service.split(".", 1)
        r = self._http.post(f"{self.base}/api/services/{domain}/{name}", json={"title": title, "message": message})
        r.raise_for_status()
