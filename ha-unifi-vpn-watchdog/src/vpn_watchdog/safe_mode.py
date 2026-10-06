"""Safe mode: a rejected configuration must never lock you out.

If config.yaml is invalid at start-up the failover is OFF, but the web UI still starts with the error and the YAML
editor so the file can be corrected. As soon as it validates, normal operation starts.
"""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from pathlib import Path

from .config import ConfigError, ServerCfg, load_raw, parse_config
from .server import StatusServer

log = logging.getLogger("vpn_watchdog.safe")


class _Engine:
    def __init__(self, error: str):
        self.error = error
        self.wake = threading.Event()
        self.cfg = None

    @property
    def last_tick(self) -> float:
        return time.time()          # keep health checks green so the container is not restarted in a loop

    @property
    def last_error(self) -> str:
        return self.error

    def status(self) -> dict:
        return {"safe_mode": True, "last_tick": time.time(), "error": self.error, "groups": {},
                "tunnels": {}, "events": [], "interval_seconds": 0}

    def submit(self, *a) -> None:
        pass

    def last_snapshot(self):
        return None


class SafeApp:
    def __init__(self, path: Path, env: dict[str, str] | None, error: str):
        self.path, self.env = path, env
        self.engine = _Engine(error)

    def config_text(self) -> str:
        return self.path.read_text()

    def validate_text(self, text: str) -> str | None:
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
        return None

    _refuse = "Configuration is rejected, so settings are unavailable. Fix the YAML in the Advanced tab first."

    def get_settings(self) -> dict:
        return {"values": None, "meta": {}, "error": self._refuse}

    def save_settings(self, form: dict) -> str | None:
        return self._refuse

    def ha_notify_services(self) -> dict:
        return {"available": False, "current": None, "services": [], "error": None}

    def set_notify_service(self, s: str) -> str | None:
        return self._refuse

    def test_notify(self, s: str | None = None) -> str | None:
        return self._refuse


def _server_cfg(path: Path, env: dict[str, str] | None) -> ServerCfg:
    """Best effort: use the file's `server` section if it is readable, else sane defaults (ingress when in an add-on)."""
    e = dict(os.environ) if env is None else env
    base = {"host": "0.0.0.0", "port": 8080, "trust_ingress": bool(e.get("SUPERVISOR_TOKEN")),
            "control_token": e.get("WATCHDOG_CONTROL_TOKEN") or None}
    try:
        base.update({k: v for k, v in (load_raw(path.read_text()).get("server") or {}).items() if k in base})
        for k, v in list(base.items()):
            if isinstance(v, str) and "${" in v:
                base[k] = base["control_token"] if k == "control_token" else v
        return ServerCfg(**{k: v for k, v in base.items() if not (isinstance(v, str) and "${" in v)})
    except Exception:  # noqa: BLE001
        return ServerCfg(host="0.0.0.0", port=8080, trust_ingress=base["trust_ingress"], control_token=base["control_token"])


def wait_until_valid(path: str, env: dict[str, str] | None, error: str, stop: threading.Event) -> bool:
    """Serve the UI in safe mode until the file validates. Returns False if asked to stop first."""
    p = Path(path)
    app = SafeApp(p, env, error)
    srv = StatusServer(_server_cfg(p, env), app, stale_after=3600)
    srv.start()
    log.error("CONFIGURATION REJECTED, failover is OFF. Fix it in the web UI (Advanced tab). Reason:\n%s", error)
    try:
        while not stop.is_set():
            time.sleep(3)
            err = app.validate_text(p.read_text()) if p.exists() else "config file is missing"
            if err is None:
                log.info("configuration is valid now, starting normally")
                return True
            app.engine.error = err
    finally:
        srv.stop()
    return False
