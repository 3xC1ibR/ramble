"""Ephemeral audio previews for listening feedback, never request admission.

The recorder publishes a few opening prefixes into one replaceable slot. The
normal transcription worker decodes them only when no complete segment waits,
so there is no second model, recorder, transcript, or action path.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import config, latency, state, voice_control
from .addressing import ADDRESS, NAME

PREVIEW_SECONDS = (1.2, 2.4, 4.0)
MAX_AGE_S = 2.0


@dataclass(frozen=True)
class Preview:
    started_at: float
    audio: np.ndarray
    captured_at: float
    epoch: int


@dataclass
class Feedback:
    acknowledged: bool = False
    work_token: int | None = None


class EarlyWake:
    def __init__(self, cfg, cues):
        self.cfg, self.cues = cfg, cues
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self._epoch = 0
        self._pending = None
        self._current = None
        self._feedback: dict[float, Feedback] = {}

    def enabled(self, since=None):
        return (self.cfg.get('early_wake_enabled', True)
                and self.cfg.get('fast_enabled', True) and self.cues.enabled()
                and state.is_listening() and state.audio_allowed(since))

    def offer(self, started_at, audio):
        if not self.enabled(started_at):
            return
        with self._lock:
            feedback = self._feedback.get(started_at)
            if feedback and feedback.acknowledged and feedback.work_token is not None:
                return
            self._current = started_at
            self._pending = Preview(started_at, audio, time.monotonic(), self._epoch)
        self.wake.set()

    def take(self):
        with self._lock:
            preview, self._pending = self._pending, None
        return preview

    def _current_preview(self, preview):
        return (preview.epoch == self._epoch and self._current == preview.started_at
                and time.monotonic() - preview.captured_at <= MAX_AGE_S)

    def valid(self, preview):
        with self._lock:
            return self._current_preview(preview) and self.enabled(preview.started_at)

    def recognize(self, preview, result):
        text = (result.get('text') or '').strip()
        address = NAME.match(text) or ADDRESS.match(text)
        segments = result.get('segments') or []
        # Only an opening, confident, unhinted address can produce early audio.
        # Quoted/mid-sentence mentions still go through normal full recognition.
        if (not address or not segments or voice_control.command(text) is not None
                or any(s.get('no_speech_prob', 1) > .6 or s.get('avg_logprob', -2) < -.8
                       or s.get('compression_ratio', 0) > 2.4 for s in segments)):
            return
        has_request = bool(re.search(r'\w', text[address.end():]))
        with self._lock:
            if not self._current_preview(preview) or not self.enabled(preview.started_at):
                return
            feedback = self._feedback.setdefault(preview.started_at, Feedback())
            if not feedback.acknowledged:
                feedback.acknowledged = bool(self.cues.emit('accepted'))
                if feedback.acknowledged:
                    latency.event('early_wake_acknowledged', audio_start_wall_s=preview.started_at,
                                  preview_duration_s=len(preview.audio) / config.SAMPLE_RATE,
                                  preview_age_s=time.monotonic() - preview.captured_at)
            if has_request and feedback.work_token is None:
                feedback.work_token = self.cues.start_work(preview.started_at)
            # In-memory feedback may outlive capture while a full decode waits.
            while len(self._feedback) > 32:
                old = self._feedback.pop(next(iter(self._feedback)))
                if old.work_token is not None:
                    self.cues.end_work(old.work_token)

    def finish(self, started_at):
        """The full file takes priority; invalidate queued/inflight previews."""
        with self._lock:
            if self._current == started_at:
                self._current = self._pending = None

    def complete(self, started_at):
        self.finish(started_at)
        with self._lock:
            return self._feedback.pop(started_at, Feedback())

    def discard(self, started_at):
        feedback = self.complete(started_at)
        if feedback.work_token is not None:
            self.cues.end_work(feedback.work_token)

    def reset(self):
        with self._lock:
            self._epoch += 1
            self._current = self._pending = None
            feedback, self._feedback = self._feedback, {}
            for receipt in feedback.values():
                if receipt.work_token is not None:
                    self.cues.end_work(receipt.work_token)
