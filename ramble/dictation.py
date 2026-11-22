"""Explicit microphone ownership and completion for shortcut dictation.

The recorder serializes boundaries with audio. Whisper persists dictation as
completed thoughts; only this in-memory session may request a paste. Restarting
can finish stored transcription but can never replay an old paste.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from . import db, state
from .activity import Activity

log = logging.getLogger("ramble.dictation")
MAX_RECORDING_SECONDS = 600
COMPLETION_TIMEOUT_SECONDS = 120
NOTICE_SECONDS = 6


@dataclass
class Recording:
    id: str
    target: object
    started_at: float = field(default_factory=time.monotonic)
    sealed: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)


class Dictation:
    def __init__(self, recorder):
        self.recorder = recorder
        self.recording = None
        self.finishing = False
        self.activity = None
        self.notice_until = 0
        self.results = queue.Queue()
        self.undo_receipt = None

    @property
    def can_undo(self):
        return self.undo_receipt is not None and time.monotonic() < self.notice_until

    def undo(self, desktop):
        receipt = self.undo_receipt if self.can_undo else None
        self.undo_receipt = None
        if receipt is None:
            return
        try:
            undone, detail = desktop.undo(receipt)
            self.notice("Undone" if undone else "Undo unavailable", detail, error=not undone)
        except Exception:
            log.exception("Dictation undo failed")
            self.notice("Undo unavailable", "Use Command+Z in the original input.", error=True)

    def notice(self, label, detail, *, error=False):
        self.activity = Activity("error" if error else "processing", label, detail)
        self.notice_until = time.monotonic() + NOTICE_SECONDS

    def toggle(self, desktop):
        if self.recording:
            if not self.finishing:
                self.finish()
            return
        self.undo_receipt = None
        if not state.is_listening() or not self.recorder.active or self.recorder.error:
            self.notice("Mic off", "Turn on Ramble's microphone before starting dictation.", error=True)
            return
        self.recording = Recording(db.new_id(), desktop.target())
        self.finishing = False
        self.activity = Activity("dictation", "Dictating", "Dictating · press the shortcut again to finish and paste.")
        self.recorder.set_dictation(self.recording.id)

    def finish(self):
        recording = self.recording
        if not recording or self.finishing:
            return
        self.finishing = True
        self.activity = Activity("processing", "Transcribing", "Finishing dictation before copying and pasting.")
        self.recorder.set_dictation(None, recording.sealed.set)
        threading.Thread(target=self._collect, args=(recording,), name="dictation-completion", daemon=True).start()

    def _collect(self, recording):
        deadline = time.monotonic() + COMPLETION_TIMEOUT_SECONDS
        conn = None
        try:
            while not recording.cancelled.is_set():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Transcription timed out. The recording is saved in Ramble's data folder.")
                if recording.sealed.wait(.1):
                    if conn is None:
                        conn = db.connect()
                    rows = conn.execute("SELECT status,text FROM segments WHERE source=? ORDER BY started_at,id",
                                        (f"dictation:{recording.id}",)).fetchall()
                    if not any(row['status'] == 'pending' for row in rows):
                        text = " ".join(row['text'].strip() for row in rows
                                        if row['status'] == 'transcribed' and row['text'])
                        error = "Some audio could not be transcribed. Available text was copied; the recording is saved." if any(
                            row['status'] == 'error' for row in rows) else None
                        self.results.put((recording.id, text, error))
                        return
                    recording.cancelled.wait(.1)
        except Exception as exc:
            log.exception("Dictation completion failed")
            self.results.put((recording.id, "", str(exc)))
        finally:
            if conn is not None:
                conn.close()

    def cancel(self, detail="Dictation cancelled. Nothing was copied or pasted."):
        if not self.recording:
            return
        self.recording.cancelled.set()
        self.recorder.set_dictation(None)
        self.recording = None
        self.finishing = False
        self.notice("Cancelled", detail)

    def poll(self, desktop):
        """Called on AppKit's main thread; paste and clipboard access stay here."""
        if self.undo_receipt is not None and (not self.can_undo or not desktop.undo_unchanged(self.undo_receipt)):
            self.undo_receipt = None
            if self.activity and self.activity.mode == "pasted":
                self.activity = None
        recording = self.recording
        if recording and (not state.is_listening() or not self.recorder.active or self.recorder.error):
            self.cancel("Dictation cancelled because microphone capture stopped.")
        elif recording and not self.finishing and time.monotonic() - recording.started_at >= MAX_RECORDING_SECONDS:
            self.finish()
        while not self.results.empty():
            identifier, text, error = self.results.get_nowait()
            if not self.recording or identifier != self.recording.id:
                continue
            target = self.recording.target
            self.recording = None
            self.finishing = False
            try:
                if text:
                    desktop.copy(text)
                if error:
                    self.notice("Check dictation", error, error=True)
                elif not text:
                    self.notice("No speech", "No speech recognized. The clipboard was left unchanged.")
                else:
                    receipt = desktop.prepare_undo(target, text)
                    pasted, detail = desktop.paste(target)
                    self.notice("Pasted" if pasted else "Copied", detail)
                    if pasted:
                        self.undo_receipt = receipt
                        self.activity = Activity("pasted", "Pasted", "Dictation pasted · Undo is available for six seconds.")
            except Exception as exc:
                log.exception("Dictation delivery failed")
                self.notice("Paste failed", f"{exc}. The transcript is saved in Ramble's data folder.", error=True)
        if not self.recording and time.monotonic() >= self.notice_until:
            self.activity = None
