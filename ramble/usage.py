"""Durable, counted-once token attribution. No prompts, audio, or credentials.

Provider counters are evidence, not invoices. Missing counters remain unknown.
Linked Agent Coord conversations retain their own lifetime scope and never get
added to Ramble's dated per-call totals.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

from . import config

log = logging.getLogger("ramble.usage")
COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens", "reasoning_output_tokens")
PURPOSES = {"reply": "Quick replies", "followup": "Follow-up checks", "triage": "Thought review",
            "chat_followup": "Conversation checks", "triage_filter": "Background filtering"}


def normalize(raw, provider="codex"):
    if not isinstance(raw, dict):
        return {}
    aliases = {"prompt_tokens": "input_tokens", "completion_tokens": "output_tokens",
               "cache_read_input_tokens": "cached_input_tokens"}
    result = {aliases.get(k, k): v for k, v in raw.items()
              if aliases.get(k, k) in (*COUNTERS, "cache_creation_input_tokens")
              and type(v) is int and 0 <= v <= 2**63 - 1}
    if provider == "claude":
        if all(k in result for k in ("input_tokens", "cached_input_tokens", "cache_creation_input_tokens")):
            result["input_tokens"] += result["cached_input_tokens"] + result["cache_creation_input_tokens"]
        else:
            result.pop("input_tokens", None)
    result.pop("cache_creation_input_tokens", None)
    if all(k in result for k in ("input_tokens", "output_tokens")):
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def codex_counts(stdout, stderr=""):
    latest = {}
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            latest = normalize(event.get("usage"))
    if not latest:
        # Older triage audits retained Codex's plain-text total only.
        matches = re.findall(r"(?m)^tokens used\s*\n([\d,]+)\s*$", stderr or "")
        if matches:
            latest = {"total_tokens": int(matches[-1].replace(",", ""))}
    return latest


class UsageStore:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else config.HOME
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "usage.sqlite3"
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, started_at REAL NOT NULL, purpose TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT, request_id TEXT, outcome TEXT NOT NULL,
                    source TEXT NOT NULL, counts TEXT NOT NULL, coverage TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS calls_time ON calls(started_at);
                CREATE TABLE IF NOT EXISTS imports (path TEXT PRIMARY KEY, stamp TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS linked (thread_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    snapshot TEXT NOT NULL, checked_at REAL NOT NULL, error TEXT);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=.5)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def record(self, call_id, *, started_at, purpose, provider, model=None, request_id=None,
               outcome="completed", source="live", counts=None, coverage=None):
        if not isinstance(call_id, str) or not call_id or type(started_at) not in (int, float) or not math.isfinite(started_at) or started_at < 0:
            raise ValueError("Invalid call identity or timestamp")
        counts = normalize(counts)
        coverage = coverage or ("reported" if "total_tokens" in counts and outcome == "completed"
                                else "partial" if counts else "unknown")
        with self.connect() as conn:
            conn.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                         "outcome=excluded.outcome,counts=excluded.counts,coverage=excluded.coverage",
                         (call_id, started_at, purpose, provider, model, request_id, outcome, source,
                          json.dumps(counts), coverage))

    def import_fast(self, path):
        events = []
        for line in bounded_read(path, 2 * 1024 * 1024).splitlines():
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
            except ValueError:
                continue  # A live trace may end with an incomplete line.
        start = next((e for e in events if e.get("stage") == "call_started"), None)
        if not start or not start.get("call_id"):
            return
        end = next((e for e in reversed(events) if e.get("stage") == "call_finished"), {})
        counts = next((normalize(e["usage"]) for e in reversed(events)
                       if e.get("event") == "turn.completed" and isinstance(e.get("usage"), dict)), {})
        self.record("fast:" + start["call_id"], started_at=start["wall_s"],
                    purpose=start.get("purpose", "reply"), provider="codex", model=start.get("model"),
                    request_id=start.get("request_id"), outcome=end.get("outcome", "interrupted"),
                    source="Fast-call trace", counts=counts)

    def import_audit(self, path):
        audit = json.loads(bounded_read(path, 4 * 1024 * 1024))
        if not isinstance(audit, dict):
            raise ValueError("Invalid triage audit")
        provider = audit.get("backend")
        if provider not in {"codex", "claude"}:
            return
        counts = {}
        if provider == "codex":
            counts = codex_counts(audit.get("stdout"), audit.get("stderr"))
        else:
            try:
                counts = normalize(json.loads(audit.get("stdout", "{}")).get("usage"), "claude")
            except (ValueError, AttributeError):
                pass
        model = audit.get("model")
        cmd = audit.get("cmd") or []
        if not model and "-m" in cmd:
            model = cmd[cmd.index("-m") + 1]
        legacy = not audit.get("usage_id")
        self.record("triage:" + (audit.get("usage_id") or path.stem),
                    started_at=audit.get("usage_started_at", path.stat().st_mtime),
                    purpose="triage", provider=provider, model=model, request_id=path.stem,
                    outcome=audit.get("status", "completed" if audit.get("returncode") == 0 else "failed"),
                    source="Recovered triage audit" if legacy else "Triage audit", counts=counts,
                    coverage="partial" if legacy and counts else None)

    def recover(self):
        """Import retained evidence once per file version, with bounded work."""
        candidates = [*(self.home / "triage/fast").glob("*.jsonl"), *(self.home / "triage").glob("*.json")]
        dated = []
        for path in candidates:
            try:
                dated.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue  # Trace retention can rotate files during a refresh.
        paths = [p for _, p in sorted(dated, reverse=True)]
        skipped = max(0, len(paths) - 2000)
        budget = 64 * 1024 * 1024
        for path in paths[:2000]:
            if path.name.endswith(".jev.json"):
                continue
            try:
                stat = path.stat()
                stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
                with self.connect() as conn:
                    old = conn.execute("SELECT stamp FROM imports WHERE path=?", (str(path),)).fetchone()
                if old and old[0] == stamp:
                    continue
                if stat.st_size > budget:
                    skipped += 1
                    continue
                budget -= stat.st_size
                (self.import_fast if path.suffix == ".jsonl" else self.import_audit)(path)
                with self.connect() as conn:
                    conn.execute("INSERT OR REPLACE INTO imports VALUES (?,?)", (str(path), stamp))
            except (OSError, ValueError, KeyError, TypeError, IndexError):
                skipped += 1
        return skipped

    def report(self, days=1):
        now = time.time()
        midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff = 0 if days is None else (midnight - timedelta(days=days - 1)).timestamp()
        with self.connect() as conn:
            rows = [dict(r) for r in conn.execute("SELECT * FROM calls WHERE started_at>=? ORDER BY started_at DESC", (cutoff,))]
            linked = [dict(r) for r in conn.execute("SELECT * FROM linked ORDER BY name")]
        for row in rows:
            row["counts"] = json.loads(row["counts"])
        groups = {}
        for row in rows:
            key = (row["purpose"], row["provider"], row["model"])
            groups.setdefault(key, []).append(row)
        return {"calls": rows[:200], "call_count": len(rows), "totals": aggregate(rows),
                "groups": [{"purpose": key[0], "provider": key[1], "model": key[2], **aggregate(items)}
                           for key, items in sorted(groups.items(), key=lambda pair: str(pair[0]))],
                "linked": [{**row, "snapshot": json.loads(row["snapshot"])} for row in linked],
                "updated_at": now}

    def refresh_linked(self, fetch=None):
        fetch = fetch or fetch_thread_usage
        threads = linked_threads(self.home)
        with self.connect() as conn:
            cached = {r["thread_id"]: dict(r) for r in conn.execute("SELECT * FROM linked")}
        due = [t for t in threads if t["thread_id"] not in cached or
               time.time() - cached[t["thread_id"]]["checked_at"] >= 30]
        def read(thread):
            try:
                value = fetch(thread["thread_id"])
                if not isinstance(value, dict) or not isinstance(value.get("totals"), dict):
                    raise ValueError("Invalid usage snapshot")
                # Keep provider scope/coverage and only normalized counters/metadata.
                snapshot = {key: value.get(key) for key in ("provider", "status", "scope", "observed_at", "note")}
                snapshot["totals"] = normalize(value["totals"])
                snapshot["models"] = [{"model": m.get("model"), "totals": normalize(m.get("totals"))}
                                      for m in value.get("models", []) if isinstance(m, dict)]
                return thread, snapshot, None
            except Exception:
                previous = cached.get(thread["thread_id"])
                return thread, json.loads(previous["snapshot"]) if previous else {}, "Usage refresh unavailable"
        with ThreadPoolExecutor(max_workers=4) as pool:
            for thread, snapshot, error in pool.map(read, due):
                with self.connect() as conn:
                    conn.execute("INSERT OR REPLACE INTO linked VALUES (?,?,?,?,?)",
                                 (thread["thread_id"], thread["name"], json.dumps(snapshot), time.time(), error))


def aggregate(rows):
    counts = {key: sum(r["counts"].get(key, 0) for r in rows) for key in COUNTERS
              if any(key in r["counts"] for r in rows)}
    missing = {key: sum(key not in r["counts"] for r in rows) for key in COUNTERS}
    return {"counts": counts, "missing": missing, "calls": len(rows),
            "measured": sum("total_tokens" in r["counts"] for r in rows)}


def bounded_read(path, limit):
    if path.is_symlink():
        raise ValueError("Usage import does not follow symlinks")
    with path.open("rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Usage import exceeds size limit")
    return data.decode("utf-8")


def linked_threads(home):
    path = home / "ramble.db"
    if not path.exists():
        return []
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        found = {}
        for table in ("work_sessions", "dispatches"):
            if table not in tables:
                continue
            for tid, name, created in conn.execute(f"SELECT thread_id,name,created_at FROM {table} WHERE thread_id IS NOT NULL ORDER BY created_at DESC LIMIT 100"):
                if tid not in found or created > found[tid]["created_at"]:
                    found[tid] = {"thread_id": tid, "name": name or tid, "created_at": created}
        return sorted(found.values(), key=lambda row: row["created_at"], reverse=True)[:100]
    finally:
        conn.close()


def fetch_thread_usage(thread_id):
    launcher = Path.home() / ".local/bin/agent-coord"
    cli = os.environ.get("AGENT_COORD_CLI") or (str(launcher) if launcher.is_file() else shutil.which("agent-coord"))
    if not cli:
        raise OSError("Agent Coord unavailable")
    result = subprocess.run([cli, "thread", "usage", "--session-id", thread_id],
                            capture_output=True, text=True, timeout=8, check=True)
    return json.loads(result.stdout)


def capture_fast(path, home=None):
    try:
        UsageStore(home).import_fast(Path(path))
    except Exception:
        log.warning("Could not record fast-call token usage", exc_info=True)


def capture_audit(path, home=None):
    try:
        UsageStore(home).import_audit(Path(path))
    except Exception:
        log.warning("Could not record triage token usage", exc_info=True)


def record_call(call_id, **fields):
    try:
        UsageStore().record(call_id, **fields)
    except Exception:
        log.warning("Could not record classifier token usage", exc_info=True)
