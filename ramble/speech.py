"""Explicit spoken replies, with cross-process mic feedback protection."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import time
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from . import config, db, latency, state
from .cue_player import PlaybackCancelled, shared_player

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


def _elevenlabs(text: str, cfg: dict, voice: str | None, path: Path) -> None:
    key_file = cfg.get("elevenlabs_api_key_file")
    key = (Path(key_file).expanduser().read_text() if key_file
           else os.environ.get("ELEVENLABS_API_KEY", "")).strip()
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValueError("ElevenLabs API key is missing or invalid")
    voice_id = urllib.parse.quote(voice or cfg["elevenlabs_voice_id"], safe="")
    request = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=pcm_24000",
        data=json.dumps({
            "text": text,
            "model_id": cfg["elevenlabs_model"],
            "voice_settings": {
                "stability": cfg["elevenlabs_stability"],
                "similarity_boost": cfg["elevenlabs_similarity_boost"],
                "style": cfg["elevenlabs_style"],
                "use_speaker_boost": cfg["elevenlabs_use_speaker_boost"],
                "speed": cfg["tts_speed"],
            },
        }).encode(),
        headers={"Content-Type": "application/json", "xi-api-key": key},
    )
    with urllib.request.urlopen(request, timeout=cfg["tts_timeout_s"]) as response:
        pcm = response.read()
    if not pcm or len(pcm) % 2:
        raise ValueError("ElevenLabs returned empty or incomplete PCM audio")
    # 24 kHz PCM works below Pro tier; wrap it so callers still get a real WAV.
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)


@latency.measured("synthesis")
def synthesize(text: str, cfg: dict | None = None, *, voice: str | None = None,
               output: str | Path | None = None) -> SpeechResult:
    """Generate a WAV without playing or ingesting it; failures are never hidden."""
    if not text.strip():
        raise ValueError("speech text must not be empty")
    cfg = cfg if cfg is not None else config.load()
    provider = cfg.get("tts_provider", "kokoro")
    if provider not in {"kokoro", "elevenlabs"}:
        raise ValueError(f"Unknown TTS provider: {provider}")
    folder = config.HOME / "speech"
    folder.mkdir(parents=True, exist_ok=True)
    path = Path(output).expanduser().resolve() if output else folder / f"{db.new_id()}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if provider == "elevenlabs":
            _elevenlabs(text, cfg, voice, path)
        else:
            request = urllib.request.Request(
                cfg["tts_url"],
                data=json.dumps({"model": cfg["tts_model"], "input": text,
                                 "voice": voice or cfg["tts_voice"], "speed": cfg["tts_speed"],
                                 "response_format": "wav"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=cfg["tts_timeout_s"]) as response:
                path.write_bytes(response.read())
        return SpeechResult(path, provider, _duration(path))
    except Exception as exc:
        log.warning("%s unavailable or invalid audio (%s); falling back to macOS say", provider, exc)
    args = ["/usr/bin/say", "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@22050"]
    if cfg["tts_fallback_voice"]:
        args.extend(["-v", cfg["tts_fallback_voice"]])
    subprocess.run([*args, "--", text], check=True, timeout=cfg["tts_timeout_s"])
    return SpeechResult(path, "macos-say", _duration(path))


def play_audio(path: str | Path, cfg: dict | None = None, *, cancelled=None, origin='reply',
               on_playback_start=None) -> None:
    """Play synchronously with a durable, bounded feedback window (never state.json)."""
    cfg = cfg if cfg is not None else config.load()
    since = time.time()
    state.check_audio(since)
    if cancelled and cancelled():
        raise state.PausedError('Speech cancelled')
    path = Path(path).resolve()
    duration = _duration(path)
    tail = max(0.0, float(cfg["tts_guard_tail_s"]))
    timeout = duration + 15.0
    conn = db.connect()
    playback_id = db.new_id()
    # Commit before submitting samples. A finite upper bound recovers after a crash.
    started_at = time.time() - 0.1
    conn.execute(
        "INSERT INTO speech_playbacks VALUES (?,?,?,?)",
        (playback_id, started_at, time.time() + timeout + tail, str(path)),
    )
    conn.execute('INSERT INTO speech_origins VALUES (?,?)', (playback_id, origin))
    log.info("playback %s started; mic feedback guard active", playback_id)
    try:
        requested_at = time.monotonic()
        def started(receipt):
            latency.event("playback_first_frame", playback_id=playback_id,
                          first_frame_mono_s=receipt['first_frame_mono_s'],
                          output_start_delay_s=receipt['first_frame_mono_s'] - requested_at,
                          timing_source='portaudio_dac_estimate')
            if on_playback_start:
                on_playback_start()
        def is_cancelled():
            return not state.audio_allowed(since) or bool(cancelled and cancelled())
        # Includes preparation through confirmed final DAC time. First-frame
        # timing is an output-driver estimate, not an acoustic measurement.
        with latency.span("playback_command", playback_id=playback_id, audio_path=str(path),
                          backend="persistent_stream"):
            shared_player().play_file(path, timeout=timeout, cancelled=is_cancelled, on_start=started)
    except PlaybackCancelled as exc:
        raise state.PausedError(str(exc)) from exc
    finally:
        ended_at = time.time() + tail
        conn.execute("UPDATE speech_playbacks SET ended_at=? WHERE id=?", (ended_at, playback_id))
        conn.close()
        log.info("playback %s ended; guard through %.3f", playback_id, ended_at)


def speak(text: str, cfg: dict | None = None, *, voice: str | None = None,
          output: str | Path | None = None, cancelled=None, origin='reply',
          on_playback_start=None) -> SpeechResult:
    """Other components can call this blocking function to talk back safely."""
    cfg = cfg if cfg is not None else config.load()
    since = time.time()
    state.check_audio(since)
    config.ensure_dirs()
    if cancelled and cancelled():
        raise state.PausedError('Speech cancelled')
    # Synthesis does not use the speaker. Let working cues continue until the
    # generated file can actually play; serialize only speaker access.
    result = synthesize(text, cfg, voice=voice, output=output)
    latency.event("speech_file_ready", audio_path=str(result.path), engine=result.engine,
                  audio_duration_s=result.duration)
    # CLI and app callers share a lock so replies cannot speak over each other.
    with (config.HOME / "speech.lock").open("a") as lock:
        with latency.span("speech_lock"):
            if cancelled:
                while True:
                    if cancelled():
                        raise state.PausedError('Speech cancelled')
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(.1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX)
        state.check_audio(since)
        if cancelled and cancelled():
            raise state.PausedError('Speech cancelled')
        options = {'cancelled': cancelled, 'origin': origin} if cancelled or origin != 'reply' else {}
        if on_playback_start:
            options['on_playback_start'] = on_playback_start
        play_audio(result.path, cfg, **options)
        return result
