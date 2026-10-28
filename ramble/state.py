"""On/off switch shared by the menu bar app and the CLI (a small JSON file)."""

from __future__ import annotations

import json
import subprocess
import time

from . import config, voice_control


def read(home=None) -> dict:
    try:
        return json.loads((home / 'state.json' if home is not None else config.STATE_PATH).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"enabled": False, "paused_until": None}


def write(enabled: bool, paused_until: float | None = None, *, home=None) -> dict:
    if home is None:
        config.ensure_dirs()
    else:
        home.mkdir(parents=True, exist_ok=True)
    path = home / 'state.json' if home is not None else config.STATE_PATH
    s = {"enabled": enabled, "paused_until": paused_until, "changed_at": time.time()}
    # Keep the pause boundary after resume/expiry so stale replies stay cancelled.
    previous = read(home)
    last_pause = previous.get("last_paused_at", previous.get("changed_at", 0) if previous.get("paused_until") else 0)
    if paused_until and paused_until > s["changed_at"]:
        last_pause = s["changed_at"]
    if last_pause:
        s["last_paused_at"] = last_pause
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    tmp.replace(path)
    return s


def is_paused(s: dict | None = None) -> bool:
    s = read() if s is None else s
    return bool(s.get("enabled") and (s.get("paused_until") or 0) > time.time())


def activity_allowed(since: float | None = None) -> bool:
    """A timed pause silences output, including work queued before that pause."""
    s = read()
    boundary = s.get("last_paused_at", s.get("changed_at", 0) if s.get("paused_until") else 0)
    return not is_paused(s) and (since is None or boundary <= since)


class PausedError(RuntimeError):
    pass


def check_activity(since: float | None = None) -> None:
    if not activity_allowed(since):
        raise PausedError("Ramble is paused or this reply was cancelled by a pause")


def audio_allowed(since: float | None = None) -> bool:
    """Audio has its own switch; capture, routing and notifications keep running."""
    return activity_allowed(since) and voice_control.allowed(since)


def check_audio(since: float | None = None) -> None:
    if not audio_allowed(since):
        raise PausedError("Ramble audio is muted, paused, or this reply was cancelled")


def run_audio(args: list[str], *, timeout: float, cancelled=None) -> None:
    """Stop an active player promptly when another process selects Pause."""
    since = time.time()
    check_audio(since)
    if cancelled and cancelled():
        raise PausedError('Speech cancelled')
    deadline = time.monotonic() + timeout
    with subprocess.Popen(args) as player:
        try:
            while True:
                check_audio(since)
                if cancelled and cancelled():
                    raise PausedError('Speech cancelled')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    code = player.wait(timeout=min(.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                if code:
                    raise subprocess.CalledProcessError(code, args)
                return
        finally:
            if player.poll() is None:
                player.terminate()
                try:
                    player.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    player.kill()
                    player.wait()


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
