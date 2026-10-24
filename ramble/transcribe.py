"""Local transcription with MLX Whisper, then grouping segments into thoughts.

A thought is a run of transcribed segments with gaps shorter than thought_gap_s.
The router closes a thought once it has been quiet for thought_gap_s (or it gets
longer than thought_max_s) and hands it to the triage agent.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, db

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

    def warmup(self) -> None:
        import mlx_whisper

        mlx_whisper.transcribe(
            np.zeros(config.SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=self.cfg["whisper_model"]
        )

    def transcribe_path(self, path: str) -> dict:
        import mlx_whisper

        audio, sr = sf.read(path, dtype="float32")
        assert sr == config.SAMPLE_RATE, sr
        return mlx_whisper.transcribe(
            audio,
            path_or_hf_repo=self.cfg["whisper_model"],
            language=self.cfg["language"],
            condition_on_previous_text=False,
        )

    def run_forever(self, stop: threading.Event) -> None:
        conn = db.connect()
        try:
            self.warmup()
            log.info("whisper ready (%s)", self.cfg["whisper_model"])
        except Exception:  # noqa: BLE001
            log.exception("whisper warmup failed")
        while not stop.is_set():
            row = conn.execute(
                "SELECT * FROM segments WHERE status='pending' ORDER BY started_at LIMIT 1"
            ).fetchone()
            self.backlog = conn.execute("SELECT COUNT(*) FROM segments WHERE status='pending'").fetchone()[0]
            if not row:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.process(conn, row)

    def process(self, conn, row) -> None:
        try:
            r = self.transcribe_path(row["audio_path"])
        except Exception as e:  # noqa: BLE001
            log.exception("transcribe failed %s", row["id"])
            conn.execute("UPDATE segments SET status='error', error=? WHERE id=?", (str(e), row["id"]))
            return
        text = (r.get("text") or "").strip()
        segs = r.get("segments") or []
        no_speech = max((s.get("no_speech_prob", 0) for s in segs), default=1.0)
        logprob = float(np.mean([s.get("avg_logprob", -2) for s in segs])) if segs else -2.0
        if looks_empty(text, no_speech, logprob):
            conn.execute(
                "UPDATE segments SET status='empty', text=?, no_speech_prob=?, avg_logprob=? WHERE id=?",
                (text, no_speech, logprob, row["id"]),
            )
            return
        thought_id = attach_to_thought(conn, self.cfg, row, text)
        conn.execute(
            "UPDATE segments SET status='transcribed', text=?, language=?, no_speech_prob=?, avg_logprob=?, thought_id=? WHERE id=?",
            (text, r.get("language"), no_speech, logprob, thought_id, row["id"]),
        )
        Path(row["audio_path"]).with_suffix(".txt").write_text(text + "\n")
        log.info("heard [%s] %s", row["id"][-6:], text[:120])
        if self.on_utterance:
            try:
                self.on_utterance(conn, row, text)
            except Exception:  # noqa: BLE001
                log.exception("fast lane hook failed")


def stamp(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def attach_to_thought(conn, cfg: dict, seg, text: str) -> str:
    """Append to the open thought if close enough in time, else close it and start a new one."""
    line = f"[{stamp(seg['started_at'])}] {text}"
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
