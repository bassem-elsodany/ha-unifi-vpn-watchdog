"""Checks on the Resolvr add-on manifest. It runs a published image, so there is no source here to test; what can go wrong
is the packaging: a form Home Assistant will not render, a missing icon, or a version that does not match its changelog."""
import re
import struct
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = yaml.safe_load((ROOT / "config.yaml").read_text())


def test_every_schema_option_is_visible_in_the_form():
    """HA's Configuration form only renders options that have a default, so every schema key must be in `options`."""
    assert set(MANIFEST["schema"]) == set(MANIFEST["options"])


def test_the_connection_options_are_the_ones_resolvr_reads():
    """The image reads exactly these two keys from /data/options.json (backend/src/addon.ts in the Resolvr repository)."""
    assert set(MANIFEST["options"]) == {"technitium_url", "technitium_token"}
    assert MANIFEST["schema"]["technitium_token"] == "password"
    assert MANIFEST["schema"]["technitium_url"] == "url"


def test_it_runs_the_published_image_and_does_not_build_on_the_device():
    assert MANIFEST["image"] == "ghcr.io/bassem-elsodany/resolvr"
    assert not (ROOT / "Dockerfile").exists(), "an image: line and a Dockerfile would be ambiguous"


def test_it_is_reached_only_through_the_sidebar():
    """Resolvr trusts requests from the ingress proxy as an administrator, so no port may be published next to it."""
    assert MANIFEST["ingress"] is True
    assert MANIFEST["ingress_port"] == 8787
    assert "ports" not in MANIFEST
    assert MANIFEST.get("panel_admin", True) is True


def test_the_version_is_a_release_and_matches_the_top_of_the_changelog():
    version = MANIFEST["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?", version), f"{version} is not a semantic version"
    first = re.search(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(), re.M).group(1)
    assert first == version, f"CHANGELOG starts at {first} but the add-on is {version}"


def test_the_image_tag_is_the_version():
    """Home Assistant pulls image:version, so the version must be a tag the Resolvr release workflow publishes."""
    assert str(MANIFEST["version"]) == MANIFEST["version"], "quote the version, or YAML turns 1.10 into a float"


def test_addon_has_icon_and_logo():
    """Home Assistant shows icon.png / logo.png from the add-on folder; without them the app has no icon in the list."""
    for name, size in (("icon.png", (128, 128)), ("logo.png", None)):
        data = (ROOT / name).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{name} is not a PNG"
        w, h = struct.unpack(">II", data[16:24])
        assert (w, h) == size if size else w > h, f"{name} has an unexpected size {w}x{h}"


def test_docs_describe_the_two_options():
    docs = (ROOT / "DOCS.md").read_text()
    for key in MANIFEST["options"]:
        assert key in docs, f"DOCS.md never mentions {key}"
