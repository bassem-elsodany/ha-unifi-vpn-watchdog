"""Home Assistant integration through MQTT discovery: entities for each group + pause / force-tunnel controls."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
import paho.mqtt.client as mqtt

from .config import MqttCfg

log = logging.getLogger("vpn_watchdog.mqtt")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def supervisor_mqtt(transport: httpx.BaseTransport | None = None) -> dict[str, Any]:
    """Broker details the Supervisor hands to add-ons that declare `services: mqtt:want` (Mosquitto add-on)."""
    with httpx.Client(timeout=8, transport=transport, headers={"Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"}) as c:
        r = c.get("http://supervisor/services/mqtt")
    if r.status_code >= 400:
        # The body says why (service not available, access denied, ...); httpx's own message hides it.
        raise RuntimeError(f"Supervisor answered HTTP {r.status_code} for /services/mqtt: {r.text[:200].strip()}")
    return r.json()["data"]


class MqttPublisher:
    def __init__(self, cfg: MqttCfg, get_engine):
        self.cfg = cfg
        self.get_engine = get_engine
        self._announced: dict[str, list[str]] = {}
        self._last: dict[str, str] = {}
        host, port, user, pw = cfg.host, cfg.port, cfg.username, cfg.password
        if cfg.supervisor:
            try:
                d = supervisor_mqtt()
                host, port, user, pw = d["host"], d["port"], d.get("username"), d.get("password")
            except Exception as e:  # noqa: BLE001 - e.g. 400: no Mosquitto add-on registered with the Supervisor
                if os.environ.get("MQTT_HOST"):      # broker given in the add-on Configuration tab
                    host = os.environ["MQTT_HOST"]
                    port = int(os.environ.get("MQTT_PORT") or 1883)
                    user = os.environ.get("MQTT_USERNAME") or None
                    pw = os.environ.get("MQTT_PASSWORD") or None
                    log.info("Supervisor offers no MQTT service (%s); using broker %s:%s from the add-on options", type(e).__name__, host, port)
                elif cfg.host != "127.0.0.1":        # broker given in config.yaml
                    log.info("Supervisor offers no MQTT service (%s); using mqtt.host %s from config.yaml", type(e).__name__, host)
                else:
                    raise
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha-unifi-vpn-watchdog")
        if user:
            self.client.username_pw_set(user, pw)
        self.avail = f"{cfg.base_topic}/availability"
        self.client.will_set(self.avail, "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.connect_async(host, port)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.publish(self.avail, "offline", retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # -------------------------------------------------------------- inbound commands from Home Assistant
    def _on_connect(self, client, userdata, flags, rc, props=None):
        log.info("mqtt connected")
        client.publish(self.avail, "online", retain=True)
        client.subscribe(f"{self.cfg.base_topic}/+/pause/set")
        client.subscribe(f"{self.cfg.base_topic}/+/tunnel/set")
        self._announced.clear()
        self._last.clear()

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        payload = msg.payload.decode()
        eng = self.get_engine()
        group = next((g.name for g in eng.cfg.groups if _slug(g.name) == parts[1]), None)
        if not group:
            return
        if parts[2] == "pause":
            eng.submit("pause" if payload.upper() == "ON" else "resume", group)
        elif parts[2] == "tunnel" and payload and payload != "(auto)":
            eng.submit("switch", group, payload)

    # -------------------------------------------------------------- outbound state
    def publish(self, status: dict[str, Any]) -> None:
        for gname, g in status.get("groups", {}).items():
            gid = _slug(gname)
            ladder = g["order"]
            if self._announced.get(gid) != ladder:
                self._announce(gname, gid, ladder)
                self._announced[gid] = list(ladder)
            base = f"{self.cfg.base_topic}/{gid}"
            self._pub(f"{base}/active", g["active"] or "none")
            self._pub(f"{base}/position", g["position"] or "none")
            self._pub(f"{base}/healthy", "ON" if g["healthy"] else "OFF")
            self._pub(f"{base}/decision", (g["decision"] or "")[:250])
            self._pub(f"{base}/paused", "ON" if g["paused"] else "OFF")

    def _pub(self, topic: str, value: str) -> None:
        if self._last.get(topic) != value:
            self._last[topic] = value
            self.client.publish(topic, value, retain=True)

    def _announce(self, gname: str, gid: str, ladder: list[str]) -> None:
        dev = {"identifiers": [f"vpn_watchdog_{gid}"], "name": f"VPN {gname}", "manufacturer": "unifi-vpn-watchdog"}
        base = f"{self.cfg.base_topic}/{gid}"
        pfx = self.cfg.discovery_prefix

        def cfg(kind: str, key: str, name: str, **extra: Any) -> None:
            body = {"name": name, "unique_id": f"vpn_watchdog_{gid}_{key}", "device": dev,
                    "availability_topic": self.avail, **extra}
            self.client.publish(f"{pfx}/{kind}/vpn_watchdog_{gid}/{key}/config", json.dumps(body), retain=True)

        cfg("sensor", "active", "Active tunnel", state_topic=f"{base}/active", icon="mdi:vpn")
        cfg("sensor", "position", "Fallback position", state_topic=f"{base}/position", icon="mdi:format-list-numbered")
        cfg("sensor", "decision", "Last decision", state_topic=f"{base}/decision", entity_category="diagnostic")
        cfg("binary_sensor", "healthy", "Healthy", state_topic=f"{base}/healthy", device_class="connectivity")
        cfg("switch", "paused", "Failover paused", state_topic=f"{base}/paused", command_topic=f"{base}/pause/set",
            icon="mdi:pause-circle")
        cfg("select", "tunnel", "Force tunnel", state_topic=f"{base}/active", command_topic=f"{base}/tunnel/set",
            options=ladder)
