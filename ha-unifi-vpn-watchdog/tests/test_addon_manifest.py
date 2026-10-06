"""HA's Configuration form only renders options that have a default, so every schema key must be in `options`."""
from pathlib import Path

import yaml

MANIFEST = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text())


def test_every_schema_option_is_visible_in_the_form():
    hidden = set(MANIFEST["schema"]) - set(MANIFEST["options"])
    assert hidden <= {"control_token"}, f"options HA will hide in the Configuration tab: {sorted(hidden)}"


def test_mqtt_fields_are_present_with_defaults():
    o = MANIFEST["options"]
    assert {"mqtt_host", "mqtt_port", "mqtt_username", "mqtt_password"} <= set(o)
    assert o["mqtt_port"] == 1883


def test_every_option_is_consumed_by_the_entrypoint():
    from vpn_watchdog import ha_entry
    import inspect
    src = inspect.getsource(ha_entry)
    for key in MANIFEST["options"]:
        assert f'"{key}"' in src, f"option {key} is defined but ha_entry never reads it"


def test_addon_has_icon_and_logo():
    """Home Assistant shows icon.png / logo.png from the add-on folder; without them the app has no icon in the list."""
    root = Path(__file__).resolve().parents[1]
    for name, size in (("icon.png", (128, 128)), ("logo.png", None)):
        data = (root / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
        w, h = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
        assert (w, h) == size if size else w > h, f"{name} has an unexpected size {w}x{h}"
