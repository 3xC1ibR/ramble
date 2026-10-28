"""A bounded quick-chat anchor, saved in the existing runtime metadata store."""
from __future__ import annotations

import json
import time

from . import state

KEY = "quick_chat_followup"


def foreground(conn):
    # Polling/heartbeats do not invalidate chat; deliberate control/focus does.
    sessions = [dict(row) for row in conn.execute(
        "SELECT id,epoch,speech_epoch,quiet FROM work_sessions WHERE foreground=1 ORDER BY id"
    )]
    interaction = conn.execute("SELECT MAX(seq) FROM session_events WHERE kind IN ('user','control')").fetchone()[0]
    return {'sessions': sessions, 'interaction': interaction}


def clear(conn):
    conn.execute("DELETE FROM session_meta WHERE key=?", (KEY,))


def save(conn, cfg, request_id, conversation, speech_job_id):
    window = min(600, max(0, cfg.get("chat_followup_s", 600)))
    clear(conn)
    if not window or not speech_job_id:
        return
    now = time.time()
    conversation = [{**e, 'text': e['text'][:4000]} for e in conversation
                    if 0 <= now - e.get('timestamp', now) < 600]
    anchor = {"id": request_id, "created_at": now, "expires_at": now + window,
              "conversation": conversation[-8:], "speech_job_id": speech_job_id,
              "foreground": foreground(conn)}
    conn.execute("INSERT INTO session_meta(key,value) VALUES (?,?)", (KEY, json.dumps(anchor)))


def current(conn, cfg, started_at):
    window = min(600, max(0, cfg.get("chat_followup_s", 600)))
    if not window:
        return None
    row = conn.execute("SELECT value FROM session_meta WHERE key=?", (KEY,)).fetchone()
    if not row:
        return None
    anchor = json.loads(row[0])
    if (not 0 <= started_at - anchor['created_at'] <= window
            or time.time() > anchor['expires_at']
            or not state.activity_allowed(anchor['created_at'])
            or foreground(conn) != anchor['foreground']):
        return None
    # A generated, queued, muted or failed reply cannot start a voice conversation.
    played = conn.execute("SELECT 1 FROM session_speech WHERE id=? AND status='played' AND kind='reply'",
                          (anchor['speech_job_id'],)).fetchone()
    anchor['conversation'] = [e for e in anchor['conversation']
                              if 0 <= started_at - e.get('timestamp', anchor['created_at']) < 600]
    return anchor if played else None


def valid(conn, cfg, item):
    anchor = current(conn, cfg, item['seg']['started_at'])
    return (anchor is not None and anchor['id'] == item['chat']['id']
            and state.activity_allowed(item['seg']['started_at'])
            and time.time() <= item['chat']['candidate_expires_at'])


def admitted_valid(conn, item):
    """Keep Stop/focus/expiry effective during response generation and restart."""
    anchor = item.get('chat_admission')
    return not anchor or (
        time.time() <= anchor['candidate_expires_at']
        and state.activity_allowed(anchor['created_at'])
        and foreground(conn) == anchor['foreground']
    )
