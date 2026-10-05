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

from .clock import Clock
from .config import Config, ConfigError, load_config, parse_config
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
        unifi = UniFiClient(cfg.unifi, cfg.naming_re(), dry_run=cfg.dry_run)
        tester = TunnelTester(cfg.probe, unifi, Prober(cfg.probe), self.clock)
        notifier = Notifier(cfg.notifications, self.clock, dry_run=cfg.dry_run)
        eng = Engine(cfg, unifi, tester, notifier, self.store, self.clock)
        if self.mqtt:
            eng.listeners.append(self.mqtt.publish)
        return eng

    # ---- used by the web UI
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
        except ConfigError as e:
            log.error("config reload rejected, keeping the previous config: %s", e)
            self.engine.notifier.emit("config_error", "VPN watchdog config rejected", str(e)[:300], level="warning", key="cfg")
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
            hint = ("the Supervisor has no MQTT service: install and start the Mosquitto broker add-on, or set "
                    "mqtt.supervisor: false with mqtt.host / username / password for an external broker"
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
                                  key="startup")
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
