"""Decide, per speech segment, whether the words are the owner's.

Three independent signals, combined so that every uncertainty keeps the words
(fail open):

1. Transcription artifacts: Whisper repetition loops (gzip compression ratio
   above 2.4, Whisper's own rejection criterion) and echoes of the vocabulary
   hint. These are never anyone's speech.
2. Owner-voice verification: cosine similarity of a WeSpeaker ResNet34
   embedding to the enrolled owner centroid (see ``speaker_embedding``).
3. External playback: whether another process on this Mac was sending audio to
   an output while the segment was recorded (see ``output_activity``).

Rule, in order:
  artifact                                   -> remove  (transcription_artifact)
  no score (no profile, model error, audio)  -> keep    (unverified)
  score >= threshold                         -> keep    (owner_verified)
  playback active for >= half the segment    -> remove  (external_playback)
  duration < min_judged_s                    -> keep    (too_short_to_judge)
  otherwise                                  -> remove  (other_voice)

Clips under two seconds keep their words unless the Mac was playing audio,
because short owner commands score poorly; that was the dominant failure of
the earlier shadow filter. Verdicts are cached in ``segment_sources``.
"""
from __future__ import annotations

import logging
import re
import time
import zlib
from dataclasses import dataclass
from pathlib import Path

from . import config, output_activity

log = logging.getLogger("ramble.source")

SCHEMA = """CREATE TABLE IF NOT EXISTS segment_sources (
    segment_id TEXT PRIMARY KEY,
    keep INTEGER NOT NULL,
    reason TEXT NOT NULL,
    score REAL,
    playback REAL,
    model TEXT,
    judged_at REAL NOT NULL
);
"""

KEEP_REASONS = {"unverified", "owner_verified", "too_short_to_judge"}
REMOVE_REASONS = {"transcription_artifact", "external_playback", "other_voice"}
REMOVED_MARKER = "[other voice removed]"
_WORD = re.compile(r"[a-z0-9']+")

DEFAULTS = {
    "source_gate_profile": "owner-voice.json",  # relative to RAMBLE_HOME
    "source_gate_min_judged_s": 2.0,
    "source_gate_playback_fraction": 0.5,
    "source_gate_compression_ratio": 2.4,
    "source_gate_triage": False,  # skip Codex triage for thoughts with no verified owner segment
}


@dataclass(frozen=True)
class Verdict:
    keep: bool
    reason: str
    score: float | None = None
    playback: float | None = None


def setting(cfg: dict, key: str):
    return cfg.get(key, DEFAULTS[key])


def is_artifact(text: str, cfg: dict) -> bool:
    text = (text or "").strip()
    if not text:
        return True
    raw = text.encode("utf-8")
    if len(raw) > 60 and len(raw) / len(zlib.compress(raw)) > setting(cfg, "source_gate_compression_ratio"):
        return True
    hint = set(_WORD.findall((cfg.get("whisper_initial_prompt") or "").lower()))
    words = _WORD.findall(text.lower())
    return bool(hint and words and set(words) <= hint)


def judge(*, text: str, duration: float, score: float | None, threshold: float | None,
          playback: float | None, cfg: dict) -> Verdict:
    if is_artifact(text, cfg):
        return Verdict(False, "transcription_artifact", score, playback)
    if score is None or threshold is None:
        return Verdict(True, "unverified", score, playback)
    if score >= threshold:
        return Verdict(True, "owner_verified", score, playback)
    if playback is not None and playback >= setting(cfg, "source_gate_playback_fraction"):
        return Verdict(False, "external_playback", score, playback)
    if duration < setting(cfg, "source_gate_min_judged_s"):
        return Verdict(True, "too_short_to_judge", score, playback)
    return Verdict(False, "other_voice", score, playback)


class SourceGate:
    """Scores segments once and caches the verdict; any failure keeps the words."""

    def __init__(self, cfg: dict, embedder=None, profile=None, read_audio=None):
        self.cfg = cfg
        self.embedder = embedder
        self.profile = profile
        self.read_audio = read_audio or _read_audio
        self.model_name = "wespeaker-resnet34-lm"
        self._loaded = embedder is not None and profile is not None

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        from . import speaker_embedding
        path = Path(setting(self.cfg, "source_gate_profile"))
        path = path if path.is_absolute() else config.HOME / path
        try:
            self.profile = speaker_embedding.OwnerProfile.load(path)
            model = speaker_embedding.default_model_path()
            if model is None or not model.exists():
                raise FileNotFoundError("WeSpeaker model is not cached locally")
            self.embedder = speaker_embedding.SpeakerEmbedder(model)
        except Exception as exc:  # noqa: BLE001
            log.warning("owner-voice verification unavailable (%s); keeping all words", exc)
            self.profile = self.embedder = None

    def score(self, audio_path: str) -> float | None:
        self._load()
        if self.embedder is None or self.profile is None:
            return None
        try:
            return self.profile.score(self.embedder.embed(self.read_audio(audio_path)))
        except Exception as exc:  # noqa: BLE001
            log.warning("owner-voice scoring failed for %s: %s", audio_path, exc)
            return None

    def verdict(self, conn, segment) -> Verdict:
        conn.execute(SCHEMA)  # one statement: no implicit COMMIT of the caller's transaction
        cached = conn.execute("SELECT keep, reason, score, playback FROM segment_sources WHERE segment_id=?",
                              (segment["id"],)).fetchone()
        if cached is not None:
            return Verdict(bool(cached[0]), cached[1], cached[2], cached[3])
        text = segment["text"] or ""
        score = None if is_artifact(text, self.cfg) else self.score(segment["audio_path"])
        playback = output_activity.active_fraction(conn, segment["started_at"], segment["ended_at"])
        v = judge(text=text, duration=float(segment["duration"]), score=score,
                  threshold=self.profile.threshold if self.profile else None, playback=playback, cfg=self.cfg)
        if v.reason != "unverified":  # unverified verdicts are retried once a profile exists
            conn.execute("INSERT OR REPLACE INTO segment_sources VALUES (?,?,?,?,?,?,?)",
                         (segment["id"], int(v.keep), v.reason, v.score, v.playback, self.model_name, time.time()))
        return v


def _read_audio(path: str):
    import soundfile as sf
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    if sr != 16000:
        raise ValueError(f"expected 16 kHz audio, got {sr}")
    return audio.mean(axis=1)


def thought_segments(conn, thought_id: str) -> list:
    return conn.execute("SELECT * FROM segments WHERE thought_id=? AND status='transcribed' AND text IS NOT NULL "
                        "ORDER BY started_at", (thought_id,)).fetchall()


def triage_decision(conn, thought, gate: SourceGate) -> dict | None:
    """A local 'ambient' decision when no segment is verified as the owner; None means run normal triage."""
    if not setting(gate.cfg, "source_gate_triage"):
        return None
    segments = thought_segments(conn, thought["id"])
    if not segments:
        return None
    verdicts = [gate.verdict(conn, s) for s in segments]
    # Triage needs positive evidence of the owner's voice. Clips kept only because
    # they were too short to judge are mostly media fragments; addressed commands
    # reach the fast lane, which this gate never touches. Unverified means fail open.
    if any(v.reason in ("owner_verified", "unverified") for v in verdicts):
        return None
    reasons = sorted({v.reason for v in verdicts})
    return {"classification": "ambient", "summary": "No owner speech detected (" + ", ".join(reasons) + ").",
            "items": [], "notify": None, "source_gate": True}
