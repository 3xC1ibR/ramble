"""The long-running process: recorder (follows the on/off state), transcriber, router."""

from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time

from . import config, db, state
from .recorder import Recorder
from .router import Router
from .transcribe import Transcriber

log = logging.getLogger("ramble")


def setup_logging() -> None:
    config.ensure_dirs()
    handler = logging.handlers.RotatingFileHandler(config.LOG_DIR / "ramble.log", maxBytes=5_000_000, backupCount=3)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
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

    def start(self) -> None:
        db.connect().close()
        threading.Thread(target=self.transcriber.run_forever, args=(self.stop,), name="transcriber", daemon=True).start()
        threading.Thread(target=self.router.run_forever, args=(self.stop,), name="router", daemon=True).start()
        threading.Thread(target=self._mic_control, name="mic-control", daemon=True).start()
        log.info("ramble started; state=%s; data=%s", state.describe(), config.HOME)

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
        want = state.is_listening()
        if want and not self.recorder.active:
            self.recorder.start()
        elif not want and self.recorder.active:
            self.recorder.stop()
            self.wake.set()

    def shutdown(self) -> None:
        self.recorder.stop()
        self.stop.set()
        self.wake.set()

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
