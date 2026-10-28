"""Short nonverbal acknowledgements. Never mute the mic or block transcription."""

from __future__ import annotations

import fcntl
import logging
import queue
import threading
import time

from . import config, state
from .cue_player import shared_player, tone

log = logging.getLogger("ramble.cues")
SessionWork = tuple[tuple[str, str, int, int], float]
WorkToken = int | SessionWork


class AudioCues:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue(maxsize=4)
        self.last_accepted = float("-inf")
        self.last_wake = self.last_error = float("-inf")
        self.player = shared_player()
        self.speech_active = lambda: False
        self.session_work = lambda: None
        self._session_work = None
        self._next_session_work_at = float("inf")
        self._work_lock = threading.Lock()
        self._work_token = 0
        self._work_since: float | None = None
        self._work_job: str | None = None
        self._next_work_at = float("inf")

    def enabled(self) -> bool:
        return self.cfg.get("audio_cues_enabled", True) and self.cfg.get("fast_speak", True)

    def start_work(self, since: float) -> int:
        """Arm a soft pulse for this response, separate from bare-name receipt."""
        with self._work_lock:
            if self._work_since == since:
                return self._work_token  # Preserve the early preview's settled pulse.
            self._work_token += 1
            self._work_since = since
            self._work_job = None
            self._next_work_at = max(time.monotonic(), self.last_accepted) + 1
            return self._work_token

    def end_work(self, token: int | None = None, *, keep_pending=False) -> None:
        with self._work_lock:
            if token is None or token == self._work_token:
                if keep_pending and self._work_job is not None:
                    return
                self._work_since = None
                self._work_job = None
                self._next_work_at = float("inf")

    def response_token(self, token: int, starts: list[float]) -> int:
        """A coalesced continuation's pulse belongs to the same final reply."""
        with self._work_lock:
            return self._work_token if self._work_since in starts else token

    def wait_for_speech(self, token: int, job_id: str) -> None:
        with self._work_lock:
            if token == self._work_token and self._work_since is not None:
                self._work_job = job_id

    def pending_speech(self) -> str | None:
        with self._work_lock:
            return self._work_job

    def finish_speech_wait(self, job_id: str) -> None:
        with self._work_lock:
            if self._work_job == job_id:
                self._work_since = self._work_job = None
                self._next_work_at = float('inf')

    def work_active(self, token: WorkToken | None = None) -> bool:
        if isinstance(token, tuple):
            # Recheck the durable owner even during playback: Companion controls
            # must silence a pulse without waiting for the session poller.
            current = self.session_work()
            return (current == token and self.enabled()
                    and self.cfg.get("audio_working_enabled", True)
                    and state.audio_allowed(token[1]))
        with self._work_lock:
            since = self._work_since
            current = token is None or token == self._work_token
        return (since is not None and current and self.enabled()
                and self.cfg.get("audio_working_enabled", True) and state.audio_allowed(since))

    def visual_work_active(self) -> bool:
        """Visual feedback remains useful when voice responses/cues are muted."""
        with self._work_lock:
            since = self._work_since
        return since is not None and state.activity_allowed(since)

    def work_due(self) -> WorkToken | None:
        """No queue of repeating cues: compute just the next live pulse."""
        now = time.monotonic()
        with self._work_lock:
            token = self._work_token
            due = now >= self._next_work_at
        if self.speech_active():
            return None
        if not self.work_active(token):
            return self.session_work_due(now)
        if not due:
            return None
        with self._work_lock:
            if token != self._work_token or self._work_since is None:
                return None
            self._next_work_at = now + max(1.0, float(self.cfg.get("audio_working_interval_s", 2.5)))
        return token

    def session_work_due(self, now: float) -> SessionWork | None:
        if not self.enabled() or not self.cfg.get("audio_working_enabled", True) or not state.audio_allowed():
            return None
        current = self.session_work()
        if current != self._session_work:
            self._session_work = current
            self._next_session_work_at = max(now, self.last_accepted) + 1
        if (current is None or not self.work_active(current)
                or now < max(self._next_session_work_at, self.last_accepted + 1)):
            return None
        self._next_session_work_at = now + max(1.0, float(self.cfg.get("audio_working_interval_s", 2.5)))
        return current

    def emit(self, kind: str) -> bool:
        if state.activity_allowed():
            if kind == "accepted":
                self.last_wake = time.monotonic()
            elif kind == "error":
                self.last_error = time.monotonic()
        if kind == "error":
            self.end_work()
        if not state.audio_allowed() or not self.enabled():
            return False
        now = time.monotonic()
        if kind == "accepted" and now - self.last_accepted < 3:
            return False
        try:
            self.q.put_nowait((kind, now))
        except queue.Full:
            return False
        if kind == "accepted":
            self.last_accepted = now
            with self._work_lock:
                self._next_work_at = max(self._next_work_at, now + 1)
        return True

    def play(self, kind: str, queued_at: float, stop: threading.Event | None = None,
             *, work_token: WorkToken | None = None) -> None:
        # A delayed cue is misleading; never queue up dings behind spoken output.
        age = time.monotonic() - queued_at
        since = time.time() - age
        def cancelled():
            return ((stop is not None and stop.is_set()) or not state.audio_allowed(since)
                    or (kind == "working" and (work_token is None
                        or not self.work_active(work_token) or self.speech_active())))

        if not self.enabled() or cancelled() or age > 2:
            return
        config.HOME.mkdir(parents=True, exist_ok=True)
        with (config.HOME / "speech.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            volume = min(1.0, max(0.0, float(self.cfg.get("audio_cues_volume", .5))))
            # These are non-speech tones, not TTS: no speech_playbacks entry,
            # feedback guard, or artificial conversational follow-up window.
            if self.player.play(kind, volume, queued_at + 2, cancelled):
                (log.debug if kind == "working" else log.info)("%s cue played", kind)
            elif not cancelled():
                log.warning("%s cue not delivered before freshness deadline; output is recovering", kind)

    def run_forever(self, stop: threading.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    if (self.cfg.get("fast_speak", True) or self.cfg.get("speak_questions", True)) and state.audio_allowed():
                        self.player.maintain()  # Warm output before an address arrives.
                    else:
                        self.player.close()
                    try:
                        kind, queued_at = self.q.get(timeout=.1)
                    except queue.Empty:
                        token = self.work_due()
                        if token is not None:
                            self.play("working", time.monotonic(), stop, work_token=token)
                        continue
                    self.play(kind, queued_at, stop)
                except Exception:
                    log.exception("audio cue failed")
                    self.player.close()
                    stop.wait(1)
        finally:
            self.end_work()
            self.player.close()
