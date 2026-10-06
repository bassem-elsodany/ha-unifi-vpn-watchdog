"""Wires the components together, runs the loop, hot-reloads the config file."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import tempfile
import threading
from pathlib import Path

import yaml

from .clock import Clock
from .config import Config, ConfigError, NotifyCfg, load_config, load_raw, parse_config
from . import settings as settings_mod
from .ha_api import SERVICE_RE, HaApi
from .engine import Engine
from .ha_mqtt import MqttPublisher
from .notify import Notifier
from .probe import Prober, TunnelTester
from .server import StatusServer
from .state import StateStore
from .unifi import UniFiClient

log = logging.getLogger("vpn_watchdog.app")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({"ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"), "level": record.levelname,
                           "logger": record.name, "msg": record.getMessage()})


def setup_logging(level: str, fmt: str) -> None:
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter() if fmt == "json" else logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpcore", "paho"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class App:
    def __init__(self, config_path: str, env: dict[str, str] | None = None, dry_run: bool | None = None):
        self.path = Path(config_path)
        self.env = env
        self.dry_run_override = dry_run
        self.clock = Clock()
        self.cfg = self._load()
        self.store = StateStore(self.cfg.state_file)
        self._apply_settings(self.cfg)
        self.stop_event = threading.Event()
        self.server: StatusServer | None = None
        self.mqtt: MqttPublisher | None = None
        self._mtime = self.path.stat().st_mtime
        self.engine = self._build()

    def _load(self) -> Config:
        cfg = load_config(self.path, self.env)
        if self.dry_run_override is not None:
            cfg.dry_run = self.dry_run_override
        return cfg

    def _build(self) -> Engine:
        cfg = self.cfg
        unifi = UniFiClient(cfg.unifi, dry_run=cfg.dry_run)
        tester = TunnelTester(cfg.probe, unifi, Prober(cfg.probe), self.clock)
        notifier = Notifier(cfg.notifications, self.clock, dry_run=cfg.dry_run, alerts=cfg.alerts)
        eng = Engine(cfg, unifi, tester, notifier, self.store, self.clock)
        if self.mqtt:
            eng.listeners.append(self.mqtt.publish)
        return eng

    def _apply_settings(self, cfg: Config) -> None:
        """Choices made in the UI overlay the file (stored in the state volume, not in config.yaml)."""
        svc = self.store.settings.get("notify_service")
        if not svc:
            return
        hits = [n for n in cfg.notifications if n.type == "home_assistant"]
        for n in hits:
            n.service = svc
        if not hits and os.environ.get("SUPERVISOR_TOKEN"):
            cfg.notifications.append(NotifyCfg(type="home_assistant", supervisor=True, service=svc))

    # ---- used by the web UI
    def get_settings(self) -> dict:
        return {"values": settings_mod.extract(self.cfg), "meta": settings_mod.meta(self.engine.last_snapshot()),
                "dry_run": self.cfg.dry_run}

    def save_settings(self, form: dict) -> str | None:
        """Apply the settings form to config.yaml (validated; the previous file is kept as .bak)."""
        try:
            raw = load_raw(self.config_text())
            new = settings_mod.apply(raw, form)
        except (KeyError, ValueError, TypeError, AttributeError, ConfigError) as e:
            return f"invalid settings: {type(e).__name__}: {e}"
        header = "# Written by the VPN Watchdog settings form (comments are not kept; secrets stay as ${VAR} references).\n"
        return self.save_text(header + yaml.safe_dump(new, sort_keys=False, allow_unicode=True))

    def current_notify_service(self) -> str | None:
        return next((n.service for n in self.cfg.notifications if n.type == "home_assistant"), None)

    def ha_notify_services(self) -> dict:
        api = HaApi.from_config(self.cfg)
        out = {"available": api is not None, "current": self.current_notify_service(), "services": [], "error": None}
        if api is None:
            return out
        try:
            out["services"] = api.notify_services()
        except Exception as e:  # noqa: BLE001
            out["error"] = f"{type(e).__name__}: {e}"
        return out

    def set_notify_service(self, service: str) -> str | None:
        if not SERVICE_RE.match(service or ""):
            return "invalid service name"
        self.store.settings["notify_service"] = service
        self.store.touch()
        self.store.save()
        self._mtime = 0                      # reload at the start of the next cycle
        self.engine.wake.set()
        return None

    def test_notify(self, service: str | None = None) -> str | None:
        api = HaApi.from_config(self.cfg)
        svc = service or self.current_notify_service()
        if api is None or not svc:
            return "not connected to Home Assistant or no service selected"
        try:
            api.call(svc, "VPN Watchdog test", "If you can read this, notifications work.")
        except Exception as e:  # noqa: BLE001
            return f"{type(e).__name__}: {e}"
        return None

    def config_text(self) -> str:
        return self.path.read_text()

    def validate_text(self, text: str) -> str | None:
        """None when valid, otherwise a secret-free error message."""
        try:
            parse_config(text, self.env)
            return None
        except ConfigError as e:
            return str(e)

    def save_text(self, text: str) -> str | None:
        err = self.validate_text(text)
        if err:
            return err
        bak = self.path.with_suffix(self.path.suffix + ".bak")
        try:
            bak.write_text(self.path.read_text())
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".cfg-")
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except OSError as e:
            return f"cannot write {self.path}: {e}"
        self.engine.wake.set()      # reload happens at the start of the next cycle
        return None

    def set_dry_run(self, dry_run: bool) -> str | None:
        text = self.config_text()
        line = f"dry_run: {str(dry_run).lower()}"
        new = re.sub(r"(?m)^dry_run:.*$", line, text) if re.search(r"(?m)^dry_run:", text) else line + "\n" + text
        return self.save_text(new)

    def reload_if_changed(self) -> None:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m == self._mtime:
            return
        self._mtime = m
        try:
            self.cfg = self._load()
            self._apply_settings(self.cfg)
        except ConfigError as e:
            log.error("config reload rejected, keeping the previous config: %s", e)
            self.engine.notifier.emit("config_error", "VPN watchdog config rejected", str(e)[:300], level="warning", key="cfg", error=str(e)[:300])
            return
        old = self.engine
        self.engine = self._build()
        old.unifi.close()
        log.info("config reloaded")

    def _start_mqtt(self) -> None:
        """MQTT only adds Home Assistant entities; failover must keep working without it."""
        try:
            self.mqtt = MqttPublisher(self.cfg.mqtt, lambda: self.engine)
            self.engine.listeners.append(self.mqtt.publish)
        except Exception as e:  # noqa: BLE001
            self.mqtt = None
            hint = ("restart the Mosquitto broker add-on (it re-registers with the Supervisor), then restart this add-on; or "
                    "fill mqtt_host (core-mosquitto for the Mosquitto add-on), mqtt_username and mqtt_password in the "
                    "Configuration tab"
                    if self.cfg.mqtt.supervisor else "check mqtt.host / port / username / password")
            log.warning("MQTT entities disabled (%s: %s). %s. Failover is unaffected.", type(e).__name__, e, hint)

    def run(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (self.stop_event.set(), self.engine.wake.set()))
        signal.signal(signal.SIGHUP, lambda *_: setattr(self, "_mtime", 0))
        cfg = self.cfg
        if cfg.mqtt.enabled:
            self._start_mqtt()
        if cfg.server.enabled:
            self.server = StatusServer(cfg.server, self, stale_after=max(60, cfg.interval_seconds * 6))
            self.server.start()
        self.engine.notifier.emit("startup", "VPN watchdog started",
                                  f"{len(cfg.groups)} group(s), {'DRY-RUN (no changes)' if cfg.dry_run else 'LIVE'}",
                                  key="startup", groups=len(cfg.groups), mode="DRY-RUN" if cfg.dry_run else "LIVE")
        while not self.stop_event.is_set():
            self.reload_if_changed()
            self.engine.tick()
            self.engine.wake.wait(self.cfg.interval_seconds)
            self.engine.wake.clear()
        log.info("shutting down")
        self.store.save(force=True)
        if self.mqtt:
            self.mqtt.stop()
        if self.server:
            self.server.stop()
