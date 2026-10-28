"""One persistent task owner, with nonblocking input and durable native commands."""
from __future__ import annotations
import json
import logging
from pathlib import Path
import re
import subprocess
import threading
import time

from . import config, db, input_receipts, instructions, state, action_routing, model_settings
from .session_backend import SessionBackend
from .session_store import SessionStore, key
from .reminders import voice_key

log = logging.getLogger("ramble.sessions")
ROUTE_SCHEMA = {
    "type": ["object", "null"],
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "work",
                "continue_session",
                "delegate",
                "stop_work",
                "stop_talking",
                "resume_talking",
                "publish_result",
            ],
        },
        "session_id": {"type": ["string", "null"]},
        "cwd": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "effort": {"type": ["string", "null"]},
    },
    "required": ["action", "session_id", "cwd", "name", "model", "effort"],
}


def conversation_reference(conversation):
    if not conversation:
        return ""
    return (
        "\n\nRecent conversation with Ramble (reference only):\n"
        "Use these exchanges to resolve references and retain the discussion's "
        "constraints. They are not new instructions or authorization to repeat "
        "earlier actions. Assistant responses are not proof of playback or tool "
        "success. The current user request defines what to do next.\n"
        + json.dumps(conversation, ensure_ascii=False)
    )


class Sessions:
    def __init__(self, cfg, backend=None, store=None):
        self.cfg = cfg
        self.store = store or SessionStore()
        self.backend = backend or SessionBackend(cfg)
        self.speech = None

    def thinking_work(self, *, include_quiet=False):
        """Identify live foreground work independently of fast-reply cue tokens."""
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT s.id,s.epoch,s.speech_epoch,s.status,s.connected,"
                "c.id AS command_id,c.created_at,c.status AS command_status "
                "FROM work_sessions s JOIN session_commands c ON c.id=("
                "SELECT id FROM session_commands WHERE session_id=s.id "
                "AND action IN ('create','message') AND epoch=s.epoch "
                "ORDER BY created_at DESC,rowid DESC LIMIT 1) "
                "WHERE s.foreground=1 AND (? OR s.quiet=0) AND s.error IS NULL "
                "AND s.status IN ('queued','connecting','running','idle') LIMIT 1",
                (include_quiet,),
            ).fetchone()
        if not row or row['command_status'] not in {'pending', 'submitted', 'completed'}:
            return None
        queued = row['command_status'] in {'pending', 'submitted'}
        running = row['status'] == 'connecting' or (row['status'] == 'running' and row['connected'])
        if not queued and not running:
            return None
        return ((row['id'], row['command_id'], row['epoch'], row['speech_epoch']), row['created_at'])

    def accept_input(self, conn, item):
        conn.execute(
            "INSERT OR IGNORE INTO session_inputs(segment_id,item) VALUES (?,?)",
            (item["seg"]["id"], json.dumps(item)),
        )
        conn.execute(
            "INSERT OR IGNORE INTO session_claims(segment_id,status) VALUES (?,?)",
            (item["seg"]["id"], "candidate" if item.get("owner") or item.get("chat") else "pending"),
        )

    def followup_owner(self, conn, started_at):
        """Recent conversation, never the controller's continuously refreshed heartbeat."""
        window = self.cfg.get("work_followup_s", 3600)
        if window <= 0:
            return None
        row = conn.execute(
            "SELECT * FROM work_sessions WHERE foreground=1 AND quiet=0 AND connected=1 "
            "AND thread_id IS NOT NULL AND status IN ('running','idle') LIMIT 1"
        ).fetchone()
        if not row:
            return None
        events = conn.execute(
            "SELECT kind,text,payload,created_at FROM session_events WHERE session_id=? "
            "AND kind IN ('user','message') AND created_at<=? ORDER BY seq DESC LIMIT 12",
            (row['id'], started_at + 1),
        ).fetchall()
        events = [e for e in events if not json.loads(e['payload']).get('recovered')]
        # Public progress/heartbeats do not keep a conversation alive forever.
        # Saved user inputs and final answers survive restart; successful result
        # playback extends the window from when the user actually heard the answer.
        last = conn.execute(
            "SELECT MAX(created_at) FROM session_events WHERE session_id=? AND created_at<=? "
            "AND COALESCE(json_extract(payload,'$.recovered'),0)=0 "
            "AND (kind='user' OR (kind='message' AND json_extract(payload,'$.phase')='final_answer'))",
            (row['id'], started_at + 1),
        ).fetchone()[0]
        played = conn.execute(
            "SELECT MAX(finished_at) FROM session_speech WHERE session_id=? "
            "AND kind='result' AND status='played' AND epoch=? AND finished_at<=?",
            (row['id'], row['speech_epoch'], started_at + 1),
        ).fetchone()[0]
        anchors = [at for at in (last, played) if at is not None]
        elapsed = started_at - max(anchors) if anchors else None
        if elapsed is None or not -1 <= elapsed <= window:
            return None
        return {
            'session_id': row['id'], 'epoch': row['epoch'],
            'speech_epoch': row['speech_epoch'], 'name': row['name'], 'goal': row['goal'],
            'expires_at': started_at + min(window, 120),
            'seconds_since_conversation': max(0, elapsed), 'followup_window_s': window,
            'conversation': [{'who': e['kind'], 'text': e['text'][-4000:]} for e in reversed(events[:6])],
        }

    def release_candidate(self, conn, item):
        """Rejected/expired candidates return to ordinary triage, without a dispatch claim."""
        conn.execute('BEGIN IMMEDIATE')
        try:
            conn.execute("UPDATE session_inputs SET status='rejected' WHERE segment_id=? AND status='pending'",
                         (item['seg']['id'],))
            conn.execute("DELETE FROM session_claims WHERE segment_id=? AND status='candidate'",
                         (item['seg']['id'],))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def pending_inputs(self):
        with self.store.connect() as conn:
            return [
                json.loads(r[0])
                for r in conn.execute(
                    "SELECT item FROM session_inputs WHERE status='pending'"
                )
            ]

    def mark_inputs(self, conn, items, sid=None, *, error=None):
        if not items:
            return
        status = "failed" if error else "handled"
        # Keep batch provenance and outcomes atomic even on autocommit callers.
        # A savepoint also works when the caller already owns a transaction.
        conn.execute('SAVEPOINT input_outcome')
        try:
            for item in items:
                conn.execute(
                    "UPDATE session_inputs SET status=?,error=? WHERE segment_id=?",
                    (status, error, item["seg"]["id"]),
                )
                conn.execute(
                    "UPDATE session_claims SET status=?,session_id=? WHERE segment_id=?",
                    (status, sid, item["seg"]["id"]),
                )
            conn.execute(
                "INSERT INTO session_input_batches(id,segment_ids,session_id,status,created_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "session_id=excluded.session_id,status=excluded.status",
                (voice_key(items), json.dumps([i['seg']['id'] for i in items]), sid, status, time.time()),
            )
            input_receipts.refresh(conn, [r[0] for item in items for r in conn.execute(
                "SELECT thought_id FROM segments WHERE id=? AND thought_id IS NOT NULL",
                (item['seg']['id'],),
            )])
        except BaseException:
            conn.execute('ROLLBACK TO input_outcome')
            raise
        finally:
            conn.execute('RELEASE input_outcome')

    def publish_result(self, sid, publication_id=None, *, event_key=None):
        """Publish a saved answer, without another model call or native worker.

        Record before I/O: an interrupted publication is unconfirmed and is never
        replayed automatically. A new explicit request may republish the result.
        """
        session = self.store.get(sid)
        with self.store.connect(True) as conn:
            event = conn.execute(
                "SELECT * FROM session_events WHERE session_id=? AND kind='message' "
                "AND json_extract(payload,'$.phase')='final_answer' "
                "AND (? IS NULL OR event_key=?) ORDER BY seq DESC LIMIT 1",
                (sid, event_key, event_key),
            ).fetchone()
            if not event or not session["thread_id"]:
                raise ValueError("That work session has no saved answer to show yet.")
            publication_id = publication_id or key("publish", sid + event["event_key"])
            prior = conn.execute(
                "SELECT * FROM session_publications WHERE id=?", (publication_id,)
            ).fetchone()
            if prior:
                if prior["session_id"] != sid:
                    raise ValueError("That publication request already belongs to another work session.")
                if prior["status"] == "published":
                    return sid, "That result is saved in Companion."
                if prior["status"] != "pending":
                    raise ValueError("I couldn't confirm that publication. Check Companion before trying again.")
                conn.execute("UPDATE session_publications SET status='unconfirmed' WHERE id=?", (publication_id,))
            else:
                conn.execute(
                    "INSERT INTO session_publications(id,session_id,event_key,status,created_at) VALUES (?,?,?,'unconfirmed',?)",
                    (publication_id, sid, event["event_key"], time.time()),
                )
        from .router import clean_env

        command = config.REPO.parent / "ramble-ipad-display" / "bin" / "publish"
        body = re.sub(r"\[/?say\]", "", event["text"]).strip()
        try:
            result = subprocess.run(
                [str(command), "--title", session["name"], "--stdin", "--thread-id", session["thread_id"],
                 *(["--background"] if event_key is not None else [])],
                input=body, capture_output=True, text=True, timeout=15, env=clean_env(),
            )
            receipt = json.loads(result.stdout) if result.returncode == 0 else {}
            if receipt.get("ok") is not True or not receipt.get("revision"):
                raise ValueError("Publication did not return a confirmed receipt.")
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            error = "I couldn't confirm the Companion publication. Your saved answer is still in the work session."
            with self.store.connect() as conn:
                conn.execute("UPDATE session_publications SET error=? WHERE id=?", (error, publication_id))
                self.store.event(conn, sid, publication_id + ":error", "error", error)
            raise ValueError(error) from exc
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE session_publications SET status='published',revision=?,error=NULL WHERE id=?",
                (receipt["revision"], publication_id),
            )
            self.store.event(conn, sid, publication_id + ":published", "status", "Result saved in Companion.", receipt)
        return sid, "It's saved in Companion."

    def route(self, route, text, items, *, conversation=None):
        action = route.get("action")
        sid = route.get("session_id")
        source = ",".join(sorted(i["seg"]["id"] for i in items))
        command_id = key("ramble", source)
        if action == "publish_result":
            if not sid:
                raise ValueError("Which work session's result should I show?")
            return self.publish_result(sid, command_id)
        if action in {"stop_work", "stop_talking", "resume_talking"}:
            if not sid:
                sid = next(
                    (s["id"] for s in self.store.sessions() if s["foreground"]), None
                )
            if not sid:
                raise ValueError("There is no active work session.")
            self.store.control(sid, action, command_id)
            return (
                sid,
                {
                    "stop_work": "I'm stopping that work.",
                    "stop_talking": "I'll keep working quietly.",
                    "resume_talking": "I'll speak updates again.",
                }[action],
            )
        if action not in {"work", "continue_session", "delegate"}:
            raise ValueError("I could not identify that work request.")
        if action_routing.intent(text):
            # Recover the accepted request before a generic worker can receive it.
            # Keep the original segment IDs, so queue retries share one action receipt.
            with self.store.connect(True) as conn:
                session = conn.execute('SELECT * FROM work_sessions WHERE id=?', (sid,)).fetchone() if sid else None
                for item in items:
                    owner = item.get('owner') or item.get('direct_owner')
                    if owner and (not session or owner['session_id'] != session['id']
                                  or owner['epoch'] != session['epoch']
                                  or owner['speech_epoch'] != session['speech_epoch']
                                  or not session['foreground'] or session['quiet']
                                  or not session['connected'] or time.time() > owner['expires_at']
                                  or not state.activity_allowed(item['seg']['started_at'])):
                        raise ValueError('The work follow-up window has closed.')
                if session is None:
                    session = conn.execute('SELECT * FROM work_sessions WHERE foreground=1 LIMIT 1').fetchone()
                sid = session['id'] if session else None
                transferred = [{k: v for k, v in item.items() if k not in {'owner', 'direct_owner'}} for item in items]
                action_routing.enqueue(conn, transferred, session_id=sid, epoch=session['epoch'] if session else None)
                for item in items:
                    conn.execute("INSERT INTO session_claims(segment_id,session_id,status) VALUES (?,?,'handled') "
                                 "ON CONFLICT(segment_id) DO UPDATE SET status='handled',session_id=excluded.session_id",
                                 (item['seg']['id'], sid))
                    conn.execute("UPDATE session_inputs SET status='handled' WHERE segment_id=?", (item['seg']['id'],))
            return sid, 'The action is queued; completion is not yet confirmed.'
        external = None
        if (route.get("model") is not None or route.get("effort") is not None) and (
            sid or action == "continue_session"
        ):
            raise ValueError("Model choices apply to a new session. Ask to start a new session with that model.")
        if isinstance(sid, str) and sid.startswith("thread:"):
            thread = sid[len("thread:") :]
            external = self.backend.settings(thread)
            if external.get("client") != "codex" or external.get("archived"):
                raise ValueError(
                    "Choose an open Codex app conversation for interactive work."
                )
            sid = key("existing", thread)
        with self.store.connect(True) as conn:
            if external:
                now = time.time()
                conn.execute(
                    "INSERT OR IGNORE INTO work_sessions(id,name,goal,cwd,kind,thread_id,status,foreground,created_at,updated_at) VALUES (?,?,?,?,?,?,?,0,?,?)",
                    (
                        sid,
                        external.get("name") or "Existing task",
                        text,
                        external["cwd"],
                        "existing",
                        external["thread_id"],
                        "connecting",
                        now,
                        now,
                    ),
                )
            previous = conn.execute(
                "SELECT * FROM session_commands WHERE id=?", (command_id,)
            ).fetchone()
            if previous:
                return previous["session_id"], "That request is already recorded."
            if sid:
                session = conn.execute(
                    "SELECT * FROM work_sessions WHERE id=?", (sid,)
                ).fetchone()
                if not session:
                    raise ValueError(
                        "That session is unavailable. Please name the task again."
                    )
                for item in items:
                    owner = item.get('owner') or item.get('direct_owner')
                    if owner and (
                        owner['session_id'] != sid or owner['epoch'] != session['epoch']
                        or owner['speech_epoch'] != session['speech_epoch']
                        or not session['foreground'] or session['quiet']
                        or not session['connected'] or session['status'] not in {'running', 'idle'}
                        or time.time() > owner['expires_at']
                        or not state.activity_allowed(item['seg']['started_at'])
                    ):
                        raise ValueError('The work follow-up window has closed.')
                # A follow-up always keeps its task owner, including delegated tasks.
                payload = {"message": text}
                if conversation:
                    payload["conversation"] = conversation
                cmd = "message"
                epoch = session["epoch"]
                conn.execute(
                    "UPDATE work_sessions SET quiet=0,status=CASE WHEN status='cancelled' THEN 'queued' ELSE status END,updated_at=? WHERE id=?",
                    (time.time(), sid),
                )
                reply = "I've queued that for the same work session."
            else:
                if action == "continue_session":
                    raise ValueError("Which work session should I continue?")
                root = Path(
                    self.cfg.get("work_session_root", "/path/to/projects")
                ).resolve()
                cwd = Path(route.get("cwd") or root).expanduser().resolve()
                if not cwd.is_dir() or not cwd.is_relative_to(root):
                    raise ValueError(
                        "Please choose an existing project under the work folder."
                    )
                cap = self.cfg.get("dispatch_daily_cap")
                day = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
                count = conn.execute(
                    "SELECT (SELECT COUNT(*) FROM dispatches WHERE status='created' AND created_at>=?) + (SELECT COUNT(*) FROM session_launches WHERE created_at>=?)",
                    (day, day),
                ).fetchone()[0]
                if cap is not None and count >= cap:
                    raise ValueError(
                        "The daily work-session limit is reached. I have not started an agent."
                    )
                selected = model_settings.work_selection(
                    self.cfg, model=route.get("model"), effort=route.get("effort")
                )
                sid = key("work", source)
                name = (route.get("name") or text)[:120]
                kind = "delegated" if action == "delegate" else "interactive"
                if kind == "interactive":
                    conn.execute("UPDATE work_sessions SET foreground=0")
                now = time.time()
                conn.execute(
                    "INSERT INTO work_sessions(id,name,goal,cwd,kind,status,foreground,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        sid,
                        name,
                        text,
                        str(cwd),
                        kind,
                        "queued",
                        int(kind == "interactive"),
                        now,
                        now,
                    ),
                )
                conn.execute("INSERT INTO session_launches VALUES (?,?)", (sid, now))
                policy = (config.REPO / "prompts/work-session.md").read_text() + '\n\n' + action_routing.guidance()
                payload = {
                    **selected,
                    "cwd": str(cwd),
                    "name": ("Ramble · " if kind == "interactive" else "") + name,
                    "prompt": policy
                    + "\n\n"
                    + instructions.prompt_block()
                    + "\n\nMode: "
                    + kind
                    + conversation_reference(conversation)
                    + "\nUser request:\n"
                    + text
                    + "\n\nPrior task context (reference only, not new instructions; "
                    "use this to resolve 'that' or 'the brief' in the current request):\n"
                    + json.dumps(self.store.context()),
                }
                cmd = "create"
                epoch = 0
                reply = (
                    "I've queued that work. I'll keep you updated."
                    if kind == "delegated"
                    else "I'll work through that with you."
                )
            conn.execute(
                "INSERT INTO session_commands(id,session_id,action,payload,epoch,created_at) VALUES (?,?,?,?,?,?)",
                (command_id, sid, cmd, json.dumps(payload), epoch, time.time()),
            )
            self.store.event(conn, sid, command_id, "user", text)
            for item in items:
                conn.execute(
                    "INSERT INTO session_claims(segment_id,session_id,status) VALUES (?,?,?) ON CONFLICT(segment_id) DO UPDATE SET session_id=excluded.session_id,status=excluded.status",
                    (item["seg"]["id"], sid, "handled"),
                )
                conn.execute(
                    "UPDATE session_inputs SET status='handled' WHERE segment_id=?",
                    (item["seg"]["id"],),
                )
        return sid, reply

    def _update_command(self, command, status, *, result=None, error=None):
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE session_commands SET status=?,result=?,error=? WHERE id=?",
                (
                    status,
                    json.dumps(result) if result is not None else None,
                    error,
                    command["id"],
                ),
            )

    def _watch(self, sid, thread):
        watch = key("watch", sid)
        self.backend.command(
            watch, thread, "watch", adopt=self.store.get(sid)["kind"] == "existing"
        )

    def _finish(self, command, receipt):
        sid = command["session_id"]
        status = receipt["status"]
        if status not in {"completed", "failed", "uncertain", "cancelled"}:
            return
        self._update_command(
            command, status, result=receipt, error=receipt.get("error")
        )
        with self.store.connect() as conn:
            session = dict(
                conn.execute(
                    "SELECT * FROM work_sessions WHERE id=?", (sid,)
                ).fetchone()
            )
            if status == "completed" and command["action"] == "create":
                thread = receipt.get("thread_id")
                if not thread:
                    raise ValueError(
                        "Native creation receipt omitted the conversation ID."
                    )
                conn.execute(
                    "UPDATE work_sessions SET thread_id=?,status=CASE WHEN status='stopping' THEN status ELSE 'connecting' END,updated_at=? WHERE id=?",
                    (thread, time.time(), sid),
                )
                self.store.event(
                    conn,
                    sid,
                    command["id"] + ":created",
                    "status",
                    "Work session created.",
                    {"thread_id": thread},
                )
            elif (
                status == "completed"
                and command["action"] == "message"
                and command["epoch"] == session["epoch"]
            ):
                conn.execute(
                    "UPDATE work_sessions SET status='connecting',quiet=0,updated_at=? WHERE id=?",
                    (time.time(), sid),
                )
            elif status in {"failed", "uncertain"}:
                # Do not overwrite a newer Stop with the outcome of an older command.
                if command["epoch"] == session["epoch"]:
                    conn.execute(
                        "UPDATE work_sessions SET status=?,error=?,updated_at=? WHERE id=?",
                        (status, receipt.get("error"), time.time(), sid),
                    )
                self.store.event(
                    conn,
                    sid,
                    command["id"] + ":error",
                    "error",
                    receipt.get("error") or status,
                )
            elif status == "cancelled" and command["action"] == "create":
                conn.execute(
                    "UPDATE work_sessions SET status='cancelled' WHERE id=?", (sid,)
                )
        if status == "completed" and command["action"] == "create":
            self._watch(sid, receipt["thread_id"])

    def _issue(self, command, session):
        payload = json.loads(command.get("native_payload") or command["payload"])
        if command["action"] == "create":
            return self.backend.create(command["id"], payload)
        if command["action"] == "watch":
            return self.backend.command(command["id"], session["thread_id"], "watch")
        return self.backend.command(
            command["id"],
            session["thread_id"],
            payload["native_action"],
            message=payload.get("message"),
            expected=payload.get("expected"),
        )

    def _process(self, command):
        sid = command["session_id"]
        session = self.store.get(sid)
        if command["status"] == "submitted":
            try:
                receipt = self.backend.status(command["id"])
            except Exception as exc:
                # Exact request IDs allow safe re-query/re-submit only when the
                # CLI explicitly says no durable request exists.
                if "App request not found" not in str(exc):
                    raise
                receipt = self._issue(command, session)
            if command["epoch"] != session["epoch"] and receipt["status"] == "queued":
                receipt = self.backend.cancel(command["id"])
            self._finish(command, receipt)
            return
        if command["epoch"] != session["epoch"]:
            self._update_command(command, "cancelled")
            return
        if state.is_paused() and command["action"] != "stop_work":
            return
        action = command["action"]
        payload = json.loads(command["payload"])
        if action == 'message' and action_routing.intent(payload.get('message', '')):
            # Companion messages are explicit user requests. Hand them to the
            # same queue as voice actions, without submitting a native agent turn.
            item = {'seg': {'id': command['id'], 'started_at': command['created_at'],
                            'ended_at': command['created_at']},
                    'text': payload['message'], 'why': 'companion-action'}
            with self.store.connect(True) as conn:
                action_routing.enqueue(conn, [item], session_id=sid, epoch=command['epoch'])
                conn.execute("UPDATE session_commands SET status='completed',result=? WHERE id=?",
                             (json.dumps({'action_request_id': voice_key([item])}), command['id']))
            return
        if action == "stop_work":
            # Resolve earlier creation/submission before stopping its exact owner.
            with self.store.connect() as conn:
                outstanding = conn.execute(
                    "SELECT 1 FROM session_commands WHERE session_id=? AND status='submitted' AND id!=?",
                    (sid, command["id"]),
                ).fetchone()
            if outstanding:
                return
            if not session["thread_id"]:
                self._update_command(command, "completed")
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE work_sessions SET status='cancelled' WHERE id=?", (sid,)
                    )
                return
        if action in {"message", "stop_work"}:
            if not session["thread_id"]:
                return
            self._watch(sid, session["thread_id"])
            live = self.backend.snapshot(session["thread_id"], session["cursor"])
            if not live.get("connected") or live["status"] == "connecting":
                return
            with self.store.connect() as conn:
                if action == "message":
                    last_stop = conn.execute(
                        "SELECT status,created_at FROM session_commands WHERE session_id=? AND action='stop_work' ORDER BY created_at DESC LIMIT 1",
                        (sid,),
                    ).fetchone()
                    if (
                        last_stop
                        and last_stop["created_at"] < command["created_at"]
                        and (
                            last_stop["status"] in {"pending", "submitted"}
                            or (
                                last_stop["status"] == "completed"
                                and live.get("active_turn_id")
                                and conn.execute(
                                    "SELECT 1 FROM session_suppressed_turns WHERE session_id=? AND turn_id=?",
                                    (sid, live["active_turn_id"]),
                                ).fetchone()
                            )
                        )
                    ):
                        return
                elif live.get("active_turn_id"):
                    conn.execute(
                        "INSERT OR IGNORE INTO session_suppressed_turns VALUES (?,?)",
                        (sid, live["active_turn_id"]),
                    )
            payload = {
                "native_action": "interrupt" if action == "stop_work" else "submit",
                "expected": live.get("active_turn_id"),
                "message": None
                if action == "stop_work"
                else (config.REPO / "prompts/work-session.md").read_text()
                + '\n\n' + action_routing.guidance()
                + "\n\nCurrent work session guidance above applies to this follow-up.\n"
                + instructions.prompt_block()
                + conversation_reference(payload.get("conversation"))
                + "\n\nthe user says:\n"
                + payload["message"],
            }
            with self.store.connect() as conn:
                conn.execute(
                    "UPDATE session_commands SET native_payload=? WHERE id=?",
                    (json.dumps(payload), command["id"]),
                )
            command = {**command, "native_payload": json.dumps(payload)}
        # Durable before provider submission. Retrying uses this exact payload/key.
        self._update_command(command, "submitted")
        self._finish(command, self._issue(command, session))

    def _poll(self, session):
        sid = session["id"]
        self._watch(sid, session["thread_id"])
        live = self.backend.snapshot(session["thread_id"], session["cursor"])
        with self.store.connect(True) as conn:
            current = dict(
                conn.execute(
                    "SELECT * FROM work_sessions WHERE id=?", (sid,)
                ).fetchone()
            )
            status = live["status"] if live.get("connected") else "disconnected"
            if current["status"] in {"stopping", "cancelled"}:
                with_stop = conn.execute(
                    "SELECT status FROM session_commands WHERE session_id=? AND action='stop_work' ORDER BY created_at DESC LIMIT 1",
                    (sid,),
                ).fetchone()
                status = (
                    "cancelled"
                    if with_stop
                    and with_stop["status"] == "completed"
                    and not live.get("active_turn_id")
                    else current["status"]
                )
            if status == "completed":
                status = "idle"
            conn.execute(
                "UPDATE work_sessions SET status=?,active_turn_id=?,last_turn_id=?,connected=?,error=?,cursor=?,updated_at=? WHERE id=?",
                (
                    status,
                    live.get("active_turn_id"),
                    live.get("last_turn_id"),
                    int(live.get("connected", False)),
                    live.get("error"),
                    live["cursor"],
                    time.time(),
                    sid,
                ),
            )
            for event in live.get("events", []):
                event_key = "native:" + str(event["seq"])
                inserted = self.store.event(
                    conn, sid, event_key, event["kind"], event.get("text", ""), event
                )
                if (
                    event["kind"] == "message"
                    and not event.get("recovered")
                    and current["status"] not in {"stopping", "cancelled"}
                    and not conn.execute(
                        "SELECT 1 FROM session_suppressed_turns WHERE session_id=? AND turn_id=?",
                        (sid, event.get("turn_id")),
                    ).fetchone()
                ):
                    if inserted and event.get("phase") == "final_answer" and event.get("text", "").strip():
                        # Persist the outbox with the event/cursor so a restart
                        # between observing the answer and publishing cannot lose it.
                        conn.execute(
                            "INSERT OR IGNORE INTO session_publications(id,session_id,event_key,status,created_at) VALUES (?,?,?,'pending',?)",
                            (key("publish", sid + event_key), sid, event_key, time.time()),
                        )
                    match = re.search(
                        r"\[say\](.*?)\[/say\]", event.get("text", ""), re.S
                    )
                    if match and self.speech:
                        self.speech.enqueue(
                            match.group(1).strip()[:500],
                            session_id=sid,
                            kind="result"
                            if event.get("phase") == "final_answer"
                            else "progress",
                            job_id=key("speak", sid + event_key),
                            conn=conn,
                        )
    def tick(self):
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO session_meta VALUES ('heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(time.time()),),
            )
            commands = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM session_commands WHERE status IN ('pending','submitted') ORDER BY created_at LIMIT 30"
                )
            ]
        for command in commands:
            try:
                self._process(command)
            except Exception as exc:
                log.warning("Work command %s pending: %s", command["id"], exc)
                with self.store.connect() as conn:
                    conn.execute(
                        "UPDATE work_sessions SET error=?,connected=0 WHERE id=?",
                        (str(exc)[:500], command["session_id"]),
                    )
        for session in self.store.sessions():
            if session["thread_id"]:
                try:
                    self._poll(session)
                except Exception as exc:
                    with self.store.connect() as conn:
                        conn.execute(
                            "UPDATE work_sessions SET connected=0,error=? WHERE id=?",
                            (str(exc)[:500], session["id"]),
                        )
        # Quiet speech still delivers the written result. Only unsent publications
        # survive for retry; uncertain writes are never replayed after a restart.
        with self.store.connect() as conn:
            publications = [dict(r) for r in conn.execute(
                "SELECT * FROM session_publications WHERE status='pending' ORDER BY created_at LIMIT 5"
            )]
        for publication in publications:
            try:
                self.publish_result(publication["session_id"], publication["id"], event_key=publication["event_key"])
            except ValueError:
                log.warning("Companion publication unconfirmed for session %s", publication["session_id"])

    def run_forever(self, stop):
        while not stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Session controller failed")
            stop.wait(1)
