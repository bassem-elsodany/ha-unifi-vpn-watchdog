"""Alerting: webhook, ntfy, Telegram, Home Assistant. Failures here never break the watchdog."""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Any

import httpx

from .alerts import EVENTS, render
from .clock import Clock
from .config import AlertCfg, NotifyCfg

log = logging.getLogger("vpn_watchdog.notify")



class Notifier:
    def __init__(self, cfgs: list[NotifyCfg], clock: Clock, transport: httpx.BaseTransport | None = None,
                 alerts: dict[str, AlertCfg] | None = None):
        self.cfgs = cfgs
        self.alerts = alerts or {}
        self.clock = clock
        self._last: dict[tuple, float] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=200)   # shown in the UI "Events" tab
        self._http = httpx.Client(timeout=8, transport=transport)

    def record(self, event: str, message: str, level: str = "info") -> None:
        """History only (no push notification)."""
        self.history.appendleft({"ts": self.clock.now(), "event": event, "level": level, "message": message})

    def emit(self, event: str, title: str = "", message: str = "", level: str | None = None, key: str | None = None,
             **fields: Any) -> None:
        a = self.alerts.get(event)
        if a is not None:                       # user-editable templates win over the code's fallback wording
            title, message = render(a.title, fields), render(a.message, fields)
        level = level or EVENTS.get(event, {}).get("level", "info")
        enabled = True if a is None else bool(a.enabled)
        self.history.appendleft({"ts": self.clock.now(), "event": event, "level": level, "message": f"{title}: {message}"})
        log.log({"info": logging.INFO, "warning": logging.WARNING, "critical": logging.ERROR}.get(level, logging.INFO), "%s: %s", event, message)
        now = self.clock.now()
        if not enabled:                          # still in the history tab, never pushed
            return
        for i, cfg in enumerate(self.cfgs):
            if "*" not in cfg.events and event not in cfg.events:
                continue
            k = (i, event, key or title)
            if now - self._last.get(k, -1e12) < cfg.throttle_seconds:
                continue
            self._last[k] = now
            try:
                self._send(cfg, event, title, message, level, fields, now)
            except Exception as e:  # noqa: BLE001
                log.warning("notification via %s failed: %s", cfg.type, e)

    def _send(self, cfg: NotifyCfg, event: str, title: str, message: str, level: str, fields: dict[str, Any], ts: float) -> None:
        if cfg.type == "webhook":
            r = self._http.post(cfg.url, json={"event": event, "title": title, "message": message, "level": level, "fields": fields, "ts": ts}, headers=cfg.headers)
        elif cfg.type == "ntfy":
            headers = {"Title": title, "Priority": {"critical": "urgent", "warning": "high"}.get(level, "default"), "Tags": event, **cfg.headers}
            if cfg.token:
                headers["Authorization"] = f"Bearer {cfg.token}"
            r = self._http.post(f"{cfg.url.rstrip('/')}/{cfg.topic}", content=message.encode(), headers=headers)
        elif cfg.type == "telegram":
            r = self._http.post(f"https://api.telegram.org/bot{cfg.token}/sendMessage", json={"chat_id": cfg.chat_id, "text": f"{title}\n{message}"})
        else:  # home_assistant (REST API, or the Supervisor proxy when running as an add-on)
            svc = cfg.service.replace(".", "/", 1)
            base = "http://supervisor/core" if cfg.supervisor else cfg.url.rstrip("/")
            token = os.environ.get("SUPERVISOR_TOKEN", "") if cfg.supervisor else cfg.token
            r = self._http.post(f"{base}/api/services/{svc}", json={"title": title, "message": message}, headers={"Authorization": f"Bearer {token}", **cfg.headers})
        r.raise_for_status()
