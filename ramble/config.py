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
    "language": None,  # None = auto-detect; "en" to force English
    # Grouping segments into a "thought" that gets triaged as one unit
    "thought_gap_s": 90.0,  # silence that closes a thought
    "thought_max_s": 900.0,  # force-close long rambles
    # Triage agent
    "triage_backend": "codex",  # "codex" (codex exec) or "claude" (claude -p)
    "triage_model": "gpt-6-astra",  # codex: OpenAI model id; claude: e.g. "sonnet"; None = CLI default
    "triage_effort": "high",  # codex reasoning effort
    "triage_cwd": "/path/to/projects",
    "triage_timeout_s": 900,
    # Dispatch
    "dispatch_client": "codex",  # agent-coord client for dispatched threads (OpenAI credits)
    "dispatch_daily_cap": 6,  # max agent threads spawned per day
    # Notifications
    "notify": True,
    "ntfy_topic": None,  # optional https://ntfy.sh/<topic> for phone pushes
}


def ensure_dirs() -> None:
    for d in (HOME, AUDIO_DIR, DIGEST_DIR, LOG_DIR, TRIAGE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    return cfg
