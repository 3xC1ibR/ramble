"""Read-only mini-window activity and ephemeral output metering.

No UI, device opens, routing, or changes to the listening switch live here.
The output helper publishes only a scalar level, never audio or transcript text.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, state

log = logging.getLogger("ramble.activity")
METER_TTL = .35


def display_level(rms: float) -> float:
    """A bounded dB scale: silence stays flat, ordinary quiet speech is visible."""
    if not math.isfinite(rms) or rms <= .0001:
        return 0.0
    return min(1.0, max(0.0, (20 * math.log10(rms) + 60) / 48))


class OutputMeter:
    """Cross-process telemetry, written off the audio callback at most 20 Hz.

    Atomic replacement tolerates readers racing a write. Expiry tolerates a
    helper killed before it can publish its final zero. Never fsync telemetry.
    """
    def __init__(self, home: Path | None = None):
        self.path = (home or config.HOME) / "output-meter.json"
        self.temp = self.path.with_name(f".output-meter-{os.getpid()}.tmp")
        self.last_write = float("-inf")
        self.was_active = False
        self.warned = False

    def publish(self, sample: tuple[float, float] | None, *, now=None):
        now = time.monotonic() if now is None else now
        active = sample is not None
        if not active and not self.was_active:
            return
        if active and now - self.last_write < .05:
            return
        at, level = sample if active else (now, 0.0)
        try:
            self.temp.write_text(json.dumps({"at": at, "level": level, "active": active}))
            self.temp.replace(self.path)
            self.last_write, self.was_active = now, active
        except OSError:
            if not self.warned:
                log.warning("Mini window output meter unavailable", exc_info=True)
                self.warned = True

    def read(self, *, now=None) -> tuple[bool, float]:
        now = time.monotonic() if now is None else now
        try:
            data = json.loads(self.path.read_text())
            age = now - float(data["at"])
            level = float(data["level"])
            # The current callback's DAC estimate may still be a few ms ahead.
            if data["active"] is True and -.15 <= age < METER_TTL and math.isfinite(level):
                return True, max(0.0, min(1.0, level))
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return False, 0.0


@dataclass(frozen=True)
class Activity:
    mode: str = "off"
    label: str = "Mic off"
    detail: str = "Ramble · microphone off"
    awake: bool = False

    @property
    def compact(self):
        return self.mode in {"idle", "off", "paused"}


def resolve_activity(*, enabled=False, paused=False, capture=False, error=None,
                     speaking=False, hearing=False, awake=False, thinking=False,
                     working=False, processing=False) -> Activity:
    """Explicit precedence; microphone off does not cancel independent work."""
    if paused:
        return Activity("paused", "Paused", "Ramble · listening paused")
    if speaking:
        return Activity("speaking", "Speaking", "Ramble is speaking")
    if enabled and error:
        return Activity("error", "Mic error", f"Ramble · microphone error: {error}")
    if capture and hearing:
        return Activity("listening", "Listening", "Ramble heard its name and is listening" if awake
                        else "Ramble is capturing speech", awake)
    if awake and capture:
        return Activity("awake", "I'm here", "Ramble heard its name and is ready for your request", True)
    if thinking:
        return Activity("thinking", "Thinking", "Ramble is preparing a response")
    if working:
        return Activity("working", "Working", "Ramble's foreground session is working")
    if processing:
        return Activity("processing", "Processing", "Ramble is transcribing or processing a thought")
    if enabled and not capture:
        return Activity("starting", "Starting", "Ramble is opening the microphone")
    if capture:
        return Activity("idle", "Ready", "Ramble is listening for speech")
    return Activity()


class ActivityMonitor:
    """Poll durable work on a worker, never in AppKit's drawing/event loop."""
    def __init__(self, daemon):
        self.daemon = daemon
        self.current = Activity()
        self.output_level = 0.0
        self.meter = OutputMeter()
        self._thread = None
        self._work_checked = float("-inf")
        self._working = False

    def start(self):
        self._thread = threading.Thread(target=self.run, name="mini-activity", daemon=True)
        self._thread.start()

    def run(self):
        while not self.daemon.stop.is_set():
            try:
                self.update()
            except Exception:
                log.exception("Mini window activity unavailable")
                self.current = Activity("error", "Unavailable", "Ramble activity is unavailable; check the log")
                self.daemon.stop.wait(1)
            self.daemon.stop.wait(.05)

    def update(self):
        daemon = self.daemon
        selected = state.read()
        cues = daemon.fast.cues
        now = time.monotonic()
        speaking, self.output_level = self.meter.read(now=now)
        if now - self._work_checked >= .4:
            self._working = bool(daemon.sessions and daemon.sessions.thinking_work(include_quiet=True))
            self._work_checked = now
        self.current = resolve_activity(
            enabled=state.is_listening(selected), paused=state.is_paused(selected),
            capture=daemon.recorder.active, error=daemon.recorder.error,
            speaking=speaking, hearing=daemon.recorder.speech_active,
            awake=(now - cues.last_wake < 1.1 or time.time() < daemon.fast.name_only_until),
            thinking=daemon.fast.busy or not daemon.fast.q.empty() or cues.visual_work_active(),
            working=self._working, processing=bool(daemon.transcriber.backlog or daemon.router.current),
        )
        if (self.current.mode not in {"paused", "speaking", "listening"}
                and now - cues.last_error < 2.5):
            self.current = Activity("error", "Try again", "Ramble could not complete the response; check the log")
