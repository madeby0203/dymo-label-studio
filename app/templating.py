"""Templates in labels, such as {{ now().strftime('%d-%m') }}, filled in when printing.

The app renders them itself in a sandboxed Jinja environment with ``now()``, ``today()`` and
``timedelta``. With HA_URL (for example http://homeassistant.local:8123) and HA_TOKEN (a
long-lived access token from your Home Assistant profile) set, Home Assistant renders them
instead, so everything Home Assistant templates can do works, including ``states()``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jinja2 import TemplateError
from jinja2.sandbox import ImmutableSandboxedEnvironment


# Label fields that may contain templates.
TEMPLATE_FIELDS = ("text", "qr")


def _home_assistant() -> tuple[str, str] | None:
    url = os.environ.get("HA_URL", "").strip().rstrip("/")
    token = os.environ.get("HA_TOKEN", "").strip()
    return (url, token) if url and token else None


def home_assistant_configured() -> bool:
    return _home_assistant() is not None


def has_template(value) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def _needs_home_assistant(*args, **kwargs):
    raise ValueError("Entity states need Home Assistant: set HA_URL and HA_TOKEN in the container's settings")


# The sandbox keeps templates from reaching anything but these values, even when anyone on the
# network can enter them.
_environment = ImmutableSandboxedEnvironment()
_environment.globals.update(
    now=lambda: datetime.now().astimezone(),
    utcnow=lambda: datetime.now(timezone.utc),
    today=date.today,
    timedelta=timedelta,
    states=_needs_home_assistant,
    state_attr=_needs_home_assistant,
    is_state=_needs_home_assistant,
)


def _render_locally(template: str) -> str:
    try:
        return _environment.from_string(template).render()
    except TemplateError as exc:
        raise ValueError(f"Template error: {exc}") from exc


def _render_in_home_assistant(template: str, url: str, token: str) -> str:
    request = Request(
        f"{url}/api/template",
        data=json.dumps({"template": template}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            return response.read().decode()
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace").strip()
        if exc.code == 401:
            detail = "Home Assistant rejected HA_TOKEN"
        raise ValueError(f"Template error: {detail[:300] or f'HTTP {exc.code}'}") from exc
    except URLError as exc:
        raise ValueError(f"Could not reach Home Assistant at {url}: {exc.reason}") from exc


def render_template(template: str) -> str:
    connection = _home_assistant()
    return _render_in_home_assistant(template, *connection) if connection else _render_locally(template)


def resolve_templates(payload: dict) -> dict:
    """Return a copy of a label request with its templates filled in."""
    resolved = dict(payload)
    for field in TEMPLATE_FIELDS:
        if has_template(resolved.get(field)):
            resolved[field] = render_template(resolved[field])
    return resolved
