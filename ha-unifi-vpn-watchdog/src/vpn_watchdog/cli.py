"""Command line: run | once | discover | validate | check | agent."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .app import App, setup_logging
from .config import ConfigError, load_config, load_env_file
from .ladder import resolve_ladder
from .probe import Prober


def _env(args) -> dict[str, str]:
    env = dict(os.environ)
    if args.env_file:
        env = {**load_env_file(args.env_file), **env}
    return env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ha-unifi-vpn-watchdog", description=__doc__)
    ap.add_argument("command", choices=["run", "once", "discover", "validate", "check", "agent"])
    ap.add_argument("--config", "-c", default=os.environ.get("WATCHDOG_CONFIG", "/config/config.yaml"))
    ap.add_argument("--env-file", help="KEY=VALUE file for local runs (Docker passes the environment itself)")
    live = ap.add_mutually_exclusive_group()
    live.add_argument("--dry-run", dest="dry_run", action="store_true", default=None, help="force dry-run")
    live.add_argument("--live", dest="dry_run", action="store_false", help="force live mode (ignores dry_run in the file)")
    ap.add_argument("--port", type=int, default=8081, help="agent listen port")
    ap.add_argument("--token", default=os.environ.get("AGENT_TOKEN"), help="agent bearer token")
    args = ap.parse_args(argv)
    env = _env(args)

    try:
        if args.command == "validate":
            cfg = load_config(args.config, env)
            print(f"OK: {len(cfg.groups)} group(s), dry_run={cfg.dry_run}, probe.mode={cfg.probe.mode}")
            return 0
        if args.command == "agent":
            cfg = load_config(args.config, env)
            setup_logging(cfg.log.level, cfg.log.format)
            from .agent import serve
            serve(cfg.probe, "0.0.0.0", args.port, args.token)
            return 0
        app = App(args.config, env, args.dry_run)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    setup_logging(app.cfg.log.level, app.cfg.log.format)
    if args.command == "run":
        app.run()
    elif args.command == "once":
        app.engine.tick()
        print(json.dumps(app.engine.status(), indent=2, default=str))
    elif args.command == "check":
        res = Prober(app.cfg.probe).probe(None, app.engine.unifi.snapshot().wan_ip)
        print(json.dumps(res.as_dict() | {"details": res.details}, indent=2))
    elif args.command == "discover":
        _discover(app)
    return 0


def _discover(app: App) -> None:
    snap = app.engine.unifi.snapshot()
    print("== VPN tunnels ==")
    for t in sorted(snap.tunnels.values(), key=lambda t: (t.iso or "~", t.city or "", t.name)):
        c = snap.connections.get(t.id)
        parsed = f"{t.iso}/{t.city}/{t.server_id}/{t.ip}" if t.iso else "UNPARSED (naming.pattern mismatch)"
        print(f"  {t.name:42} enabled={str(t.enabled):5} status={(c.status if c else '-'):11} {parsed}")
    print("== Policy routes ==")
    for r in snap.routes:
        nets = [snap.networks.get(n, n) for n in r.target_networks]
        tun = snap.tunnels.get(r.network_id or "")
        print(f"  {r.description:42} enabled={str(r.enabled):5} -> {tun.name if tun else r.network_id} nets={nets} macs={sorted(r.target_macs)} kill_switch={r.kill_switch}")
    print("== Networks ==")
    print("  " + ", ".join(sorted(snap.networks.values())))
    print(f"== WAN IP == {snap.wan_ip}")
    print("== Resolved ladders ==")
    for g in app.cfg.groups:
        print(f"  {g.name}: " + " > ".join(t.name for t in resolve_ladder(g, list(snap.tunnels.values()))))
