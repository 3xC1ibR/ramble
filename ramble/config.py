"""Paths and settings.

All runtime data lives under RAMBLE_HOME (default ~/.ramble), never in the repo.
Settings are defaults below, overridden by RAMBLE_HOME/config.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOME = Path(os.environ.get("RAMBLE_HOME", Path.home() / ".ramble")).expanduser()

AUDIO_DIR = HOME / "audio"
DIGEST_DIR = HOME / "digest"
LOG_DIR = HOME / "logs"
TRIAGE_DIR = HOME / "triage"
DB_PATH = HOME / "ramble.db"
STATE_PATH = HOME / "state.json"
CONFIG_PATH = HOME / "config.json"

VAD_MODEL = REPO / "models" / "silero_vad.onnx"
TRIAGE_PROMPT = REPO / "prompts" / "triage.md"

SAMPLE_RATE = 16000

DEFAULTS = {
    # Capture
    "input_device": None,  # sounddevice name/index; None = system default input
    "vad_threshold": 0.5,  # Silero speech probability to start/continue speech
    "segment_silence_s": 0.9,  # silence that ends an utterance segment
    "segment_max_s": 30.0,  # hard cut for very long utterances
    "pad_s": 0.3,  # audio kept before/after speech
    "min_speech_s": 0.5,  # drop blips shorter than this
    # Transcription (local, Apple Silicon via MLX)
    "whisper_model": "mlx-community/whisper-large-v3-turbo",
    "language": "en",  # primary language; None restores auto-detection
    "whisper_initial_prompt": "Ramble, the user, Buddy, Ribbon Field, Stoic, ElevenLabs.",
    "whisper_wired_mb": 2048,  # resident budget while listening; 0 disables; capped at 1/4 RAM
    "whisper_cache_mb": 128,  # unused MLX buffers; null keeps MLX's default
    "whisper_residency_refresh_ms": 1000,  # renew Metal residency during idle; 0 disables
    # Grouping segments into a "thought" that gets triaged as one unit
    "thought_gap_s": 90.0,  # silence that closes a thought
    "thought_max_s": 900.0,  # force-close long rambles
    # Triage agent
    "triage_backend": "codex",  # "codex" (codex exec) or "claude" (claude -p)
    "triage_model": "gpt-6-astra",  # codex: OpenAI model id; claude: e.g. "sonnet"; None = CLI default
    "triage_effort": "high",  # codex reasoning effort
    "triage_cwd": "/path/to/projects",
    "triage_timeout_s": 900,
    "triage_jev_enabled": True,  # skip full triage only for confidently ambient/personal thoughts
    "triage_jev_min_probability": 0.95,
    "triage_jev_min_confidence": 0.8,
    "triage_jev_max_review": 0.1,  # any possible useful/uncertain content keeps full triage
    # Dispatch
    "dispatch_client": "codex",  # agent-coord client for dispatched threads (OpenAI credits)
    "dispatch_daily_cap": None,  # unlimited by default; positive integer = daily max, 0 = no dispatch
    # Persistent work uses the existing dispatcher permission policy.
    "work_sessions_enabled": True,
    "work_session_root": "/path/to/projects",
    "work_session_yolo": True,
    "work_session_model": None,
    "work_session_effort": None,
    "work_progress_interval_s": 30,
    # Notifications
    "notify": True,
    "ntfy_topic": None,  # optional https://ntfy.sh/<topic> for phone pushes
    # Fast lane: spoken answers when addressed by name ("Ramble, ...")
    "fast_enabled": True,
    "early_wake_enabled": True,  # Unhinted opening previews only acknowledge; full speech still routes.
    "fast_model": "gpt-6.1-sol",
    "fast_effort": "low",
    "fast_timeout_s": None,  # auto: 30–120s by effort, ultra 900s; number overrides hard bound
    "fast_wait_notice_s": 12,  # one progress notice; never opens a follow-up window
    "fast_failure_cooldown_s": 30,  # do not hammer an unavailable responder with queued requests
    "followup_s": 20.0,  # after Ramble speaks, the next utterance counts as addressed
    "work_followup_s": 3600.0,  # foreground conversation idle timeout; 0 disables direct ownership
    "chat_followup_s": 600.0,  # Jev checks late quick-chat replies; bounded to 10 min; 0 disables
    "chat_followup_threshold": 0.8,  # both directed-to-Ramble and related scores must pass
    "jev_model": "jev-1.13.0",  # shared by continuation admission and background triage
    "jev_timeout_s": 3.0,  # chat failures stay passive; background failures use full triage
    "jev_api_key_file": "/path/to/projects/secrets/typesafe_jev",  # shared local key; env keys take precedence
    "fast_name_window_s": 8.0,  # after a bare "Ramble?", the next utterance is the request
    "fast_coalesce_s": 1.2,  # silence window; includes time already spent transcribing
    # Spoken replies (explicit only; no automatic dispatch acknowledgements)
    "tts_provider": "kokoro",  # kokoro or elevenlabs; both fall back to macOS say
    "tts_url": "http://127.0.0.1:8880/v1/audio/speech",
    "tts_model": "kokoro",
    "tts_voice": "af_heart",
    "tts_speed": 1.0,
    "tts_timeout_s": 30,
    "tts_fallback_voice": None,  # macOS default voice, or e.g. Samantha
    "tts_guard_tail_s": 1.0,  # suppress room echo after playback ends
    # Keep the credential outside config/repo; alternatively set ELEVENLABS_API_KEY.
    "elevenlabs_api_key_file": None,
    "elevenlabs_model": "eleven_flash_v2_5",
    "elevenlabs_voice_id": "CwhRBWXzGAHq8TQ4Fs17",  # Roger
    "elevenlabs_stability": 0.75,
    "elevenlabs_similarity_boost": 0.75,
    "elevenlabs_style": 0.0,
    "elevenlabs_use_speaker_boost": True,
}


def ensure_dirs() -> None:
    for d in (HOME, AUDIO_DIR, DIGEST_DIR, LOG_DIR, TRIAGE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg
