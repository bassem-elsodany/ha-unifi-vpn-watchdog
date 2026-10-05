"""Configuration: YAML + ${ENV} interpolation, strict validation, per-group overrides."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from .alerts import EVENTS, unknown_placeholders
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ConfigError(Exception):
    pass


DEFAULT_NAMING = r"^(?P<iso>[A-Za-z]{2})__(?P<city>.+?)__(?P<id>\d+)__(?P<ip>\d{1,3}(?:\.\d{1,3}){3})$"
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


class _M(BaseModel):
    # Unknown keys are errors so typos in the config never silently disable a feature.
    model_config = ConfigDict(extra="forbid")


class UnifiCfg(_M):
    url: str = "https://10.0.1.1"
    api_key: str
    site: str = "default"
    verify_tls: bool = False
    timeout_seconds: float = 10
    retries: int = 2


class NamingCfg(_M):
    pattern: str = DEFAULT_NAMING

    @model_validator(mode="after")
    def _compiles(self) -> "NamingCfg":
        try:
            re.compile(self.pattern)
        except re.error as e:
            raise ValueError(f"naming.pattern is not a valid regex: {e}")
        return self


class BlackholeCfg(_M):
    enabled: bool = True
    min_tx_bps: int = Field(2000, ge=0)       # we are sending at least this much ...
    samples: int = Field(6, ge=1)             # ... and received exactly nothing for this many polls in a row


class DetectionCfg(_M):
    failure_threshold: int = Field(3, ge=1)          # consecutive bad status polls before the tunnel is declared down
    probe_failure_threshold: int = Field(2, ge=1)    # consecutive bad exit-IP probes before the tunnel is declared down
    probe_interval_seconds: int = Field(30, ge=5)
    blackhole: BlackholeCfg = BlackholeCfg()


class ProbeEndpoint(_M):
    name: str
    url: str
    format: Literal["json", "cloudflare_trace", "text"] = "json"
    ip_field: str = "ip"
    country_field: str | None = "country"


def _default_endpoints() -> list[ProbeEndpoint]:
    return [
        ProbeEndpoint(name="ipinfo", url="https://ipinfo.io/json", ip_field="ip", country_field="country"),
        ProbeEndpoint(name="cloudflare", url="https://www.cloudflare.com/cdn-cgi/trace", format="cloudflare_trace"),
        ProbeEndpoint(name="ifconfig.co", url="https://ifconfig.co/json", ip_field="ip", country_field="country_iso"),
    ]


class CanaryCfg(_M):
    mac: str | None = None                         # MAC of the container (macvlan) that is routed through the tested tunnel
    route_description: str = "WATCHDOG_CANARY"
    settle_seconds: float = 6


class ProbeCfg(_M):
    # canary: swap a dedicated client's route to any tunnel to test it before use (recommended)
    # remote: like canary, but the probe is run by a `ha-unifi-vpn-watchdog agent` on the canary host (use for HA add-ons)
    # direct: probe from the container's own egress (only valid for the tunnel it is routed through)
    # none:   status + throughput checks only
    mode: Literal["canary", "remote", "direct", "none"] = "none"
    endpoints: list[ProbeEndpoint] = Field(default_factory=_default_endpoints)
    require: int = 1
    check_country: bool = True                     # geo-IP databases disagree on VPN ranges; false = only "works and is not the WAN IP"
    timeout_seconds: float = 8
    wan_ip: str | None = None                      # None = learn it from UniFi; a probe returning it is a LEAK
    source_address: str | None = None
    remote_url: str | None = None                  # http://<agent-host>:8081 (probe.mode=remote)
    remote_token: str | None = None
    country_aliases: dict[str, list[str]] = Field(default_factory=lambda: {"GB": ["UK"]})
    canary: CanaryCfg = CanaryCfg()

    @model_validator(mode="after")
    def _canary_needs_mac(self) -> "ProbeCfg":
        if self.mode in ("canary", "remote") and not self.canary.mac:
            raise ValueError(f"probe.mode={self.mode} requires probe.canary.mac")
        if self.mode == "remote" and not self.remote_url:
            raise ValueError("probe.mode=remote requires probe.remote_url")
        if self.require < 1 or self.require > max(1, len(self.endpoints)):
            raise ValueError("probe.require must be between 1 and the number of endpoints")
        return self


class QuarantineCfg(_M):
    base_seconds: int = Field(120, ge=10)
    factor: float = 2
    max_seconds: int = 3600


class SwitchingCfg(_M):
    connect_timeout_seconds: float = Field(40, ge=5)
    min_hold_seconds: int = Field(60, ge=0)                  # minimum time between switches (hard failures bypass it)
    max_switches_per_hour: int = Field(6, ge=1)
    prefer_different_city: bool = True          # within a country, try other cities before other servers in the same city
    rename_route_to_tunnel: bool = True         # keep the route description equal to the active tunnel name
    quarantine: QuarantineCfg = QuarantineCfg()
    on_exhausted: Literal["keep", "kill_switch"] = "keep"


class FailbackCfg(_M):
    enabled: bool = True
    check_interval_seconds: int = Field(120, ge=10)
    stable_seconds: int = Field(300, ge=0)


class StandbyCfg(_M):
    warm: int = Field(2, ge=0)                # next N candidates kept enabled (connected) for instant failover
    disable_unused: bool = True  # disable managed tunnels that are neither active nor warm
    max_enabled: int = Field(6, ge=1)         # NordVPN allows 10 simultaneous connections per account


class LadderStep(_M):
    country: str | None = None           # ISO code, matched against the parsed tunnel name
    tunnels: list[str] = Field(default_factory=list)   # glob patterns on tunnel name
    prefer: list[str] = Field(default_factory=list)    # globs tried first inside this step
    exclude: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _something(self) -> "LadderStep":
        if not self.country and not self.tunnels:
            raise ValueError("a ladder step needs `country` and/or `tunnels`")
        if self.country:
            self.country = self.country.upper()
        return self


class GroupCfg(_M):
    name: str
    networks: list[str]                  # network names (or ids) whose internet traffic the route steers
    ladder: list[LadderStep]
    kill_switch: bool | None = None      # None = leave the route's kill switch alone
    route_id: str | None = None          # pin a specific route; otherwise discovered from `networks`
    overrides: dict[str, Any] = Field(default_factory=dict)  # deep-merged over detection/switching/failback/standby

    @model_validator(mode="after")
    def _non_empty(self) -> "GroupCfg":
        if not self.networks or not self.ladder:
            raise ValueError(f"group {self.name!r} needs networks and a ladder")
        return self


class NotifyCfg(_M):
    type: Literal["webhook", "ntfy", "telegram", "home_assistant"]
    events: list[str] = Field(default_factory=lambda: ["*"])
    throttle_seconds: int = 300
    url: str | None = None
    token: str | None = None
    topic: str | None = None          # ntfy
    chat_id: str | None = None        # telegram
    service: str = "notify.notify"    # home_assistant: any notify service, e.g. notify.mobile_app_pixel
    supervisor: bool = False          # home_assistant add-on: use the Supervisor proxy + SUPERVISOR_TOKEN
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _fields(self) -> "NotifyCfg":
        need = {"webhook": ["url"], "ntfy": ["url", "topic"], "telegram": ["token", "chat_id"],
                "home_assistant": [] if self.supervisor else ["url", "token"]}
        missing = [f for f in need[self.type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"notification type {self.type} requires {missing}")
        return self


class MqttCfg(_M):
    """Publishes Home Assistant entities through MQTT discovery and accepts commands from HA."""
    enabled: bool = False
    supervisor: bool = False          # HA add-on: read broker credentials from the Supervisor (Mosquitto add-on)
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
    base_topic: str = "vpn_watchdog"


class AlertCfg(_M):
    enabled: bool | None = None       # None = the default for that event
    title: str | None = None
    message: str | None = None


class ServerCfg(_M):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    control_token: str | None = None     # bearer token for the UI/API controls; empty = read-only
    trust_ingress: bool = False          # Home Assistant add-on: the Supervisor ingress proxy is already authenticated


class LogCfg(_M):
    level: str = "INFO"
    format: Literal["text", "json"] = "text"


class GroupSettings(_M):
    detection: DetectionCfg = DetectionCfg()
    switching: SwitchingCfg = SwitchingCfg()
    failback: FailbackCfg = FailbackCfg()
    standby: StandbyCfg = StandbyCfg()


class Config(_M):
    dry_run: bool = True                 # SAFE DEFAULT: log what would change, change nothing
    interval_seconds: int = Field(15, ge=5)
    state_file: str = "/data/state.json"
    log: LogCfg = LogCfg()
    unifi: UnifiCfg
    naming: NamingCfg = NamingCfg()
    detection: DetectionCfg = DetectionCfg()
    probe: ProbeCfg = ProbeCfg()
    switching: SwitchingCfg = SwitchingCfg()
    failback: FailbackCfg = FailbackCfg()
    standby: StandbyCfg = StandbyCfg()
    groups: list[GroupCfg]
    notifications: list[NotifyCfg] = Field(default_factory=list)
    alerts: dict[str, AlertCfg] = Field(default_factory=dict)    # per event: on/off, title, message
    mqtt: MqttCfg = MqttCfg()
    server: ServerCfg = ServerCfg()

    @model_validator(mode="after")
    def _alerts_complete(self) -> "Config":
        for name in self.alerts:
            if name not in EVENTS:
                raise ValueError(f"unknown alert {name!r}; known: {', '.join(EVENTS)}")
        full: dict[str, AlertCfg] = {}
        for name, d in EVENTS.items():
            a = self.alerts.get(name, AlertCfg())
            cfg = AlertCfg(enabled=d["enabled"] if a.enabled is None else a.enabled,
                           title=a.title or d["title"], message=a.message or d["message"])
            for field in ("title", "message"):
                bad = unknown_placeholders(getattr(cfg, field))
                if bad:
                    raise ValueError(f"alerts.{name}.{field}: unknown placeholder(s) {bad}")
            full[name] = cfg
        self.alerts = full
        return self

    @model_validator(mode="after")
    def _unique_groups(self) -> "Config":
        names = [g.name for g in self.groups]
        if len(set(names)) != len(names):
            raise ValueError("group names must be unique")
        if not self.groups:
            raise ValueError("at least one group is required")
        return self

    def naming_re(self) -> re.Pattern[str]:
        return re.compile(self.naming.pattern)

    def settings_for(self, group: GroupCfg) -> GroupSettings:
        base = {
            "detection": self.detection.model_dump(),
            "switching": self.switching.model_dump(),
            "failback": self.failback.model_dump(),
            "standby": self.standby.model_dump(),
        }
        try:
            return GroupSettings.model_validate(deep_merge(base, group.overrides))
        except ValidationError as e:
            raise ConfigError(f"group {group.name!r} overrides invalid:\n{format_errors(e)}") from e


def deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


_SENTINEL = "WDENVREF{}X"


def _protect(text: str) -> tuple[str, list[re.Match[str]]]:
    """Swap every ${VAR} for a YAML-safe placeholder so references work in block, flow and quoted contexts."""
    found: list[re.Match[str]] = []

    def sub(m: re.Match[str]) -> str:
        found.append(m)
        return _SENTINEL.format(len(found) - 1)

    return _ENV.sub(sub, text), found


def _restore(value: Any, found: list[re.Match[str]], env: dict[str, str]) -> Any:
    if isinstance(value, str):
        def sub(m: re.Match[str]) -> str:
            ref = found[int(m.group(1))]
            name, default = ref.group(1), ref.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            raise ConfigError(f"environment variable {name} is referenced in the config but not set")
        return re.sub(r"WDENVREF(\d+)X", sub, value)
    if isinstance(value, list):
        return [_restore(v, found, env) for v in value]
    if isinstance(value, dict):
        return {k: _restore(v, found, env) for k, v in value.items()}
    return value


def load_raw(text: str) -> dict[str, Any]:
    """YAML -> dict with `${VAR}` references left as literal text (for editors; parse_config resolves them)."""
    safe, found = _protect(text)
    try:
        raw = yaml.safe_load(safe) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    def back(v: Any) -> Any:
        if isinstance(v, str):
            return re.sub(r"WDENVREF(\d+)X", lambda m: found[int(m.group(1))].group(0), v)
        if isinstance(v, list):
            return [back(x) for x in v]
        if isinstance(v, dict):
            return {k: back(x) for k, x in v.items()}
        return v

    return back(raw)


def parse_config(text: str, env: dict[str, str] | None = None) -> Config:
    safe, found = _protect(text)
    try:
        raw = yaml.safe_load(safe) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    raw = _restore(raw, found, dict(os.environ) if env is None else env)
    try:
        cfg = Config.model_validate(raw)
        for g in cfg.groups:
            cfg.settings_for(g)  # validate overrides eagerly
        cfg.naming_re()
    except ValidationError as e:
        raise ConfigError(format_errors(e)) from e
    return cfg


def format_errors(e: ValidationError) -> str:
    """Readable, secret-free: pydantic's default text echoes the offending input value."""
    return "\n".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors(include_input=False, include_url=False))


def load_config(path: str | Path, env: dict[str, str] | None = None) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    return parse_config(p.read_text(), env)


def load_env_file(path: str | Path) -> dict[str, str]:
    """Minimal KEY=VALUE loader for local runs (Docker passes env itself)."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out
