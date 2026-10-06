"""Configuration: YAML + ${ENV} interpolation, strict validation, per-group overrides."""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from .alerts import EVENTS, unknown_placeholders
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ConfigError(Exception):
    pass


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
    quarantine: QuarantineCfg = QuarantineCfg()
    on_exhausted: Literal["keep", "kill_switch"] = "keep"


class FailbackCfg(_M):
    enabled: bool = True
    check_interval_seconds: int = Field(120, ge=10)
    stable_seconds: int = Field(300, ge=0)


class NetRef(_M):
    """A VLAN of a group. The UniFi id is what counts (a rename in UniFi changes nothing); the name is a label, and the way an
    older config that only has names is found again the first time (the id is then filled in automatically)."""
    id: str | None = None
    name: str = ""

    @model_validator(mode="before")
    @classmethod
    def _plain(cls, v: Any) -> Any:
        return {"name": v} if isinstance(v, str) else v

    @model_validator(mode="after")
    def _need_one(self) -> "NetRef":
        if not (self.id or self.name):
            raise ValueError("a network needs an id or a name")
        return self


class OrderItem(_M):
    """One position in the fallback order."""
    tunnel: str                          # the tunnel's name when it was chosen: a label, and how an older config is matched the first time
    id: str | None = None                # the tunnel's UniFi id: what is matched first, so renaming the VPN client in UniFi changes nothing
    expect_country: str | None = None    # optional, typed by you: country the exit-IP test must see for this tunnel

    @model_validator(mode="after")
    def _norm(self) -> "OrderItem":
        if self.expect_country:
            self.expect_country = self.expect_country.upper()
        return self


class GroupCfg(_M):
    name: str
    networks: list[NetRef]               # VLANs whose internet traffic the group's policies steer (by UniFi id; a name alone is accepted and upgraded)
    order: list[OrderItem] = Field(default_factory=list)   # fallback sequence: #1 is the most preferred, then #2, #3, ...
    kill_switch: bool | None = None      # None = leave the route's kill switch alone
    overrides: dict[str, Any] = Field(default_factory=dict)  # deep-merged over detection/switching/failback

    @field_validator("order", mode="before")
    @classmethod
    def _allow_plain_names(cls, v: Any) -> Any:
        return [{"tunnel": x} if isinstance(x, str) else x for x in (v or [])]

    @model_validator(mode="after")
    def _valid(self) -> "GroupCfg":
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("a group needs a name")
        if not self.networks:
            raise ValueError(f"group {self.name!r} needs at least one network: tick the networks that should use the VPN")
        keys = [i.id or i.tunnel for i in self.order]
        dup = sorted({i.tunnel for i in self.order if keys.count(i.id or i.tunnel) > 1})
        if dup:
            raise ValueError(f"group {self.name!r}: tunnel listed twice in the fallback order: {dup}")
        return self


class JobCfg(_M):
    """A job a VPN group runs besides failover. `rotation`: every so often move the group to another tunnel in its order."""
    kind: Literal["rotation"] = "rotation"
    group: str
    enabled: bool = True
    every: int = Field(1, ge=1, le=999)
    unit: Literal["hours", "days", "weeks"] = "days"
    at: str = "03:00"                      # time of day (the add-on's clock) for days and weeks; hours count from when the job starts
    go_to: Literal["next", "random"] = "next"

    @model_validator(mode="after")
    def _valid(self) -> "JobCfg":
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", self.at.strip())
        if not m:
            raise ValueError(f"rotation of {self.group!r}: `at` must be a time like 03:00")
        self.at = f"{int(m.group(1)):02d}:{m.group(2)}"
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


class Config(_M):
    interval_seconds: int = Field(15, ge=5)
    state_file: str = "/data/state.json"
    log: LogCfg = LogCfg()
    unifi: UnifiCfg
    detection: DetectionCfg = DetectionCfg()
    probe: ProbeCfg = ProbeCfg()
    switching: SwitchingCfg = SwitchingCfg()
    failback: FailbackCfg = FailbackCfg()
    groups: list[GroupCfg] = Field(default_factory=list)   # created by the user in the UI; none on a fresh install
    jobs: list[JobCfg] = Field(default_factory=list)       # extra jobs per group (rotation); failover is every group's own job
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
        owner: dict[str, str] = {}
        for g in self.groups:
            for ref in g.networks:
                key = ref.id or ref.name
                if key in owner and owner[key] != g.name:
                    raise ValueError(f"the VLAN {ref.name or ref.id!r} is in two groups ({owner[key]!r} and {g.name!r}); a VLAN can belong to one group")
                owner[key] = g.name
        seen: set[tuple[str, str]] = set()
        for j in self.jobs:
            if j.group not in names:
                raise ValueError(f"{j.kind} job: there is no VPN group called {j.group!r}")
            if (j.kind, j.group) in seen:
                raise ValueError(f"group {j.group!r} has two {j.kind} jobs; a group can have one job of each kind")
            seen.add((j.kind, j.group))
        return self

    def job_for(self, group: str, kind: str = "rotation") -> JobCfg | None:
        return next((j for j in self.jobs if j.group == group and j.kind == kind), None)

    def settings_for(self, group: GroupCfg) -> GroupSettings:
        base = {
            "detection": self.detection.model_dump(),
            "switching": self.switching.model_dump(),
            "failback": self.failback.model_dump(),
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


def removed_settings(raw: dict[str, Any]) -> list[str]:
    """Settings that earlier versions had and that no longer exist. They are reported, never converted or guessed."""
    errs: list[str] = []
    if "standby" in raw or (isinstance(raw.get("groups"), list) and any(isinstance(g, dict) and isinstance(g.get("overrides"), dict) and "standby" in g["overrides"] for g in raw["groups"])):
        errs.append("`standby` was removed: only the tunnel in use stays connected. Several tunnels up at once for the same VLAN means "
                    "traffic can leave through different exit IPs and looks odd to firewalls and VPN providers. Delete the standby section.")
    if "naming" in raw:
        errs.append("`naming` was removed: tunnel names are never interpreted. Choose each step's tunnels by name or pattern.")

    def city(d: Any, where: str) -> None:
        if isinstance(d, dict) and isinstance(d.get("switching"), dict) and "prefer_different_city" in d["switching"]:
            errs.append(f"{where}switching.prefer_different_city was removed (cities are not a concept any more).")

    city(raw, "")
    if isinstance(raw.get("switching"), dict) and "rename_route_to_tunnel" in raw["switching"]:
        errs.append("switching.rename_route_to_tunnel was removed: every tunnel now keeps its own routing policy and the watchdog only turns them on and off.")
    for g in raw.get("groups") or []:
        if not isinstance(g, dict):
            continue
        city(g.get("overrides"), f"groups[{g.get('name')}].overrides.")
        if "route_id" in g:
            errs.append(f"groups[{g.get('name')}].route_id was removed: each tunnel has its own routing policy, found automatically.")
        if "ladder" in g:
            errs.append(f"groups[{g.get('name')}].ladder was replaced by `order`: list the tunnels in the sequence you want, first = "
                        "most preferred, e.g. order: [My-Tunnel-A, My-Tunnel-B, {tunnel: My-Tunnel-C, expect_country: DE}]. "
                        "Or set it in the web UI: Settings > Fallback order.")
    return errs


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
    errs = removed_settings(raw)
    if errs:
        raise ConfigError("\n".join(errs))
    try:
        cfg = Config.model_validate(raw)
        for g in cfg.groups:
            cfg.settings_for(g)  # validate overrides eagerly
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
