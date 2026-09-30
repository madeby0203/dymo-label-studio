import base64
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from PIL import ImageOps

sys.path.insert(0, str(Path(__file__).parents[1] / "app"))
import label_renderer
import mqtt_bridge
import templating


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for name in ("HA_URL", "HA_TOKEN", "PRINTER_CONNECTION", "PRINTER_DEVICE", "CUPS_PRINTER", "RESET_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    import app as module
    for attribute, file_name in (("DATA_DIR", ""), ("TEMPLATES_FILE", "templates.json"), ("SETTINGS_FILE", "settings.json"),
                                 ("AUTH_FILE", "auth.json"), ("SECRET_KEY_FILE", "secret_key")):
        monkeypatch.setattr(module, attribute, tmp_path / file_name if file_name else tmp_path)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    module.app.secret_key = module._secret_key()
    printed = []
    monkeypatch.setattr(module, "make_printer", lambda settings: type("P", (), {"print_image": lambda self, image, copies, rotate: printed.append((image, copies, rotate))})())
    module.printed = printed
    return module


def test_dates_are_formatted_and_today_is_resolved_at_print_time():
    assert label_renderer.format_date("2026-09-29", "%d-%m-%Y") == "29-09-2026"
    assert label_renderer.format_date("today", "%Y-%m-%d") == date.today().isoformat()
    assert label_renderer.format_date(True) == date.today().strftime(label_renderer.DEFAULT_DATE_FORMAT)
    assert label_renderer.format_date("") is None
    assert label_renderer.format_date("best before summer") == "best before summer"


def test_template_print_applies_overrides(app_module):
    client = app_module.app.test_client()
    client.post("/api/templates", json={"name": "Opened", "text": "Opened", "date": "today", "copies": "1"})
    response = client.post("/api/templates/Opened/print", json={"copies": 2})
    assert response.json == {"ok": True, "message": "Printed Opened"}
    assert app_module.printed[-1][1] == 2


def test_unknown_template_is_an_error(app_module):
    response = app_module.app.test_client().post("/api/print", json={"template": "missing"})
    assert response.status_code == 500
    assert "Unknown template" in response.json["message"]


def test_printer_settings_come_from_the_environment(app_module, monkeypatch):
    monkeypatch.setenv("PRINTER_DEVICE", "/dev/usb/lp1")
    monkeypatch.setenv("MQTT_PASSWORD", "secret")
    settings = app_module.app.test_client().get("/api/settings").json
    assert settings["device"] == "/dev/usb/lp1" and settings["backend"] == "direct"
    assert "secret" not in json.dumps(settings)


# Templates without Home Assistant

def test_templates_are_rendered_locally_without_home_assistant(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%d-%m")
    assert templating.render_template("{{ (now() + timedelta(days=1)).strftime('%d-%m') }}") == tomorrow
    assert templating.resolve_templates({"text": "Opened {{ today().year }}", "copies": 1}) == {"text": f"Opened {date.today().year}", "copies": 1}


def test_entity_states_explain_that_they_need_home_assistant(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    with pytest.raises(ValueError, match="HA_URL and HA_TOKEN"):
        templating.render_template("{{ states('sensor.x') }}")


def test_local_templates_are_sandboxed(monkeypatch):
    monkeypatch.delenv("HA_URL", raising=False)
    with pytest.raises(ValueError, match="Template error"):
        templating.render_template("{{ now().__class__.__init__.__globals__ }}")


# Password protection

def _set_password(client, password="correct horse", current=None):
    return client.post("/api/auth", json={"password": password, "current_password": current})


def test_password_protection_is_off_by_default(app_module):
    client = app_module.app.test_client()
    assert client.get("/api/auth").json == {"enabled": False}
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 302


def test_setting_a_password_protects_everything_but_keeps_this_browser_signed_in(app_module):
    client = app_module.app.test_client()
    assert _set_password(client, "short").status_code == 400
    assert _set_password(client).json == {"ok": True}
    assert "correct horse" not in (app_module.AUTH_FILE).read_text()
    assert client.get("/api/settings").status_code == 200

    other = app_module.app.test_client()
    assert other.get("/api/settings").status_code == 401
    page = other.get("/")
    assert page.status_code == 302 and page.headers["Location"].endswith("login")
    assert other.get("/favicon.svg").status_code == 200
    assert other.post("/login", data={"password": "wrong"}).status_code == 401
    assert other.post("/login", data={"password": "correct horse"}).status_code == 302
    assert other.get("/api/settings").status_code == 200


def test_changing_the_password_signs_out_other_browsers(app_module):
    client, other = app_module.app.test_client(), app_module.app.test_client()
    _set_password(client)
    other.post("/login", data={"password": "correct horse"})
    assert _set_password(client, "battery staple", current="wrong").status_code == 403
    assert _set_password(client, "battery staple", current="correct horse").status_code == 200
    assert client.get("/api/settings").status_code == 200
    assert other.get("/api/settings").status_code == 401


def test_turning_off_the_password_needs_the_current_one(app_module):
    client = app_module.app.test_client()
    _set_password(client)
    assert client.delete("/api/auth", json={"current_password": "wrong"}).status_code == 403
    assert client.delete("/api/auth", json={"current_password": "correct horse"}).status_code == 200
    assert app_module.app.test_client().get("/api/settings").status_code == 200


def test_scripts_can_print_with_basic_auth(app_module):
    _set_password(app_module.app.test_client())
    script = app_module.app.test_client()
    body = {"text": "Milk"}
    assert script.post("/api/print", json=body).status_code == 401
    wrong = {"Authorization": "Basic " + base64.b64encode(b":nope").decode()}
    assert script.post("/api/print", json=body, headers=wrong).status_code == 401
    right = {"Authorization": "Basic " + base64.b64encode(b"any:correct horse").decode()}
    assert script.post("/api/print", json=body, headers=right).json["ok"] is True


def test_reset_password_turns_protection_off_at_start(app_module, monkeypatch):
    _set_password(app_module.app.test_client())
    monkeypatch.setenv("RESET_PASSWORD", "true")
    monkeypatch.setattr(app_module, "_start_mqtt", lambda: None)
    monkeypatch.setattr(app_module.app, "run", lambda **kwargs: None)
    app_module.main()
    assert not app_module.auth_enabled()


# MQTT

class FakeClient:
    def __init__(self):
        self.published = {}

    def publish(self, topic, payload, retain=False):
        self.published[topic] = payload

    def subscribe(self, topics):
        self.subscribed = topics


class Message:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


class ReasonCode:
    is_failure = False


def _bridge(templates, printed, **options):
    bridge = mqtt_bridge.MqttBridge("broker", 1883, None, None, lambda: templates, lambda request: printed.append(request) or "Printed", **options)
    bridge.client = FakeClient()
    bridge._on_connect(bridge.client, None, None, ReasonCode(), None)
    return bridge


def _wait_for(items):
    for _ in range(50):
        if items:
            return
        time.sleep(0.01)


def test_bridge_publishes_a_button_per_template_and_prints_on_press():
    printed = []
    bridge = _bridge([{"name": "Opened today", "icon": "mdi:fridge"}], printed)
    config = json.loads(bridge.client.published["homeassistant/button/dymo_label_studio_template_opened_today/config"])
    assert config["name"] == "Print Opened today" and config["icon"] == "mdi:fridge"
    assert config["command_topic"] == "dymo_label_studio/template/opened_today/press"
    assert bridge.client.published["dymo_label_studio/status"] == "online"
    assert json.loads(bridge.client.published["dymo_label_studio/templates"]) == [
        {"name": "Opened today", "press_topic": "dymo_label_studio/template/opened_today/press"}]

    bridge._on_message(bridge.client, None, Message(config["command_topic"], b"PRESS"))
    _wait_for(printed)
    assert printed == [{"template": "Opened today"}]


def test_bridge_removes_buttons_of_deleted_templates():
    templates = [{"name": "A"}, {"name": "B"}]
    bridge = _bridge(templates, [])
    templates.pop()
    bridge.publish_templates()
    assert bridge.client.published["homeassistant/button/dymo_label_studio_template_b/config"] == ""
    # A retained button left over from an earlier run is removed as well.
    bridge._on_message(bridge.client, None, Message("homeassistant/button/dymo_label_studio_template_old/config", b"{}"))
    assert bridge.client.published["homeassistant/button/dymo_label_studio_template_old/config"] == ""


def test_bridge_without_discovery_still_prints_on_its_own_topics():
    printed = []
    bridge = _bridge([{"name": "A"}], printed, topic="labels/kitchen", discovery_prefix=None)
    assert not any(topic.startswith("homeassistant/") for topic in bridge.client.published)
    assert [topic for topic, qos in bridge.client.subscribed] == ["labels/kitchen/template/+/press", "labels/kitchen/print"]
    bridge._on_message(bridge.client, None, Message("labels/kitchen/print", b'{"text": "Milk"}'))
    bridge._on_message(bridge.client, None, Message("labels/kitchen/template/a/press", b""))
    for _ in range(50):
        if len(printed) == 2:
            break
        time.sleep(0.01)
    assert sorted(printed, key=str) == sorted([{"text": "Milk"}, {"template": "A"}], key=str)


# Settings

def test_settings_keep_valid_favourites_and_default(app_module):
    client = app_module.app.test_client()
    client.post("/api/settings", json={"backend": "direct", "favorite_labels": ["99010", "bogus"], "default_label": "11354"})
    settings = client.get("/api/settings").json
    assert settings["favorite_labels"] == ["99010"] and settings["default_label"] == "99010"


def test_print_corrections_move_the_printed_label_only(app_module):
    client = app_module.app.test_client()
    client.post("/api/settings", json={"backend": "direct", "favorite_labels": ["11354"],
                                       "label_offsets": {"11354": {"x": 0, "y": -2}, "99010": {"y": 3}, "bogus": {"y": 1}}})
    settings = client.get("/api/settings").json
    # Only non-zero corrections of known labels are kept, rounded and limited.
    assert settings["label_offsets"] == {"11354": {"y": -2.0}, "99010": {"y": 3.0}}

    body = {"text": "", "graphic": "qr", "qr": "x", "label_size": "11354"}
    client.post("/api/print", json=body)
    printed = app_module.printed[-1][0]
    preview = app_module.render_request(body)
    shift = label_renderer.mm(2)
    # The printed QR code sits 2 mm higher than in the preview.
    inverted = lambda image: ImageOps.invert(image.convert("L"))
    assert inverted(printed).getbbox()[1] == inverted(preview).getbbox()[1] - shift


def test_network_printer_is_off_by_default_and_never_shared_through_cups(app_module, monkeypatch):
    assert app_module.app.test_client().get("/api/settings").json["print_server"] is False
    started = []
    monkeypatch.setattr(app_module.network_printer, "start", lambda *args: started.append(args))
    client = app_module.app.test_client()
    client.post("/api/settings", json={"backend": "cups", "print_server": True, "favorite_labels": ["11354"]})
    assert client.get("/api/settings").json["print_server"] is False
    time.sleep(0.2)
    assert started == []
    client.post("/api/settings", json={"backend": "direct", "print_server": True, "favorite_labels": ["11354"]})
    _wait_for(started)
    assert started and started[0][1] == ["11354"]
