"""Mic capture -> voice activity detection -> speech segments saved as FLAC.

Silence is never stored. Each detected utterance (plus a little padding) becomes
one FLAC file under RAMBLE_HOME/audio/YYYY-MM-DD/ and one `segments` row with
status 'pending' for the transcriber.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque

import numpy as np
import soundfile as sf

from . import config, db, latency, state
from .early_wake import PREVIEW_SECONDS
from .speech import overlaps_playback
from .vad import CHUNK, SileroVAD

log = logging.getLogger("ramble.recorder")
CHUNK_S = CHUNK / config.SAMPLE_RATE
SHORT_SPEECH_S = 0.3
SHORT_SPEECH_CONFIDENCE = 0.9
CALLBACK_TIMEOUT_S = 5.0
PROCESSING_TIMEOUT_S = 10.0
ZERO_INPUT_TIMEOUT_S = 30.0
RECOVERY_RESET_S = 30.0
MAX_RETRY_DELAY_S = 30.0


class Segmenter:
    """Feeds 512-sample chunks through VAD and emits (start_ts, audio) speech segments."""

    def __init__(self, cfg: dict, on_segment, *, on_preview=None, on_discard=None) -> None:
        self.cfg = cfg
        self.on_segment = on_segment
        self.on_preview = on_preview
        self.on_discard = on_discard
        self.vad = SileroVAD()
        self.pad_chunks = max(1, int(cfg["pad_s"] / CHUNK_S))
        self.silence_chunks = int(cfg["segment_silence_s"] / CHUNK_S)
        self.max_chunks = int(cfg["segment_max_s"] / CHUNK_S)
        self.min_speech_chunks = int(cfg["min_speech_s"] / CHUNK_S)
        # A single wake word can be shorter than the normal noise filter.
        # Admit short clips only when Silero is highly confident they are speech.
        self.short_speech_chunks = max(1, int(SHORT_SPEECH_S / CHUNK_S))
        self.on_thr = cfg["vad_threshold"]
        self.off_thr = max(0.05, self.on_thr - 0.15)
        self.reset()

    def reset(self) -> None:
        self.vad.reset()
        self.pre = deque(maxlen=self.pad_chunks)
        self.buf: list[np.ndarray] = []
        self.start_ts = 0.0
        self.speech_chunks = 0
        self.silent_run = 0
        self.peak_speech_probability = 0.0
        self.preview_index = 0

    def feed(self, chunk: np.ndarray, ts: float) -> float:
        """ts = wall-clock time of the chunk's first sample. Returns the speech probability."""
        p = self.vad(chunk)
        self._feed(chunk, ts, p)
        return p

    def _feed(self, chunk: np.ndarray, ts: float, p: float) -> None:
        if not self.buf:
            if p >= self.on_thr:
                self.start_ts = ts - len(self.pre) * CHUNK_S
                self.buf = list(self.pre) + [chunk]
                self.speech_chunks, self.silent_run = 1, 0
                self.peak_speech_probability = p
            else:
                self.pre.append(chunk)
            return
        self.buf.append(chunk)
        if p >= self.off_thr:
            self.speech_chunks += 1
            self.silent_run = 0
            self.peak_speech_probability = max(self.peak_speech_probability, p)
        else:
            self.silent_run += 1
        if self.silent_run >= self.silence_chunks:
            # Keep pad_s of trailing silence, drop the rest.
            drop = max(0, self.silent_run - self.pad_chunks)
            self._emit(self.buf[: len(self.buf) - drop] if drop else self.buf)
        elif len(self.buf) >= self.max_chunks:
            self._emit(self.buf)
        elif (self.on_preview and self.silent_run == 0
              and self.preview_index < len(PREVIEW_SECONDS)
              and len(self.buf) * CHUNK_S >= PREVIEW_SECONDS[self.preview_index]):
            self.preview_index += 1
            self.on_preview(self.start_ts, np.concatenate(self.buf))

    def flush(self) -> None:
        if self.buf:
            self._emit(self.buf)
        self.pre.clear()

    def _emit(self, chunks: list[np.ndarray]) -> None:
        if (self.speech_chunks >= self.min_speech_chunks or
                (self.speech_chunks >= self.short_speech_chunks and
                 self.peak_speech_probability >= SHORT_SPEECH_CONFIDENCE)):
            self.on_segment(self.start_ts, np.concatenate(chunks))
        elif self.on_discard:
            self.on_discard(self.start_ts)
        self.buf, self.speech_chunks, self.silent_run = [], 0, 0
        self.peak_speech_probability = 0.0
        self.preview_index = 0
        self.pre.clear()


def save_segment(conn, start_ts: float, audio: np.ndarray, source: str = "mic") -> str | None:
    if source == "mic" and not state.activity_allowed(start_ts):
        return None
    dur = len(audio) / config.SAMPLE_RATE
    if source == "mic" and overlaps_playback(conn, start_ts, start_ts + dur):
        log.info("feedback guard: dropped mic segment %.3f..%.3f", start_ts, start_ts + dur)
        return None
    seg_id = db.new_id()
    day = time.strftime("%Y-%m-%d", time.localtime(start_ts))
    folder = config.AUDIO_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.strftime('%H%M%S', time.localtime(start_ts))}-{seg_id[-6:]}.flac"
    sf.write(path, audio, config.SAMPLE_RATE, format="FLAC", subtype="PCM_16")
    conn.execute(
        "INSERT INTO segments (id, started_at, ended_at, duration, audio_path, source) VALUES (?,?,?,?,?,?)",
        (seg_id, start_ts, start_ts + dur, dur, str(path), source),
    )
    log.info("segment %s %.1fs -> %s", seg_id, dur, path.name)
    latency.event("segment_saved", segment_id=seg_id, source=source,
                  audio_start_wall_s=start_ts, audio_end_wall_s=start_ts + dur)
    return seg_id


def ingest_file(path: str, cfg: dict, wake: threading.Event | None = None) -> list[str]:
    """Run an audio file through the same VAD path as the mic (used for testing/imports)."""
    import subprocess

    raw = subprocess.run(
        ["ffmpeg", "-v", "quiet", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(config.SAMPLE_RATE), "-"],
        check=True,
        capture_output=True,
    ).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    conn = db.connect()
    ids: list[str] = []
    t0 = time.time()
    seg = Segmenter(cfg, lambda ts, a: ids.append(save_segment(conn, ts, a, source=f"file:{path}")))
    tail = np.zeros(int(cfg["segment_silence_s"] / CHUNK_S + 2) * CHUNK, dtype=np.float32)
    audio = np.concatenate([audio, tail])
    for i in range(0, len(audio) - CHUNK + 1, CHUNK):
        seg.feed(audio[i : i + CHUNK], t0 + i / config.SAMPLE_RATE)
    seg.flush()
    if wake:
        wake.set()
    return ids


def ingest_companion(path: str) -> str:
    """Queue one deliberately recorded request, independent of the Mac mic."""
    import subprocess

    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-t", "61", "-f", "f32le",
         "-ac", "1", "-ar", str(config.SAMPLE_RATE), "-"],
        check=True, capture_output=True, timeout=30,
    ).stdout
    audio = np.frombuffer(raw, dtype=np.float32)
    duration = len(audio) / config.SAMPLE_RATE
    if duration < .25 or duration > 60:
        raise ValueError("Recording must be between 0.25 and 60 seconds.")
    conn = db.connect()
    try:
        return save_segment(conn, time.time() - duration, audio, source="companion")
    finally:
        conn.close()


class Recorder:
    """Owns the mic stream. start()/stop() are called when the on/off state changes."""

    def __init__(self, cfg: dict, wake: threading.Event) -> None:
        self.cfg = cfg
        self.wake = wake
        self.stream = None
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.running = False
        self.last_speech_at: float | None = None
        self.last_voice_at = 0.0
        self.last_voice_mono = 0.0
        self.early = None
        self.level = 0.0
        self.error: str | None = None
        self.on_failure = None
        self._started_at = 0.0
        self._last_callback_at = 0.0
        self._last_processed_at = 0.0
        self._last_nonzero_at = 0.0
        self._retry_at = 0.0
        self._retry_delay = 1.0

    @property
    def active(self) -> bool:
        return self.running and self._health_error() is None

    def _health_error(self) -> str | None:
        now = time.monotonic()
        try:
            if self.stream is None or not self.stream.active:
                return "Microphone stream stopped."
        except Exception as exc:
            return f"Microphone stream unavailable: {exc}"
        if self.worker is None or not self.worker.is_alive():
            return "Microphone processing worker stopped."
        if now - max(self._started_at, self._last_callback_at) >= CALLBACK_TIMEOUT_S:
            return "Microphone stopped delivering audio."
        if now - max(self._started_at, self._last_processed_at) >= PROCESSING_TIMEOUT_S:
            return "Microphone processing stopped making progress."
        # Ordinary quiet rooms still contain nonzero microphone noise. Only
        # sustained, exact digital zero is treated as a broken/muted input.
        if now - max(self._started_at, self._last_nonzero_at) >= ZERO_INPUT_TIMEOUT_S:
            return "Microphone is delivering only zero samples."
        return None

    @property
    def speech_active(self) -> bool:
        return self.active and time.time() - self.last_voice_at < self.cfg["segment_silence_s"] + .2

    def start(self) -> None:
        import sounddevice as sd

        now = time.monotonic()
        if self.running:
            reason = self._health_error()
            if reason is None:
                if now - self._started_at >= RECOVERY_RESET_S:
                    self._retry_at, self._retry_delay = 0.0, 1.0
                return
            self.error = reason
            log.warning("microphone recovery: %s", reason)
            if self.on_failure:
                self.on_failure("Dictation cancelled because microphone capture failed.")
            self.stop(reset_retry=False)
        if now < self._retry_at:
            return
        self._retry_at = now + self._retry_delay
        self._retry_delay = min(MAX_RETRY_DELAY_S, self._retry_delay * 2)
        if self.worker is not None and self.worker.is_alive():
            self.error = "Previous microphone processing worker has not stopped."
            return
        self.running = True
        self.error = None
        self.q = queue.Queue()
        audio_queue = self.q
        self._started_at = now
        self._last_callback_at = self._last_processed_at = self._last_nonzero_at = now
        try:
            self.worker = threading.Thread(target=self._process, args=(audio_queue,), name="segmenter", daemon=True)
            self.worker.start()
            self.stream = sd.InputStream(
                samplerate=config.SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=CHUNK,
                device=self.cfg["input_device"],
                callback=lambda *args: self._callback(*args, audio_queue=audio_queue),
            )
            self.stream.start()
            # A permission prompt/device open can block; give callbacks their
            # full startup grace period after it returns.
            self._started_at = time.monotonic()
            log.info("mic on (%s)", self.stream.device)
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            log.exception("could not open mic")
            self.stop(reset_retry=False)

    def stop(self, *, reset_retry=True) -> None:
        if self.early:
            self.early.reset()
        was_running = self.running
        self.running = False
        self.level = self.last_voice_at = self.last_voice_mono = 0.0
        if reset_retry:
            self._retry_at, self._retry_delay = 0.0, 1.0
            self.error = None
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.abort()
            except Exception:
                log.warning("could not abort microphone stream", exc_info=True)
            finally:
                try:
                    stream.close()
                except Exception:
                    log.warning("could not close microphone stream", exc_info=True)
        if was_running:
            self.q.put(None)
        if self.worker:
            if self.worker.is_alive():
                self.worker.join(timeout=1)
            if not self.worker.is_alive():
                self.worker = None
        if was_running:
            log.info("mic off")

    def _callback(self, indata, frames, time_info, status, *, audio_queue=None) -> None:
        if status:
            log.debug("stream status: %s", status)
        audio_queue = self.q if audio_queue is None else audio_queue
        data = indata[:, 0].copy()
        nonzero = bool(np.any(data))
        if not self.running or audio_queue is not self.q or not frames:
            return
        now = time.monotonic()
        self._last_callback_at = now
        if nonzero:
            self._last_nonzero_at = now
        audio_queue.put((time.time() - frames / config.SAMPLE_RATE, data))

    def set_dictation(self, session_id: str | None, completed=None) -> None:
        """Serialize mode changes with audio, so no segment straddles modes."""
        self.q.put(("dictation", session_id, completed))

    def _process(self, audio_queue=None) -> None:
        audio_queue = self.q if audio_queue is None else audio_queue
        conn = db.connect()
        source = "mic"

        def on_segment(ts, audio):
            if self.early and source == "mic":
                self.early.finish(ts)
            if save_segment(conn, ts, audio, source=source):
                self.last_speech_at = time.time()
                self.wake.set()
            elif self.early and source == "mic":
                self.early.discard(ts)

        seg = Segmenter(self.cfg, on_segment,
                        on_preview=self.early.offer if self.early else None,
                        on_discard=self.early.discard if self.early else None)
        pending = np.zeros(0, dtype=np.float32)
        pending_ts = 0.0
        # Periodic input levels in the log: tells "mic delivers silence" (permission/device)
        # apart from "too quiet/far for the VAD".
        win_start, rms_max, vad_max = time.time(), 0.0, 0.0
        guarded = False
        while True:
            item = audio_queue.get()
            if item is None:
                break
            self._last_processed_at = time.monotonic()
            if len(item) == 3:
                _, session_id, completed = item
                seg.flush()
                seg.reset()
                pending = np.zeros(0, dtype=np.float32)
                if self.early:
                    self.early.reset()
                source = f"dictation:{session_id}" if session_id else "mic"
                seg.on_preview = self.early.offer if self.early and not session_id else None
                seg.on_discard = self.early.discard if self.early and not session_id else None
                if completed:
                    completed()
                continue
            ts, data = item
            if overlaps_playback(conn, ts, ts + len(data) / config.SAMPLE_RATE):
                if not guarded:
                    log.info("feedback guard: dropping mic audio during Ramble playback")
                guarded = True
                if self.early:
                    self.early.reset()
                seg.reset()
                pending = np.zeros(0, dtype=np.float32)
                self.level = 0.0
                continue
            if guarded:
                log.info("feedback guard: mic audio resumed")
                guarded = False
            if not len(pending):
                pending_ts = ts
            pending = np.concatenate([pending, data])
            self.level = float(np.sqrt(np.mean(data**2))) if len(data) else 0.0
            rms_max = max(rms_max, self.level)
            while len(pending) >= CHUNK:
                probability = seg.feed(pending[:CHUNK], pending_ts)
                vad_max = max(vad_max, probability)
                if probability >= self.cfg["vad_threshold"]:
                    self.last_voice_at = time.time()
                    self.last_voice_mono = time.monotonic()
                pending = pending[CHUNK:]
                pending_ts += CHUNK_S
            if time.time() - win_start >= 60:
                log.info("levels (60s): rms_max=%.4f vad_max=%.2f", rms_max, vad_max)
                win_start, rms_max, vad_max = time.time(), 0.0, 0.0
        seg.flush()
        conn.close()
