"""Explicit spoken replies, with cross-process mic feedback protection."""

from __future__ import annotations

import fcntl
import json
import logging
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from . import config, db

log = logging.getLogger("ramble.speech")


@dataclass(frozen=True)
class SpeechResult:
    path: Path
    engine: str
    duration: float


def overlaps_playback(conn, started_at: float, ended_at: float) -> bool:
    """Historical windows also protect audio waiting in the recorder queue."""
    return conn.execute(
        "SELECT 1 FROM speech_playbacks WHERE started_at < ? AND ended_at > ? LIMIT 1",
        (ended_at, started_at),
    ).fetchone() is not None


def _duration(path: Path) -> float:
    info = sf.info(path)
    if info.frames <= 0 or info.samplerate <= 0:
        raise ValueError(f"TTS returned empty audio: {path}")
    return info.duration


def synthesize(text: str, cfg: dict | None = None, *, voice: str | None = None,
               output: str | Path | None = None) -> SpeechResult:
    """Generate a WAV without playing or ingesting it; failures are never hidden."""
    if not text.strip():
        raise ValueError("speech text must not be empty")
    cfg = cfg if cfg is not None else config.load()
    folder = config.HOME / "speech"
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(output).expanduser().resolve() if output else folder / f"{db.new_id()}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        cfg["tts_url"],
        data=json.dumps({"model": cfg["tts_model"], "input": text,
                         "voice": voice or cfg["tts_voice"], "speed": cfg["tts_speed"],
                         "response_format": "wav"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg["tts_timeout_s"]) as response:
            path.write_bytes(response.read())
        return SpeechResult(path, "kokoro", _duration(path))
    except Exception as exc:
        log.warning("Kokoro unavailable or invalid audio (%s); falling back to macOS say", exc)
    args = ["/usr/bin/say", "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@22050"]
    if cfg["tts_fallback_voice"]:
        args.extend(["-v", cfg["tts_fallback_voice"]])
    subprocess.run([*args, "--", text], check=True, timeout=cfg["tts_timeout_s"])
    return SpeechResult(path, "macos-say", _duration(path))


def play_audio(path: str | Path, cfg: dict | None = None) -> None:
    """Play synchronously with a durable, bounded feedback window (never state.json)."""
    cfg = cfg if cfg is not None else config.load()
    path = Path(path).resolve()
    duration = _duration(path)
    tail = max(0.0, float(cfg["tts_guard_tail_s"]))
    timeout = duration + 15.0
    conn = db.connect()
    playback_id = db.new_id()
    # Commit before starting afplay. A finite upper bound recovers after a crash.
    started_at = time.time() - 0.1
    conn.execute(
        "INSERT INTO speech_playbacks VALUES (?,?,?,?)",
        (playback_id, started_at, time.time() + timeout + tail, str(path)),
    )
    log.info("playback %s started; mic feedback guard active", playback_id)
    try:
        subprocess.run(["/usr/bin/afplay", str(path)], check=True, timeout=timeout)
    finally:
        ended_at = time.time() + tail
        conn.execute("UPDATE speech_playbacks SET ended_at=? WHERE id=?", (ended_at, playback_id))
        conn.close()
        log.info("playback %s ended; guard through %.3f", playback_id, ended_at)


def speak(text: str, cfg: dict | None = None, *, voice: str | None = None,
          output: str | Path | None = None) -> SpeechResult:
    """Other components can call this blocking function to talk back safely."""
    cfg = cfg if cfg is not None else config.load()
    config.ensure_dirs()
    # CLI and app callers share a lock so replies cannot speak over each other.
    with (config.HOME / "speech.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = synthesize(text, cfg, voice=voice, output=output)
        play_audio(result.path, cfg)
        return result
