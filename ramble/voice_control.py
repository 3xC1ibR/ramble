"""Persistent audio-only switch; microphone and task state are independent."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import time

from . import config
from .addressing import ADDRESS, NAME


def read(home: Path | None = None) -> dict:
    path = (home if home is not None else config.HOME) / "voice.json"
    try:
        saved = json.loads(path.read_text())
        if type(saved.get("enabled")) is not bool:
            raise ValueError("Invalid voice switch")
        if not isinstance(saved.get("changed_at", 0), (int, float)):
            raise ValueError("Invalid voice boundary")
        return saved
    except FileNotFoundError:
        return {"enabled": True, "changed_at": 0}
    except (OSError, ValueError, TypeError, AttributeError):
        # A damaged switch must not silently turn the speakers back on.
        return {"enabled": False, "changed_at": 0}


def write(enabled: bool, *, home: Path | None = None) -> dict:
    if type(enabled) is not bool:
        raise ValueError("Supply enabled as true or false")
    folder = home if home is not None else config.HOME
    folder.mkdir(parents=True, exist_ok=True)
    saved = {"enabled": enabled, "changed_at": time.time()}
    # Both edges invalidate old audio, including replies queued while muted.
    fd, temp = tempfile.mkstemp(prefix=".voice-", dir=folder)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(saved, handle)
        Path(temp).replace(folder / "voice.json")
    finally:
        Path(temp).unlink(missing_ok=True)
    return saved


def allowed(since: float | None = None) -> bool:
    saved = read()
    return saved["enabled"] and (since is None or saved.get("changed_at", 0) <= since)


COMMAND = re.compile(
    r"(?:please\s+)?(?P<verb>disable|mute|pause|turn off|stop|enable|unmute|resume|turn on)"
    r"\s+(?:(?:your|the|all)\s+)?(?:voice responses|voice replies|spoken responses|spoken replies|audio responses)"
    r"(?:,?\s+please)?", re.I,
)


def command(text: str) -> bool | None:
    """Only a complete, explicit command qualifies; quoted/discussed text does not."""
    text = text.strip(" \t\n,.!?")
    address = ADDRESS.match(text) or NAME.match(text)
    if address:
        text = text[address.end():].strip(" ,.!?")
    match = COMMAND.fullmatch(text)
    if not match:
        return None
    return match["verb"].lower() in {"enable", "unmute", "resume", "turn on"}
