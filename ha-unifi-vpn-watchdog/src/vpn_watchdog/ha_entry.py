"""Home Assistant add-on entrypoint: maps /data/options.json to environment variables, seeds the config, runs."""
from __future__ import annotations

import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

OPTIONS = Path(os.environ.get("HA_OPTIONS_FILE", "/data/options.json"))
CONFIG = Path(os.environ.get("WATCHDOG_CONFIG", "/config/config.yaml"))


def main() -> int:
    opts = json.loads(OPTIONS.read_text()) if OPTIONS.exists() else {}
    mapping = {"unifi_url": "UNIFI_URL", "unifi_api_key": "UNIFI_API_KEY", "log_level": "LOG_LEVEL",
               "notify_service": "NOTIFY_SERVICE", "control_token": "WATCHDOG_CONTROL_TOKEN"}
    for key, env in mapping.items():
        if opts.get(key):
            os.environ[env] = str(opts[key])
    if not os.environ.get("UNIFI_API_KEY"):
        print("Set `unifi_api_key` in the add-on Configuration tab, then start the add-on again.", file=sys.stderr)
        return 1
    if not CONFIG.exists():
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with resources.as_file(resources.files("vpn_watchdog").joinpath("templates/ha_config.yaml")) as tpl:
            shutil.copy(tpl, CONFIG)
        print(f"created {CONFIG} from the template (dry-run); open the add-on panel to edit it")
    from .cli import main as cli_main

    return cli_main(["run", "--config", str(CONFIG)])


if __name__ == "__main__":
    raise SystemExit(main())
