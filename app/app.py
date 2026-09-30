"""Label Studio for DYMO: the web app, printing, MQTT buttons and the optional network printer."""

from __future__ import annotations

from io import BytesIO
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import threading
import time

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, send_from_directory, session
from werkzeug.security import check_password_hash, generate_password_hash

import print_server
import templating
from label_renderer import (
    DATE_FORMATS, DEFAULT_FAVORITES, DEFAULT_LABEL_SIZE, DEFAULT_LANGUAGE, GAP_MM, HYPHENATION_LANGUAGES, LABEL_SIZES,
    MARGIN_MM, available_fonts, find_font_file, home_icons, mdi_icons, render_request, shift_image,
)
from mqtt_bridge import DEFAULT_DISCOVERY_PREFIX, DEFAULT_TOPIC, MqttBridge
from printer import CupsPrinter, PrinterError, make_printer


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
TEMPLATES_FILE = DATA_DIR / "templates.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
AUTH_FILE = DATA_DIR / "auth.json"
SECRET_KEY_FILE = DATA_DIR / "secret_key"
# Environment variables that seed the printer settings; the web UI can change them afterwards.
PRINTER_ENV = {"backend": "PRINTER_CONNECTION", "device": "PRINTER_DEVICE", "cups_printer": "CUPS_PRINTER"}
# Largest print position correction per label, in mm.
MAX_OFFSET_MM = 10.0
MIN_PASSWORD_LENGTH = 8
# PBKDF2 works on every Python build (scrypt needs OpenSSL support); iterations per OWASP.
PASSWORD_HASH = "pbkdf2:sha256:600000"
# Pages and files the sign-in page itself needs.
PUBLIC_ENDPOINTS = {"login", "static", "web_font", "favicon"}

app = Flask(__name__)
app.config.update(SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_HTTPONLY=True, PERMANENT_SESSION_LIFETIME=30 * 24 * 3600)
log = logging.getLogger("dymo_label_studio")
bridge: MqttBridge | None = None
network_printer = print_server.PrintServer()
_print_lock = threading.Lock()


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _settings() -> dict:
    settings = {
        "backend": "direct",
        "device": "/dev/usb/lp0",
        "cups_printer": "DYMO",
        "fallback_cups": False,
        "favorite_labels": list(DEFAULT_FAVORITES),
        "default_label": DEFAULT_LABEL_SIZE,
        # Per label: {"x": mm, "y": mm} to correct where the printer puts the print.
        "label_offsets": {},
        "print_server": False,
    }
    settings.update({key: os.environ[name] for key, name in PRINTER_ENV.items() if os.environ.get(name)})
    settings.update(_load(SETTINGS_FILE, {}))
    return settings


# Password protection. Off until a password is set in the web UI; the hash lives in auth.json
# with a version number, so changing or removing the password signs out every other session.

def _auth() -> dict:
    return _load(AUTH_FILE, {})


def auth_enabled() -> bool:
    return bool(_auth().get("password_hash"))


def _signed_in() -> bool:
    auth = _auth()
    if not auth.get("password_hash") or session.get("auth_version") == auth.get("version"):
        return True
    # Scripts and tools can send the password with HTTP Basic auth instead (any user name).
    credentials = request.authorization
    return bool(credentials and credentials.password and check_password_hash(auth["password_hash"], credentials.password))


def _sign_in(version: int) -> None:
    session.clear()
    session.permanent = True
    session["auth_version"] = version


def _secret_key() -> str:
    """A random key for signing session cookies, kept across restarts."""
    if not SECRET_KEY_FILE.exists():
        SECRET_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_KEY_FILE.write_text(secrets.token_hex(32))
        SECRET_KEY_FILE.chmod(0o600)
    return SECRET_KEY_FILE.read_text().strip()


@app.before_request
def require_sign_in():
    if request.endpoint in PUBLIC_ENDPOINTS or _signed_in():
        return None
    if request.path.startswith("/api/"):
        return jsonify(ok=False, message="Sign in first"), 401
    # Relative, so it also works behind a reverse proxy that serves the app under a sub-path.
    return redirect("login")


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        auth = _auth()
        if not auth.get("password_hash") or check_password_hash(auth["password_hash"], request.form.get("password", "")):
            _sign_in(auth.get("version", 0))
            return redirect("./")
        # Slow down guessing.
        time.sleep(1)
        error = "That password is not correct."
    elif not auth_enabled():
        return redirect("./")
    return render_template("login.html", error=error), 401 if error else 200


@app.post("/logout")
def logout():
    session.clear()
    return redirect("login")


@app.get("/api/auth")
def auth_status():
    return jsonify(enabled=auth_enabled())


@app.post("/api/auth")
def set_password():
    """Turn on password protection, or change the password (the current one is required)."""
    body = request.get_json(silent=True) or {}
    auth = _auth()
    if auth.get("password_hash") and not check_password_hash(auth["password_hash"], str(body.get("current_password") or "")):
        time.sleep(1)
        return jsonify(ok=False, message="The current password is not correct"), 403
    password = str(body.get("password") or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        return jsonify(ok=False, message=f"Use at least {MIN_PASSWORD_LENGTH} characters"), 400
    version = auth.get("version", 0) + 1
    _save(AUTH_FILE, {"password_hash": generate_password_hash(password, method=PASSWORD_HASH), "version": version})
    AUTH_FILE.chmod(0o600)
    # Keep this browser signed in; every other session has to sign in again.
    _sign_in(version)
    return jsonify(ok=True)


@app.delete("/api/auth")
def remove_password():
    body = request.get_json(silent=True) or {}
    auth = _auth()
    if auth.get("password_hash") and not check_password_hash(auth["password_hash"], str(body.get("current_password") or "")):
        time.sleep(1)
        return jsonify(ok=False, message="The current password is not correct"), 403
    AUTH_FILE.unlink(missing_ok=True)
    return jsonify(ok=True)


def _update_network_printer(settings: dict) -> None:
    """Start or stop sharing the printer to match the settings.

    Sharing is off when printing goes through CUPS: then the app is a CUPS client, not a server.
    """
    wanted = bool(settings.get("print_server")) and settings.get("backend") != "cups"
    if wanted and not network_printer.running:
        # Starting takes a few seconds; the web UI shows progress through the status.
        threading.Thread(
            target=network_printer.start,
            args=(settings.get("device") or "/dev/usb/lp0", settings["favorite_labels"], settings["default_label"]),
            daemon=True,
        ).start()
    elif not wanted and (network_printer.running or network_printer.status.get("enabled")):
        network_printer.stop()
    elif wanted and not network_printer.status.get("starting"):
        # Network clients only see the favourite label sizes.
        print_server.apply_label_sizes(settings["favorite_labels"], settings["default_label"])


def _templates() -> list[dict]:
    return _load(TEMPLATES_FILE, [])


def _templates_changed() -> None:
    if bridge:
        bridge.publish_templates()


def print_request(payload: dict) -> str:
    """Print a label request, optionally based on a saved template, and describe the result."""
    name = payload.get("template")
    if name:
        template = next((t for t in _templates() if t.get("name") == name), None)
        if template is None:
            raise ValueError(f"Unknown template: {name}")
        # Fields sent along with the template name override the saved ones.
        payload = {**template, **{key: value for key, value in payload.items() if key != "template"}}
    image = render_request(templating.resolve_templates(payload))
    settings = _settings()
    copies = int(payload.get("copies") or 1)
    label_size = payload.get("label_size") or DEFAULT_LABEL_SIZE
    rotate = LABEL_SIZES[label_size].feeds_along_width
    # Correct for where this printer puts this label type; the preview stays as designed.
    offset = settings["label_offsets"].get(label_size) or {}
    image = shift_image(image, offset.get("x", 0), offset.get("y", 0))
    with _print_lock:
        try:
            make_printer(settings).print_image(image, copies, rotate)
        except PrinterError:
            if settings.get("backend", "direct") != "direct" or not settings.get("fallback_cups", False):
                raise
            CupsPrinter(settings.get("cups_printer", "DYMO")).print_image(image, copies, rotate)
    return f"Printed {name}" if name else "Label sent to printer"


@app.get("/")
def index():
    return render_template(
        "index.html",
        sizes=[{"id": size.id, "name": size.name} for size in sorted(LABEL_SIZES.values(), key=lambda size: size.id)],
        fonts=available_fonts(),
        date_formats=DATE_FORMATS,
        languages=HYPHENATION_LANGUAGES,
        default_language=DEFAULT_LANGUAGE,
        margin=MARGIN_MM,
        gap=GAP_MM,
        auth_enabled=auth_enabled(),
    )


@app.get("/api/status")
def status():
    return jsonify(
        mqtt=bridge is not None,
        mqtt_connected=bool(bridge and bridge.connected),
        mqtt_discovery=bool(bridge and bridge.discovery_prefix),
        home_assistant=templating.home_assistant_configured(),
        print_server=network_printer.status,
    )


@app.get("/api/icons")
def icons():
    return jsonify(all=mdi_icons(), home=home_icons())


@app.get("/mdi-font")
def mdi_font():
    """The icon webfont, so the picker shows the same glyphs that get printed."""
    for name, mimetype in (("materialdesignicons-webfont.woff2", "font/woff2"), ("materialdesignicons-webfont.ttf", "font/ttf")):
        path = find_font_file(name)
        if path:
            return send_file(path, mimetype=mimetype, max_age=86400)
    abort(404)


# Web fonts for the interface: the brand's display face and Roboto for body text.
WEB_FONTS = {
    "bricolage": ("BricolageGrotesque.ttf", "font/ttf"),
    "roboto-400": ("Roboto-Regular.ttf", "font/ttf"),
    "roboto-500": ("Roboto-Medium.ttf", "font/ttf"),
    "roboto-700": ("Roboto-Bold.ttf", "font/ttf"),
}


@app.get("/webfont/<name>")
def web_font(name: str):
    file_name, mimetype = WEB_FONTS.get(name, (None, None))
    path = find_font_file(file_name) if file_name else None
    if not path:
        abort(404)
    return send_file(path, mimetype=mimetype, max_age=86400)


@app.get("/favicon.svg")
def favicon():
    return send_from_directory(app.static_folder, "icon.svg", mimetype="image/svg+xml", max_age=86400)


@app.post("/api/preview")
def preview():
    try:
        image = render_request(templating.resolve_templates(request.get_json(silent=True) or {}))
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    buffer = BytesIO()
    image.save(buffer, format="PNG", dpi=(300, 300))
    buffer.seek(0)
    return send_file(buffer, mimetype="image/png")


@app.route("/api/templates", methods=["GET", "POST"])
def templates():
    saved = _templates()
    if request.method == "POST":
        item = request.get_json(silent=True) or {}
        if not str(item.get("name") or "").strip():
            return jsonify(ok=False, message="A template needs a name"), 400
        saved = [old for old in saved if old.get("name") != item.get("name")]
        saved.append(item)
        _save(TEMPLATES_FILE, saved)
        _templates_changed()
    return jsonify(saved)


@app.delete("/api/templates/<name>")
def delete_template(name: str):
    saved = [item for item in _templates() if item.get("name") != name]
    _save(TEMPLATES_FILE, saved)
    _templates_changed()
    return jsonify(saved)


@app.post("/api/templates/<name>/print")
def print_template(name: str):
    return _print_response({**(request.get_json(silent=True) or {}), "template": name})


@app.post("/api/print")
def print_label():
    return _print_response(request.get_json(silent=True) or {})


def _print_response(payload: dict):
    try:
        return jsonify(ok=True, message=print_request(payload))
    except (PrinterError, ValueError, TypeError) as exc:
        return jsonify(ok=False, message=str(exc)), 500


@app.get("/api/settings")
def settings():
    return jsonify(_settings())


@app.post("/api/settings")
def save_settings():
    value = request.get_json(silent=True) or {}
    favorites = [label for label in value.get("favorite_labels") or [] if label in LABEL_SIZES]
    value["favorite_labels"] = favorites or [DEFAULT_LABEL_SIZE]
    if value.get("default_label") not in value["favorite_labels"]:
        value["default_label"] = value["favorite_labels"][0]
    value["label_offsets"] = _clean_offsets(value.get("label_offsets"))
    value["print_server"] = bool(value.get("print_server")) and value.get("backend") != "cups"
    _save(SETTINGS_FILE, value)
    try:
        _update_network_printer(_settings())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        log.error("Could not update the network printer: %s", exc)
        return jsonify(ok=True, message="Saved, but the network printer could not be updated; see the container log")
    return jsonify(ok=True)


def _clean_offsets(offsets) -> dict:
    """Keep non-zero corrections for known labels, limited to a sensible range."""
    cleaned = {}
    for label, offset in (offsets or {}).items() if isinstance(offsets, dict) else ():
        if label not in LABEL_SIZES or not isinstance(offset, dict):
            continue
        values = {}
        for axis in ("x", "y"):
            try:
                amount = round(max(-MAX_OFFSET_MM, min(MAX_OFFSET_MM, float(offset.get(axis) or 0))), 1)
            except (TypeError, ValueError):
                amount = 0
            if amount:
                values[axis] = amount
        if values:
            cleaned[label] = values
    return cleaned


def _start_mqtt() -> MqttBridge | None:
    host = os.environ.get("MQTT_HOST", "").strip()
    if not host:
        log.info("No MQTT_HOST set; MQTT printing is off")
        return None
    discovery = os.environ.get("MQTT_DISCOVERY_PREFIX", DEFAULT_DISCOVERY_PREFIX).strip()
    mqtt_bridge = MqttBridge(
        host,
        int(os.environ.get("MQTT_PORT") or 1883),
        os.environ.get("MQTT_USERNAME") or None,
        os.environ.get("MQTT_PASSWORD") or None,
        _templates,
        print_request,
        topic=os.environ.get("MQTT_TOPIC") or DEFAULT_TOPIC,
        # "off" (or an empty value) publishes no discovery buttons.
        discovery_prefix=None if discovery.lower() in ("", "off", "false", "none") else discovery,
    )
    mqtt_bridge.start()
    return mqtt_bridge


def main() -> None:
    global bridge
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if _env_flag("RESET_PASSWORD") and AUTH_FILE.exists():
        AUTH_FILE.unlink()
        log.warning("RESET_PASSWORD is set: password protection is off. Remove RESET_PASSWORD and set a new password in the web UI.")
    app.secret_key = _secret_key()
    bridge = _start_mqtt()
    _update_network_printer(_settings())
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)


if __name__ == "__main__":
    main()
