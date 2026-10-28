"""SQLite store. One connection per thread; WAL so the CLI can read while the app writes."""

from __future__ import annotations

import sqlite3
import time
import uuid

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    duration REAL NOT NULL,
    audio_path TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'mic',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | transcribed | empty | error
    text TEXT,
    language TEXT,
    no_speech_prob REAL,
    avg_logprob REAL,
    thought_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS segments_status ON segments(status, started_at);
CREATE INDEX IF NOT EXISTS segments_thought ON segments(thought_id);

CREATE TABLE IF NOT EXISTS thoughts (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',      -- open | closed | triaging | done | error
    text TEXT NOT NULL DEFAULT '',
    classification TEXT,
    summary TEXT,
    triage_json TEXT,
    error TEXT,
    triaged_at REAL
);
CREATE INDEX IF NOT EXISTS thoughts_status ON thoughts(status, started_at);

CREATE TABLE IF NOT EXISTS dispatches (
    id TEXT PRIMARY KEY,
    thought_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    name TEXT NOT NULL,
    cwd TEXT NOT NULL,
    prompt TEXT NOT NULL,
    thread_id TEXT,
    status TEXT NOT NULL,                     -- created | skipped_cap | error
    detail TEXT
);

CREATE TABLE IF NOT EXISTS session_claims (
 segment_id TEXT PRIMARY KEY, session_id TEXT, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS session_launches (id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS session_input_batches (
 id TEXT PRIMARY KEY, segment_ids TEXT NOT NULL, session_id TEXT,
 status TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS speech_origins (playback_id TEXT PRIMARY KEY, origin TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS action_requests (
 id TEXT PRIMARY KEY, session_id TEXT, epoch INTEGER, items TEXT NOT NULL,
 text TEXT NOT NULL, family TEXT NOT NULL, operation TEXT NOT NULL,
 status TEXT NOT NULL, reply TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_requests_status ON action_requests(status,created_at);

CREATE TABLE IF NOT EXISTS speech_playbacks (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL NOT NULL,
    audio_path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS speech_playbacks_end ON speech_playbacks(ended_at);
"""


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
