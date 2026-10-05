"""Alerting: webhook, ntfy, Telegram, Home Assistant. Failures here never break the watchdog."""
from __future__ import annotations

import logging
import os
from collections import deque
from typing import Any

import httpx

from .clock import Clock
from .config import NotifyCfg

log = logging.getLogger("vpn_watchdog.notify")

EVENTS = ("startup", "switch", "failback", "exhausted", "recovered", "leak", "blocked", "config_error", "error")


class Notifier:
    def __init__(self, cfgs: list[NotifyCfg], clock: Clock, dry_run: bool = False, transport: httpx.BaseTransport | None = None):
        self.cfgs = cfgs
        self.clock = clock
        self.dry_run = dry_run
        self._last: dict[tuple, float] = {}
        self.history: deque[dict[str, Any]] = deque(maxlen=200)   # shown in the UI "Events" tab
        self._http = httpx.Client(timeout=8, transport=transport)

    def record(self, event: str, message: str, level: str = "info") -> None:
        """History only (no push notification)."""
        self.history.appendleft({"ts": self.clock.now(), "event": event, "level": level, "message": message})

    def emit(self, event: str, title: str, message: str, level: str = "info", key: str | None = None, **fields: Any) -> None:
        if self.dry_run:
            title = f"[DRY-RUN] {title}"
        self.history.appendleft({"ts": self.clock.now(), "event": event, "level": level, "message": f"{title}: {message}"})
        log.log({"info": logging.INFO, "warning": logging.WARNING, "critical": logging.ERROR}.get(level, logging.INFO), "%s: %s", event, message)
        now = self.clock.now()
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
