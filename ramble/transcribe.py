"""Local transcription with MLX Whisper, then grouping segments into thoughts.

A thought is a run of transcribed segments with gaps shorter than thought_gap_s.
The router closes a thought once it has been quiet for thought_gap_s (or it gets
longer than thought_max_s) and hands it to the triage agent.
"""

from __future__ import annotations

import logging
import re
import resource
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, db, latency, state
from .addressing import has_name
from .recognition_memory import RecognitionMemory

log = logging.getLogger("ramble.transcribe")

# Whisper's classic hallucinations on near-silence / noise.
HALLUCINATIONS = {
    "",
    "you",
    "thank you",
    "thank you.",
    "thanks for watching",
    "thanks for watching!",
    "thank you for watching",
    "thank you for watching.",
    "bye",
    "bye.",
    "okay",
    "so",
    "...",
    ".",
}
NORMALIZE = re.compile(r"[^a-z ]+")


def looks_empty(text: str, no_speech: float, logprob: float) -> bool:
    norm = NORMALIZE.sub("", text.lower()).strip()
    if norm in HALLUCINATIONS or text.strip() in HALLUCINATIONS:
        return True
    return no_speech > 0.6 and logprob < -1.0


class Transcriber:
    def __init__(self, cfg: dict, wake: threading.Event) -> None:
        self.cfg = cfg
        self.wake = wake
        self.backlog = 0
        self.on_utterance = None  # fast lane hook: (conn, segment_row, text) -> bool
        self.early = None
        self.memory = RecognitionMemory(cfg)
        self._last_decode_at = None

    def warmup(self) -> None:
        self._decode(
            np.zeros(config.SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=self.cfg["whisper_model"],
            language=self.cfg["language"], decode_pass="warmup",
        )

    def _decode(self, audio, *, decode_pass: str, segment_id: str | None = None, **options) -> dict:
        self.memory.prepare()  # Must precede mlx_whisper's first import.
        self.memory.set_resident(True)
        import mlx_whisper

        now = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_SELF)
        fields = dict(segment_id=segment_id, decode_pass=decode_pass,
                      audio_duration_s=len(audio) / config.SAMPLE_RATE,
                      idle_s=None if self._last_decode_at is None else now - self._last_decode_at)
        try:
            with latency.span("transcription_decode", **fields):
                return mlx_whisper.transcribe(audio, **options)
        finally:
            self._last_decode_at = time.monotonic()
            after = resource.getrusage(resource.RUSAGE_SELF)
            latency.event("transcription_decode_memory", segment_id=segment_id, decode_pass=decode_pass,
                          process_major_faults=after.ru_majflt - before.ru_majflt,
                          process_minor_faults=after.ru_minflt - before.ru_minflt,
                          **self.memory.snapshot())

    def transcribe_path(self, path: str, *, segment_id: str | None = None) -> dict:
        audio, sr = sf.read(path, dtype="float32")
        assert sr == config.SAMPLE_RATE, sr
        options = dict(
            path_or_hf_repo=self.cfg["whisper_model"],
            language=self.cfg["language"],
            condition_on_previous_text=False,
        )
        prompt = self.cfg.get("whisper_initial_prompt") or None
        result = self._decode(
            audio,
            initial_prompt=prompt,
            decode_pass="hinted" if prompt else "unhinted", segment_id=segment_id,
            **options,
        )
        # Hints can turn "Right, but..." into "Ramble...". A newly named
        # transcript must also sound like an address without vocabulary bias.
        if prompt and has_name((result.get("text") or "").strip()):
            unbiased = self._decode(audio, initial_prompt=None, decode_pass="confirmation",
                                    segment_id=segment_id, **options)
            if not has_name((unbiased.get("text") or "").strip()):
                log.info("vocabulary hint introduced an unconfirmed name; keeping unhinted transcript")
                return unbiased
        return result

    def run_forever(self, stop: threading.Event) -> None:
        conn = db.connect()
        try:
            try:
                self.warmup()
                log.info("whisper ready (%s; language=%s; vocabulary hints=%s)",
                         self.cfg["whisper_model"], self.cfg["language"] or "auto",
                         bool(self.cfg.get("whisper_initial_prompt")))
            except Exception:  # noqa: BLE001
                log.exception("whisper warmup failed")
            while not stop.is_set():
                row = conn.execute(
                    "SELECT * FROM segments WHERE status='pending' ORDER BY started_at LIMIT 1"
                ).fetchone()
                self.backlog = conn.execute("SELECT COUNT(*) FROM segments WHERE status='pending'").fetchone()[0]
                # Off/snooze releases the reservation without changing the mic
                # switch or decoding dummy audio. Pending files can still finish.
                self.memory.set_resident(state.is_listening() or row is not None)
                if not row:
                    preview = self.early.take() if self.early else None
                    if preview is not None:
                        self.process_preview(preview)
                        continue
                    self.wake.wait(5)
                    self.wake.clear()
                    continue
                self.process(conn, row)
        finally:
            self.memory.close()
            conn.close()

    def process_preview(self, preview) -> None:
        if not self.early.valid(preview):
            return
        try:
            result = self._decode(
                preview.audio, decode_pass="wake_preview", initial_prompt=None,
                path_or_hf_repo=self.cfg["whisper_model"], language=self.cfg["language"],
                condition_on_previous_text=False, temperature=0.0,
                without_timestamps=True, sample_len=48,
            )
            self.early.recognize(preview, result)
        except Exception:  # A preview failure must never lose the complete request.
            log.exception("early wake preview failed")

    def process(self, conn, row) -> None:
        try:
            with latency.span("transcription", segment_id=row["id"]):
                r = self.transcribe_path(row["audio_path"], segment_id=row["id"])
        except Exception as e:  # noqa: BLE001
            if self.early:
                self.early.discard(row['started_at'])
            log.exception("transcribe failed %s", row["id"])
            conn.execute("UPDATE segments SET status='error', error=? WHERE id=?", (str(e), row["id"]))
            return
        text = (r.get("text") or "").strip()
        segs = r.get("segments") or []
        no_speech = max((s.get("no_speech_prob", 0) for s in segs), default=1.0)
        logprob = float(np.mean([s.get("avg_logprob", -2) for s in segs])) if segs else -2.0
        if looks_empty(text, no_speech, logprob):
            if self.early:
                self.early.discard(row['started_at'])
            conn.execute(
                "UPDATE segments SET status='empty', text=?, no_speech_prob=?, avg_logprob=? WHERE id=?",
                (text, no_speech, logprob, row["id"]),
            )
            return
        # Publish transcription, durable ownership and grouping together. The
        # router must not see text before the fast lane has reserved it.
        nested = conn.in_transaction
        conn.execute('SAVEPOINT transcription' if nested else 'BEGIN IMMEDIATE')
        try:
            conn.execute(
                "UPDATE segments SET status='transcribed', text=?, language=?, no_speech_prob=?, avg_logprob=? WHERE id=?",
                (text, r.get("language"), no_speech, logprob, row["id"]),
            )
            if self.on_utterance:
                try:
                    self.on_utterance(conn, row, text)
                except Exception:  # noqa: BLE001
                    if self.early:
                        self.early.discard(row['started_at'])
                    log.exception("fast lane hook failed")
            owned = conn.execute('SELECT 1 FROM session_claims WHERE segment_id=?', (row['id'],)).fetchone()
            thought_id = attach_to_thought(conn, self.cfg, row, text, isolated=bool(owned))
            conn.execute('UPDATE segments SET thought_id=? WHERE id=?', (thought_id, row['id']))
            conn.execute('RELEASE transcription') if nested else conn.commit()
        except BaseException:
            if nested:
                conn.execute('ROLLBACK TO transcription')
                conn.execute('RELEASE transcription')
            else:
                conn.rollback()
            raise
        Path(row["audio_path"]).with_suffix(".txt").write_text(text + "\n")
        log.info("heard [%s] %s", row["id"][-6:], text[:120])


def stamp(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def attach_to_thought(conn, cfg: dict, seg, text: str, *, isolated=False) -> str:
    """Append to the open thought if close enough in time, else close it and start a new one."""
    line = f"[{stamp(seg['started_at'])}] {text}"
    if isolated:
        # Accepted input has its own lifecycle and cannot postpone an unrelated
        # open thought. Candidate thoughts wait for their admission decision.
        tid = db.new_id()
        conn.execute(
            "INSERT INTO thoughts(id,started_at,ended_at,text,status) VALUES (?,?,?,?,'closed')",
            (tid, seg['started_at'], seg['ended_at'], line),
        )
        return tid
    open_t = conn.execute("SELECT * FROM thoughts WHERE status='open' ORDER BY started_at DESC LIMIT 1").fetchone()
    if open_t and seg["started_at"] - open_t["ended_at"] <= cfg["thought_gap_s"]:
        conn.execute(
            "UPDATE thoughts SET ended_at=?, text=text || char(10) || ? WHERE id=?",
            (seg["ended_at"], line, open_t["id"]),
        )
        return open_t["id"]
    if open_t:
        conn.execute("UPDATE thoughts SET status='closed' WHERE id=?", (open_t["id"],))
    tid = db.new_id()
    conn.execute(
        "INSERT INTO thoughts (id, started_at, ended_at, text) VALUES (?,?,?,?)",
        (tid, seg["started_at"], seg["ended_at"], line),
    )
    return tid
