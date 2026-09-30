"""Print labels over MQTT, and offer templates as buttons through MQTT discovery.

Topics, under the base topic (``MQTT_TOPIC``, default ``dymo_label_studio``):

- ``<base>/print``: publish a label request as JSON to print it, e.g. ``{"template": "Opened"}``
  or ``{"text": "Milk", "icon": "mdi:fridge"}``.
- ``<base>/template/<slug>/press``: publish anything to print that template.
- ``<base>/templates``: the saved templates with their press topics (retained JSON).
- ``<base>/last_print``: the result of the latest print; ``<base>/status``: ``online``/``offline``.

With a discovery prefix (``MQTT_DISCOVERY_PREFIX``, default ``homeassistant``) every template is
also announced as a button, which Home Assistant and other discovery-aware systems pick up.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Callable

import paho.mqtt.client as mqtt


log = logging.getLogger(__name__)

DEFAULT_TOPIC = "dymo_label_studio"
DEFAULT_DISCOVERY_PREFIX = "homeassistant"


def template_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "template"


class MqttBridge:
    def __init__(self, host: str, port: int, username: str | None, password: str | None,
                 load_templates: Callable[[], list[dict]], print_label: Callable[[dict], str],
                 topic: str = DEFAULT_TOPIC, discovery_prefix: str | None = DEFAULT_DISCOVERY_PREFIX) -> None:
        self.host, self.port = host, port
        self.load_templates = load_templates
        self.print_label = print_label
        self.topic = topic.strip("/") or DEFAULT_TOPIC
        self.discovery_prefix = (discovery_prefix or "").strip("/") or None
        # Discovery IDs follow the base topic, so two instances with different topics don't clash.
        self.object_prefix = re.sub(r"[^a-z0-9]+", "_", self.topic.lower()).strip("_")
        self.button_prefix = f"{self.object_prefix}_template_"
        self.device = {
            "identifiers": [self.object_prefix],
            "name": "Label Studio for DYMO",
            "manufacturer": "Label Studio for DYMO",
            "model": "DYMO LabelWriter",
        }
        self.connected = False
        self._buttons: dict[str, str] = {}
        self._lock = threading.Lock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.object_prefix)
        if username:
            self.client.username_pw_set(username, password or None)
        self.client.will_set(self._topic("status"), "offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

    def _topic(self, *parts: str) -> str:
        return "/".join((self.topic, *parts))

    def start(self) -> None:
        log.info("Connecting to MQTT broker %s:%s", self.host, self.port)
        self.client.connect_async(self.host, self.port)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("Connected to MQTT broker; topics under %s/", self.topic)
        self.connected = True
        subscriptions = [(self._topic("template", "+", "press"), 0), (self._topic("print"), 0)]
        if self.discovery_prefix:
            # Retained discovery messages reveal buttons of templates deleted while we were offline.
            subscriptions.append((f"{self.discovery_prefix}/button/+/config", 0))
            client.publish(f"{self.discovery_prefix}/sensor/{self.object_prefix}_last_print/config", json.dumps({
                "name": "Last print",
                "unique_id": f"{self.object_prefix}_last_print",
                "state_topic": self._topic("last_print"),
                "availability_topic": self._topic("status"),
                "icon": "mdi:printer",
                "device": self.device,
            }), retain=True)
        client.subscribe(subscriptions)
        self.publish_templates()
        client.publish(self._topic("status"), "online", retain=True)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self.connected = False
        log.warning("Disconnected from MQTT broker: %s", reason_code)

    def publish_templates(self) -> None:
        """Publish the template list, and a discovery button for every template."""
        if not self.connected:
            return
        buttons = {template_slug(t["name"]): t for t in self.load_templates() if t.get("name")}
        with self._lock:
            stale = set(self._buttons) - set(buttons)
            self._buttons = {slug: t["name"] for slug, t in buttons.items()}
        self.client.publish(self._topic("templates"), json.dumps([
            {"name": template["name"], "press_topic": self._topic("template", slug, "press")}
            for slug, template in buttons.items()
        ]), retain=True)
        if not self.discovery_prefix:
            return
        for slug in stale:
            self._remove_button(slug)
        for slug, template in buttons.items():
            icon = template.get("icon") or ""
            self.client.publish(self._config_topic(slug), json.dumps({
                "name": f"Print {template['name']}",
                "unique_id": self.button_prefix + slug,
                "command_topic": self._topic("template", slug, "press"),
                "availability_topic": self._topic("status"),
                "icon": icon if icon.startswith("mdi:") else "mdi:printer",
                "device": self.device,
            }), retain=True)

    def _config_topic(self, slug: str) -> str:
        return f"{self.discovery_prefix}/button/{self.button_prefix}{slug}/config"

    def _remove_button(self, slug: str) -> None:
        self.client.publish(self._config_topic(slug), "", retain=True)

    def _on_message(self, client, userdata, message) -> None:
        topic = message.topic
        if self.discovery_prefix and topic.startswith(f"{self.discovery_prefix}/button/"):
            object_id = topic.split("/")[-2]
            slug = object_id[len(self.button_prefix):]
            with self._lock:
                known = slug in self._buttons
            if object_id.startswith(self.button_prefix) and message.payload and not known:
                self._remove_button(slug)
            return
        if topic == self._topic("print"):
            try:
                request = json.loads(message.payload or b"{}")
                if not isinstance(request, dict):
                    raise ValueError("expected a JSON object")
            except ValueError as exc:
                self._report(f"Invalid print request: {exc}")
                return
        else:
            with self._lock:
                name = self._buttons.get(topic.split("/")[-2])
            if name is None:
                return
            request = {"template": name}
        # Printing can take a while; keep the MQTT network loop responsive.
        threading.Thread(target=self._print, args=(request,), daemon=True).start()

    def _print(self, request: dict) -> None:
        try:
            self._report(self.print_label(request))
        except Exception as exc:  # reported over MQTT rather than lost in a thread
            log.exception("Print over MQTT failed")
            self._report(f"Failed: {exc}")

    def _report(self, message: str) -> None:
        self.client.publish(self._topic("last_print"), message[:255], retain=True)
