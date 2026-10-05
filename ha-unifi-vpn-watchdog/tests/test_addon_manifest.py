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
