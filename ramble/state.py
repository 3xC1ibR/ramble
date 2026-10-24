"""On/off switch shared by the menu bar app and the CLI (a small JSON file)."""

from __future__ import annotations

import json
import time

from . import config


def read() -> dict:
    try:
        return json.loads(config.STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "paused_until": None}


def write(enabled: bool, paused_until: float | None = None) -> dict:
    config.ensure_dirs()
    s = {"enabled": enabled, "paused_until": paused_until, "changed_at": time.time()}
    tmp = config.STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(config.STATE_PATH)
    return s


def is_listening(s: dict | None = None) -> bool:
    s = s or read()
    if not s.get("enabled"):
        return False
    until = s.get("paused_until")
    return not until or time.time() >= until


def describe(s: dict | None = None) -> str:
    s = s or read()
    if not s.get("enabled"):
        return "off"
    until = s.get("paused_until")
    if until and time.time() < until:
        return "paused until " + time.strftime("%-I:%M %p", time.localtime(until))
    return "on"
