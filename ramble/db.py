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
