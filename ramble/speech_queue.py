"""Speech delivery runs separately from task execution and user-input routing."""
from __future__ import annotations
import json
import logging
import threading
import time
import uuid
from . import latency, speech, state

log = logging.getLogger("ramble.speech_queue")


class SpeechQueue:
    def __init__(self, cfg, store):
        self.cues = None
        self.cfg = cfg
        self.store = store
        self.speech_active = lambda: False
        self.last_progress = 0.0

    def enqueue(
        self,
        text,
        *,
        session_id=None,
        kind="reply",
        job_id=None,
        thought_id=None,
        conn=None,
        since=None,
    ):
        job_id = job_id or "reply-" + uuid.uuid4().hex

        def save(c):
            epoch = 0
            if session_id:
                row = c.execute(
                    "SELECT speech_epoch,quiet FROM work_sessions WHERE id=?",
                    (session_id,),
                ).fetchone()
                if not row or row["quiet"]:
                    return job_id
                epoch = row["speech_epoch"]
            if kind == "progress":
                c.execute(
                    "UPDATE session_speech SET status='superseded' WHERE session_id=? AND kind='progress' AND status='pending'",
                    (session_id,),
                )
            ttl = 45 if kind == "progress" else 120
            c.execute(
                "INSERT OR IGNORE INTO session_speech(id,session_id,text,kind,priority,epoch,created_at,expires_at,thought_id,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    session_id,
                    text,
                    kind,
                    {"reply": 0, "result": 1, "progress": 2}[kind],
                    epoch,
                    time.time(),
                    time.time() + ttl,
                    thought_id,
                    "pending" if state.audio_allowed(since) else "cancelled",
                ),
            )
            latency.event('speech_queued', speech_job_id=job_id, thought_id=thought_id, kind=kind)
            return job_id

        if conn is not None:
            return save(conn)
        with self.store.connect(True) as c:
            return save(c)

    def cancelled(self, job, stop):
        if (
            stop.is_set()
            or time.time() > job["expires_at"]
            or not state.audio_allowed(job["created_at"])
        ):
            return True
        with self.store.connect() as conn:
            row = conn.execute(
                "SELECT status FROM session_speech WHERE id=?", (job["id"],)
            ).fetchone()
            if not row or row["status"] != "playing":
                return True
            if job["session_id"]:
                session = conn.execute(
                    "SELECT speech_epoch,quiet FROM work_sessions WHERE id=?",
                    (job["session_id"],),
                ).fetchone()
                if (
                    not session
                    or session["quiet"]
                    or session["speech_epoch"] != job["epoch"]
                ):
                    return True
        return False

    def tick(self, stop):
        with self.store.connect(True) as conn:
            conn.execute(
                "UPDATE session_speech SET status='expired' WHERE status='pending' AND expires_at<?",
                (time.time(),),
            )
            for pending in conn.execute("SELECT id,created_at FROM session_speech WHERE status='pending'").fetchall():
                if not state.audio_allowed(pending['created_at']):
                    conn.execute("UPDATE session_speech SET status='cancelled' WHERE id=?", (pending['id'],))
            waiting = self.cues.pending_speech() if self.cues else None
            if waiting:
                row = conn.execute('SELECT status FROM session_speech WHERE id=?', (waiting,)).fetchone()
                if row and row['status'] not in ('pending', 'playing'):
                    self.cues.finish_speech_wait(waiting)
            if (
                self.speech_active()
                or conn.execute(
                    "SELECT 1 FROM segments WHERE status='pending' LIMIT 1"
                ).fetchone()
            ):
                return
            row = conn.execute(
                "SELECT * FROM session_speech WHERE status='pending' ORDER BY priority,created_at LIMIT 1"
            ).fetchone()
            if not row:
                return
            job = dict(row)
            if job[
                "kind"
            ] == "progress" and time.monotonic() - self.last_progress < self.cfg.get(
                "work_progress_interval_s", 30
            ):
                return
            conn.execute(
                "UPDATE session_speech SET status='playing' WHERE id=?", (job["id"],)
            )
        # Recover the originating request on this separate worker thread.
        with self.store.connect() as conn:
            source = conn.execute('SELECT id FROM segments WHERE thought_id=? ORDER BY started_at LIMIT 1',
                                  (job['thought_id'],)).fetchone()
        with latency.request(source[0] if source else job['id']):
            self._deliver(job, stop)

    def _deliver(self, job, stop):
        latency.event('speech_job_start', speech_job_id=job['id'], thought_id=job['thought_id'],
                      queue_wait_s=time.time() - job['created_at'], kind=job['kind'])
        status = "played"
        error = None
        try:
            if self.cancelled(job, stop):
                raise state.PausedError("Speech cancelled")
            if self.cfg.get("fast_speak", True):
                speech.speak(
                    job["text"],
                    self.cfg,
                    cancelled=lambda: self.cancelled(job, stop),
                    origin="reply" if job["kind"] == "reply" else "work",
                    **({'on_playback_start': lambda: self.cues.finish_speech_wait(job['id'])}
                       if self.cues else {}),
                )
            else:
                status = "suppressed"
        except state.PausedError:
            status = "cancelled"
        except Exception as exc:
            status = "failed"
            error = str(exc)[:500]
            log.exception("Queued speech failed")
        finally:
            if self.cues:
                self.cues.finish_speech_wait(job['id'])
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE session_speech SET status=?,error=?,finished_at=? WHERE id=?",
                (status, error, time.time(), job["id"]),
            )
            if job["thought_id"]:
                note = f"(Ramble speech {status}: {job['text']})"
                conn.execute(
                    "UPDATE thoughts SET text=text || char(10) || ? WHERE id=?",
                    (note, job["thought_id"]),
                )
        latency.event('speech_job_end', speech_job_id=job['id'], status=status)
        if job["kind"] == "progress" and status == "played":
            self.last_progress = time.monotonic()

    def run_forever(self, stop):
        # Playback may already have reached the speaker; never replay after a crash.
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE session_speech SET status='uncertain',error='Playback interrupted by app restart; not replayed.' WHERE status='playing'"
            )
        while not stop.is_set():
            try:
                self.tick(stop)
            except Exception:
                log.exception("Speech scheduler failed")
            stop.wait(0.2)
