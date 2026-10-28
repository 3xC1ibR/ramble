"""Durable interactive tasks, command outbox and public display events."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from . import config, db, action_routing

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_sessions (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, goal TEXT NOT NULL, cwd TEXT NOT NULL,
 kind TEXT NOT NULL, thread_id TEXT, status TEXT NOT NULL, foreground INTEGER NOT NULL DEFAULT 1,
 active_turn_id TEXT, last_turn_id TEXT, epoch INTEGER NOT NULL DEFAULT 0,
 cursor INTEGER NOT NULL DEFAULT 0, connected INTEGER NOT NULL DEFAULT 0,
 error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 speech_epoch INTEGER NOT NULL DEFAULT 0, quiet INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_commands (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', epoch INTEGER NOT NULL, created_at REAL NOT NULL,
 result TEXT, error TEXT, native_payload TEXT
);
CREATE TABLE IF NOT EXISTS session_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_key TEXT NOT NULL,
 kind TEXT NOT NULL, text TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(session_id,event_key)
);
CREATE TABLE IF NOT EXISTS session_inputs (
 segment_id TEXT PRIMARY KEY, item TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT
);
CREATE TABLE IF NOT EXISTS session_publications (
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL, event_key TEXT NOT NULL,
 status TEXT NOT NULL, revision TEXT, error TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session_speech (
 id TEXT PRIMARY KEY, session_id TEXT, text TEXT NOT NULL, kind TEXT NOT NULL,
 priority INTEGER NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 created_at REAL NOT NULL, expires_at REAL NOT NULL, error TEXT,
 thought_id TEXT
);
CREATE TABLE IF NOT EXISTS session_suppressed_turns (session_id TEXT NOT NULL, turn_id TEXT NOT NULL, PRIMARY KEY(session_id,turn_id));
CREATE TABLE IF NOT EXISTS session_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def key(prefix, value):
    return prefix + "-" + hashlib.sha256(value.encode()).hexdigest()[:32]


class SessionStore:
    def __init__(self, home=None):
        self.home = Path(home) if home is not None else config.HOME
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "ramble.db"
        with self.connect() as conn:
            conn.executescript(db.SCHEMA + SCHEMA)
            if "native_payload" not in {
                r["name"] for r in conn.execute("PRAGMA table_info(session_commands)")
            }:
                conn.execute(
                    "ALTER TABLE session_commands ADD COLUMN native_payload TEXT"
                )
            if "error" not in {
                r["name"] for r in conn.execute("PRAGMA table_info(session_inputs)")
            }:
                conn.execute("ALTER TABLE session_inputs ADD COLUMN error TEXT")
            if "finished_at" not in {
                r["name"] for r in conn.execute("PRAGMA table_info(session_speech)")
            }:
                conn.execute("ALTER TABLE session_speech ADD COLUMN finished_at REAL")

    @contextmanager
    def connect(self, transaction=False):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            if transaction:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def event(self, conn, sid, event_key, kind, text="", payload=None):
        cursor = conn.execute(
            "INSERT OR IGNORE INTO session_events(session_id,event_key,kind,text,payload,created_at) VALUES (?,?,?,?,?,?)",
            (sid, event_key, kind, text, json.dumps(payload or {}), time.time()),
        )
        return cursor.rowcount == 1

    def sessions(self):
        with self.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM work_sessions ORDER BY foreground DESC,updated_at DESC LIMIT 20"
                )
            ]

    def get(self, sid):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE id=?", (sid,)
            ).fetchone()
        if not row:
            raise ValueError(
                "That work session is unavailable. Refresh the session list."
            )
        return dict(row)

    def context(self):
        rows = self.sessions()
        result = [
            {
                k: row[k]
                for k in (
                    "id",
                    "name",
                    "goal",
                    "cwd",
                    "kind",
                    "thread_id",
                    "status",
                    "foreground",
                )
            }
            for row in rows
        ]
        known = {r["thread_id"] for r in rows}
        with self.connect() as conn:
            for session in result:
                recent = conn.execute(
                    "SELECT text,payload,created_at FROM session_events "
                    "WHERE session_id=? AND kind='message' ORDER BY seq DESC LIMIT 1",
                    (session["id"],),
                ).fetchone()
                if recent:
                    session["latest_message"] = recent["text"][:2000]
                    session["latest_message_at"] = recent["created_at"]
                final = conn.execute(
                    "SELECT 1 FROM session_events WHERE session_id=? AND kind='message' "
                    "AND json_extract(payload,'$.phase')='final_answer' LIMIT 1",
                    (session["id"],),
                ).fetchone()
                session["has_saved_result"] = bool(final)
            for r in conn.execute(
                "SELECT * FROM dispatches WHERE status='created' AND thread_id IS NOT NULL ORDER BY created_at DESC LIMIT 10"
            ):
                if r["thread_id"] not in known:
                    result.append(
                        {
                            "id": "thread:" + r["thread_id"],
                            "name": r["name"],
                            "cwd": r["cwd"],
                            "kind": "existing",
                            "thread_id": r["thread_id"],
                            "foreground": 0,
                        }
                    )
        return result

    def snapshot(self, sid=None, after=0):
        if type(after) is not int or after < 0:
            raise ValueError("Invalid event cursor.")
        rows = self.sessions()
        selected = (
            self.get(sid)
            if sid
            else next((r for r in rows if r["foreground"]), rows[0] if rows else None)
        )
        with self.connect() as conn:
            events = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM session_events WHERE session_id=? AND seq>? ORDER BY seq LIMIT 100",
                    (selected["id"] if selected else "", after),
                )
            ]
            heartbeat = conn.execute(
                "SELECT value FROM session_meta WHERE key='heartbeat'"
            ).fetchone()
            commands = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,action,status,error FROM session_commands WHERE session_id=? ORDER BY created_at DESC LIMIT 10",
                    (selected["id"] if selected else "",),
                )
            ]
            speech = [
                dict(r)
                for r in conn.execute(
                    "SELECT id,kind,status,error FROM session_speech WHERE session_id=? ORDER BY created_at DESC LIMIT 5",
                    (selected["id"] if selected else "",),
                )
            ]
            action = action_routing.current(conn, sid)
            if action is None and selected and action_routing.intent(selected['goal']):
                action = {'status': 'unconfirmed', 'text': selected['goal'],
                          'reply': 'No execution receipt is recorded for this request. An idle conversation does not confirm completion.'}
        return {
            "sessions": rows,
            "session": selected,
            "events": events,
            "cursor": events[-1]["seq"] if events else after,
            "has_more": len(events) == 100,
            "controller_online": bool(
                heartbeat and time.time() - float(heartbeat[0]) < 15
            ),
            "commands": commands,
            "speech": speech,
            "action": action,
        }

    def control(self, sid, action, request_id, *, message=None, conn=None):
        if action not in {
            "stop_work",
            "stop_talking",
            "resume_talking",
            "message",
            "focus",
        }:
            raise ValueError("Unknown work-session action.")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 120:
            raise ValueError("Supply a stable request ID.")
        if action == "message" and (
            not isinstance(message, str) or not message.strip() or len(message) > 20000
        ):
            raise ValueError("Supply a message of 1–20000 characters.")
        payload = json.dumps({"message": message}, sort_keys=True)
        command_id = key("companion", request_id)
        with (self.connect(True) if conn is None else nullcontext(conn)) as conn:
            prior = conn.execute(
                "SELECT * FROM session_commands WHERE id=?", (command_id,)
            ).fetchone()
            if prior:
                if (
                    prior["session_id"] != sid
                    or prior["action"] != action
                    or prior["payload"] != payload
                ):
                    raise ValueError(
                        "This request ID already belongs to another command."
                    )
                return dict(prior)
            row = conn.execute(
                "SELECT * FROM work_sessions WHERE id=?", (sid,)
            ).fetchone()
            if not row:
                raise ValueError("Unknown work session.")
            epoch = row["epoch"]
            if action in {"stop_work", "stop_talking"}:
                conn.execute(
                    "UPDATE work_sessions SET quiet=1,speech_epoch=speech_epoch+1 WHERE id=?",
                    (sid,),
                )
                conn.execute(
                    "UPDATE session_speech SET status='cancelled' WHERE session_id=? AND status='pending'",
                    (sid,),
                )
            if action == "stop_work":
                epoch += 1
                conn.execute(
                    "UPDATE work_sessions SET epoch=?,status='stopping' WHERE id=?",
                    (epoch, sid),
                )
                # A submitted native request is reconciled/cancelled by the owner.
                conn.execute(
                    "UPDATE session_commands SET status='cancelled' WHERE session_id=? AND status='pending'",
                    (sid,),
                )
                conn.execute(
                    "UPDATE session_publications SET status='cancelled' WHERE session_id=? AND status='pending'",
                    (sid,),
                )
                conn.execute("UPDATE action_requests SET status='cancelled',reply='Action cancelled before execution.',updated_at=? "
                             "WHERE session_id=? AND status='pending'", (time.time(), sid))
            elif action == "message":
                conn.execute(
                    "UPDATE work_sessions SET quiet=0,status=CASE WHEN status='cancelled' THEN 'queued' ELSE status END WHERE id=?",
                    (sid,),
                )
            elif action == "resume_talking":
                conn.execute("UPDATE work_sessions SET quiet=0 WHERE id=?", (sid,))
            elif action == "focus":
                conn.execute("UPDATE work_sessions SET foreground=0")
                conn.execute("UPDATE work_sessions SET foreground=1 WHERE id=?", (sid,))
            status = "pending" if action in {"stop_work", "message"} else "completed"
            conn.execute(
                "INSERT INTO session_commands(id,session_id,action,payload,status,epoch,created_at) VALUES (?,?,?,?,?,?,?)",
                (command_id, sid, action, payload, status, epoch, time.time()),
            )
            self.event(
                conn,
                sid,
                command_id,
                "user" if action == "message" else "control",
                message if action == "message" else action.replace("_", " "),
            )
        return {"id": command_id, "status": status}
