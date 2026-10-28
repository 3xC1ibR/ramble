"""The long-running process: recorder (follows the on/off state), transcriber, router."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time

from . import config, db, state
from .fastlane import FastLane
from .recorder import Recorder
from .router import Router
from .runtime import acquire_daemon_lock
from .capture_control import acknowledge
from .transcribe import Transcriber

log = logging.getLogger("ramble")


def setup_logging() -> None:
    config.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(config.LOG_DIR / "ramble.log", maxBytes=5_000_000, backupCount=3)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s [pid=%(process)d thread=%(threadName)s]: %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    if sys.stderr.isatty():
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    for noisy in ("httpx", "httpx2", "httpcore", "huggingface_hub", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class Daemon:
    def __init__(self) -> None:
        self.cfg = config.load()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.recorder = Recorder(self.cfg, self.wake)
        self.transcriber = Transcriber(self.cfg, self.wake)
        self.router = Router(self.cfg, self.wake)
        self.fast = FastLane(self.cfg)
        self.fast.early.wake = self.wake
        self.recorder.early = self.transcriber.early = self.fast.early
        self.sessions = None
        self.fast.speech_active = lambda: self.recorder.speech_active
        self.fast.last_voice_mono = lambda: self.recorder.last_voice_mono
        self.fast.cues.speech_active = lambda: self.recorder.speech_active
        self.transcriber.on_utterance = self.fast.offer
        self._start_lock = threading.Lock()
        self._runtime_lock = None
        self._started = False

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            # App launches and `ramble run` must share this guard. Acquiring it
            # before any worker/DB access prevents duplicate mic capture and
            # SELECT-pending races, rather than filtering legitimate repeats.
            self._runtime_lock = acquire_daemon_lock(config.HOME)
            self._started = True
            self._start_workers()

    def _start_workers(self) -> None:
        db.connect().close()
        if self.cfg.get('work_sessions_enabled', True):
            from .sessions import Sessions
            from .speech_queue import SpeechQueue
            self.sessions = Sessions(self.cfg)
            self.sessions.speech = SpeechQueue(self.cfg, self.sessions.store)
            self.sessions.speech.cues = self.fast.cues
            self.fast.cues.session_work = self.sessions.thinking_work
            self.sessions.speech.speech_active = lambda: self.recorder.speech_active
            self.fast.sessions = self.sessions
        threading.Thread(target=self.transcriber.run_forever, args=(self.stop,), name="transcriber", daemon=True).start()
        threading.Thread(target=self.router.run_forever, args=(self.stop,), name="router", daemon=True).start()
        threading.Thread(target=self._mic_control, name="mic-control", daemon=True).start()
        threading.Thread(target=self.fast.run_forever, args=(self.stop,), name="fast-lane", daemon=True).start()
        threading.Thread(target=self.fast.cues.run_forever, args=(self.stop,), name="audio-cues", daemon=True).start()
        if self.sessions:
            threading.Thread(target=self.sessions.run_forever, args=(self.stop,), name='work-sessions', daemon=True).start()
            threading.Thread(target=self.sessions.speech.run_forever, args=(self.stop,), name='speech-output', daemon=True).start()
        log.info("ramble started; pid=%s; state=%s; data=%s", os.getpid(), state.describe(), config.HOME)

    def _mic_control(self) -> None:
        # Opening the mic can block for a long time (e.g. while macOS shows the
        # permission prompt), so it never happens on the menu bar's main thread.
        while not self.stop.is_set():
            try:
                self.sync()
            except Exception:  # noqa: BLE001
                log.exception("mic control error")
            self.stop.wait(1)

    def sync(self) -> None:
        """Make the mic match the on/off state."""
        selected = state.read()
        try:
            want = state.is_listening(selected)
            if want and not self.recorder.active:
                self.recorder.start()
            elif not want and self.recorder.active:
                self.recorder.stop()
                self.wake.set()
        except Exception:
            acknowledge(config.HOME, selected, self.recorder.active, failed=True)
            raise
        acknowledge(config.HOME, selected, self.recorder.active, failed=want and bool(self.recorder.error))

    def shutdown(self) -> None:
        self.stop.set()
        self.wake.set()
        self.fast.cues.player.shutdown()
        self.recorder.stop()
        # The process lock stays held until exit: workers can still be finishing
        # transcription, model generation, or playback after stop is signalled.

    def stats(self) -> dict:
        conn = db.connect()
        today = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        s = {
            "segments_today": q("SELECT COUNT(*) FROM segments WHERE started_at>=? AND status='transcribed'", today),
            "speech_min_today": round(
                (q("SELECT COALESCE(SUM(duration),0) FROM segments WHERE started_at>=?", today) or 0) / 60, 1
            ),
            "thoughts_today": q("SELECT COUNT(*) FROM thoughts WHERE started_at>=?", today),
            "dispatched_today": q("SELECT COUNT(*) FROM dispatches WHERE created_at>=? AND status='created'", today),
            "pending": q("SELECT COUNT(*) FROM segments WHERE status='pending'"),
            "open_thought": q("SELECT COUNT(*) FROM thoughts WHERE status IN ('open','closed','triaging')"),
        }
        conn.close()
        return s

    def run_headless(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            self.shutdown()
