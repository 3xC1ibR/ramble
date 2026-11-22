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
from .dictation import Dictation

log = logging.getLogger("ramble")
MIC_CONTROL_TIMEOUT_S = 30.0
# The first device open may be waiting for the macOS permission prompt.
INITIAL_MIC_CONTROL_TIMEOUT_S = 300.0


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
        self.dictation = Dictation(self.recorder)
        self.recorder.on_failure = self.dictation.cancel
        self.transcriber = Transcriber(self.cfg, self.wake)
        self.router = Router(self.cfg, self.wake)
        self.fast = FastLane(self.cfg)
        self.fast.early.wake = self.wake
        self.recorder.early = self.transcriber.early = self.fast.early
        self.sessions = None
        self.reading = None
        self.reading_questions = None
        self.fast.speech_active = lambda: self.recorder.speech_active
        self.fast.last_voice_mono = lambda: self.recorder.last_voice_mono
        self.fast.cues.speech_active = lambda: self.recorder.speech_active
        self.transcriber.on_utterance = self.fast.offer
        self._start_lock = threading.Lock()
        self._runtime_lock = None
        self._started = False
        self._mic_control_deadline = None

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
            from . import model_settings
            threading.Thread(target=model_settings.run_discovery, args=(self.stop,),
                             name="model-discovery", daemon=True).start()
        from .reading import ReadingController
        from .reading_interaction import ReadingQuestions
        self.reading = ReadingController(self.cfg)
        self.reading.speech_active = lambda: self.recorder.speech_active
        self.reading.can_play = self._reading_can_play
        self.fast.reading = self.reading.store
        self.reading_questions = ReadingQuestions(self.cfg, self.fast.run_codex)
        threading.Thread(target=self.transcriber.run_forever, args=(self.stop,), name="transcriber", daemon=True).start()
        threading.Thread(target=self.router.run_forever, args=(self.stop,), name="router", daemon=True).start()
        threading.Thread(target=self._mic_control, name="mic-control", daemon=True).start()
        threading.Thread(target=self._mic_watchdog, name="mic-watchdog", daemon=True).start()
        threading.Thread(target=self.fast.run_forever, args=(self.stop,), name="fast-lane", daemon=True).start()
        threading.Thread(target=self.fast.cues.run_forever, args=(self.stop,), name="audio-cues", daemon=True).start()
        threading.Thread(target=self.reading.run_forever, args=(self.stop,), name="article-reading", daemon=True).start()
        threading.Thread(target=self.reading_questions.run_forever, args=(self.stop,), name="reading-questions", daemon=True).start()
        if self.sessions:
            threading.Thread(target=self.sessions.run_forever, args=(self.stop,), name='work-sessions', daemon=True).start()
            threading.Thread(target=self.sessions.speech.run_forever, args=(self.stop,), name='speech-output', daemon=True).start()
        if self.cfg.get("output_activity_monitor"):
            # Records which other processes play audio, so segments recorded during
            # laptop playback can be judged as media. Reads Core Audio state only.
            from .output_activity import OutputActivityMonitor
            threading.Thread(target=OutputActivityMonitor(db.connect).run_forever, args=(self.stop,),
                             name="output-activity", daemon=True).start()
        log.info("ramble started; pid=%s; state=%s; data=%s", os.getpid(), state.describe(), config.HOME)

    def _reading_can_play(self):
        if self.fast.busy or not self.fast.q.empty():
            return False
        if self.sessions:
            with self.sessions.store.connect() as conn:
                return not conn.execute(
                    "SELECT 1 FROM session_speech WHERE status IN ('pending','playing') LIMIT 1"
                ).fetchone()
        return True

    def _mic_control(self) -> None:
        # Opening the mic can block for a long time (e.g. while macOS shows the
        # permission prompt), so it never happens on the menu bar's main thread.
        timeout = INITIAL_MIC_CONTROL_TIMEOUT_S
        while not self.stop.is_set():
            self._mic_control_deadline = time.monotonic() + timeout
            try:
                self.sync()
            except Exception:  # noqa: BLE001
                log.exception("mic control error")
            finally:
                self._mic_control_deadline = None
                timeout = MIC_CONTROL_TIMEOUT_S
            self.stop.wait(1)

    def _mic_watchdog(self) -> None:
        """Recover from native audio calls that never return to mic control.

        PortAudio/CoreAudio can deadlock in abort/close as well as device open.
        Retrying on another thread cannot release those native locks. Exit the
        capture owner so Ramble.app and launchd can restart it; normal shutdown
        would call the same blocked driver. The saved listening switch is kept.
        """
        while not self.stop.wait(1):
            deadline = self._mic_control_deadline
            if deadline is None or time.monotonic() < deadline:
                continue
            self.recorder.error = "Microphone control stopped responding; restarting Ramble."
            try:
                log.critical(self.recorder.error)
                acknowledge(config.HOME, state.read(), False, failed=True)
            finally:
                os._exit(1)

    def sync(self) -> None:
        """Make the mic match the on/off state."""
        selected = state.read()
        try:
            want = state.is_listening(selected)
            if want:
                self.recorder.start()
            else:
                self.recorder.stop()
                self.wake.set()
        except Exception:
            acknowledge(config.HOME, selected, self.recorder.active, failed=True)
            raise
        acknowledge(config.HOME, selected, self.recorder.active, failed=want and bool(self.recorder.error))

    def shutdown(self) -> None:
        self.dictation.cancel()
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
