"""Addressed speech and bounded conversational follow-ups to an existing owner."""

from __future__ import annotations

import json
import logging
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

from . import chat_followup, codex_runner, config, db, instructions, latency, reminders, state, time_entries, voice_control
from .jev import JevClient, JevError
from .addressing import ADDRESS, NAME, has_name
from .cues import AudioCues
from .early_wake import EarlyWake
from .fast_trace import FastTrace, diagnostic
from . import model_settings, direct_routing, action_routing, usage
from .sessions import ROUTE_SCHEMA
from .router import clean_env
from .transcribe import stamp

log = logging.getLogger("ramble.fast")

FAST_PROMPT = config.REPO / "prompts" / "fast.md"
FOLLOWUP_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"accept": {"type": "boolean"}}, "required": ["accept"],
}

REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string", "description": "What Ramble says out loud: one or two short sentences, no markdown."},
        "route": ROUTE_SCHEMA,
        "handoff": {"type": "boolean", "description": "Legacy dispatch only when interactive sessions are unavailable; false otherwise."},
        "reminder": reminders.ACTION_SCHEMA,
        "time_entry": time_entries.ACTION_SCHEMA,
    },
    "required": ["reply", "handoff", "reminder", "time_entry", "route"],
}


class FastLane:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.candidates: queue.Queue = queue.Queue()
        self.history: deque = deque(maxlen=8)  # (ts, who, text)
        self.name_only_until = 0.0
        self.request_until = 0.0
        self.busy = False
        self.unavailable_until = 0.0
        self.unavailable_reply = ""
        self.sessions = None
        self.jev = JevClient(cfg)
        self.cues = AudioCues(cfg)
        self.early = EarlyWake(cfg, self.cues)
        self.speech_active = lambda: False  # wired to the recorder by the daemon
        self.last_voice_mono = lambda: 0.0

    # ---- called from the transcriber thread --------------------------------
    def offer(self, conn, seg, text: str) -> bool:
        """Queue the utterance if it's addressed to Ramble. Returns True if queued."""
        feedback = self.early.complete(seg['started_at'])
        admitted = False
        try:
            admitted = self._offer(conn, seg, text, acknowledged=feedback.acknowledged,
                                   provisional_work=feedback.work_token)
            return admitted
        finally:
            if not admitted and feedback.work_token is not None:
                self.cues.end_work(feedback.work_token)

    def _offer(self, conn, seg, text: str, *, acknowledged=False, provisional_work=None) -> bool:
        if not state.activity_allowed(seg["started_at"]):
            self.name_only_until = self.request_until = 0.0
            return False
        voice_enabled = voice_control.command(text)
        if voice_enabled is not None:
            # Apply before any chime/model call. This stays available while
            # muted and when the model-backed fast lane is disabled or busy.
            voice_control.write(voice_enabled)
            if self.sessions:
                chat_followup.clear(conn)
            conn.execute("INSERT OR IGNORE INTO session_claims(segment_id,status) VALUES (?,'handled')",
                         (seg['id'],))
            self.cues.end_work()
            self.name_only_until = self.request_until = 0.0
            log.info("voice responses %s (local command)", "enabled" if voice_enabled else "disabled")
            return True
        companion = dict(seg).get('source') == 'companion'
        named = has_name(text)
        control = direct_routing.control(text)
        if control and self.sessions:
            row = conn.execute('SELECT id FROM work_sessions WHERE foreground=1 LIMIT 1').fetchone()
            if row:
                self.sessions.store.control(row['id'], control, 'voice-control-' + seg['id'], conn=conn)
                conn.execute("INSERT OR IGNORE INTO session_claims(segment_id,session_id,status) VALUES (?,?,'handled')",
                             (seg['id'], row['id']))
                chat_followup.clear(conn)
                self.cues.end_work()
                self.name_only_until = self.request_until = 0.0
                latency.event('local_work_control', segment_id=seg['id'], action=control)
                return True
        if not self.cfg.get("fast_enabled", True):
            return False
        rest = NAME.sub(" ", ADDRESS.sub(" ", text)).strip(" ,.!?")
        if named and not rest:
            # Just "Ramble?" -> the next utterance is the request.
            self.name_only_until = seg["ended_at"] + self.cfg["fast_name_window_s"]
            if self.sessions:
                chat_followup.clear(conn)
            if not acknowledged:
                self.cues.emit("accepted")
            log.info("name heard, listening for the request")
            latency.event("name_only", segment_id=seg["id"])
            return False
        followup = self._in_followup(conn, seg["started_at"])
        continuation = seg["started_at"] <= max(self.name_only_until, self.request_until)
        if not (companion or named or followup or continuation):
            if provisional_work is not None:
                self.cues.end_work(provisional_work)
            chat = (chat_followup.current(conn, self.cfg, seg['started_at'])
                    if self.sessions and self.jev.available else None)
            if chat:
                chat['candidate_expires_at'] = min(chat['expires_at'], seg['ended_at'] + 120)
                item = {'seg': dict(seg), 'text': text, 'why': 'chat-candidate', 'chat': chat}
                self.sessions.accept_input(conn, item)
                self.candidates.put(item)
                latency.event('chat_followup_candidate', segment_id=seg['id'], conversation_id=chat['id'])
                return True
            owner = self.sessions.followup_owner(conn, seg['started_at']) if self.sessions else None
            if owner:
                item = {'seg': dict(seg), 'text': text, 'why': 'work-candidate', 'owner': owner}
                self.sessions.accept_input(conn, item)
                self.candidates.put(item)
                log.info('work follow-up candidate: %s', seg['id'])
                return True
            return False
        if not companion and not continuation and not acknowledged:
            self.cues.emit("accepted")
        self.name_only_until = 0.0
        # Whisper emits each pause-delimited segment independently. Keep the rest
        # of an addressed request even before our first reply has been played.
        self.request_until = seg["ended_at"] + self.cfg["fast_name_window_s"]
        why = "companion" if companion else "named" if named else "follow-up" if followup else "continuation"
        item = {"seg": dict(seg), "text": text, "why": why}
        if self.sessions:
            owner = self.sessions.followup_owner(conn, seg['started_at'])
            if owner and not direct_routing.needs_router(text):
                item['direct_owner'] = owner
            chat = chat_followup.current(conn, self.cfg, seg['started_at'])
            if chat:
                item['conversation'] = chat['conversation']
            # A new directly addressed request supersedes an in-flight check.
            chat_followup.clear(conn)
            self.sessions.accept_input(conn, item)
        self.cues.start_work(seg['started_at'])
        self.q.put(item)
        log.info("addressed (%s): %s", why, text[:100])
        latency.event("request_queued", segment_id=seg["id"], why=why)
        return True

    def _in_followup(self, conn, started_at: float) -> bool:
        row = conn.execute("SELECT MAX(p.ended_at) FROM speech_playbacks p LEFT JOIN speech_origins o ON o.playback_id=p.id WHERE COALESCE(o.origin,'reply')='reply' AND p.ended_at <= ?", (started_at + 1,)).fetchone()
        last_end = row[0] if row else None
        return bool(last_end) and -1 <= started_at - last_end <= self.cfg["followup_s"]

    # ---- worker ------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        conn = db.connect()
        action_routing.recover_interrupted(conn)
        log.info('late chat follow-ups: %s (window=%ss, model=%s, threshold=%s)',
                 'enabled' if self.sessions and self.jev.available and self.cfg.get('chat_followup_s', 600) > 0 else 'disabled',
                 min(600, self.cfg.get('chat_followup_s', 600)), self.cfg.get('jev_model', 'jev-1.13.0'),
                 self.cfg.get('chat_followup_threshold', .8))
        if self.sessions:
            for item in self.sessions.pending_inputs():
                (self.candidates if item.get('owner') or item.get('chat') else self.q).put(item)
        while not stop.is_set():
            try:
                item = self.q.get(timeout=.1)
            except queue.Empty:
                queued = action_routing.pending(conn) if not state.is_paused() else None
                if queued:
                    items = json.loads(queued['items'])
                    items[0]['action_request_id'] = queued['id']
                    items[0]['action_session_id'] = queued['session_id']
                    items[0]['action_epoch'] = queued['epoch']
                    try:
                        self.busy = True
                        self.respond(conn, items, stop)
                    except Exception:
                        log.exception('Recovered action failed')
                        action_routing.finish(conn, queued['id'], 'unconfirmed',
                                              'The action result could not be confirmed. Check its current state.')
                    finally:
                        self.busy = False
                    continue
                if time.monotonic() < self.unavailable_until:
                    continue  # Keep passive candidates reserved during backoff.
                try:
                    candidate = self.candidates.get_nowait()
                except queue.Empty:
                    continue
                try:
                    self.busy = True
                    self.respond_candidate(conn, candidate, stop)
                except Exception:  # noqa: BLE001
                    log.exception('follow-up failed')
                finally:
                    self.busy = False
                continue
            # Coalesce a request split across two segments by a short pause.
            items = self.collect(conn, [item], stop)
            try:
                self.busy = True
                self.respond(conn, items, stop)
            except Exception:  # noqa: BLE001
                log.exception("fast lane failed")
            finally:
                self.busy = False

    def respond_candidate(self, conn, item, stop):
        if time.monotonic() < self.unavailable_until:
            self.candidates.put(item)
            return
        if item.get('chat'):
            self.respond_chat_candidate(conn, item, stop)
            return
        # Candidates never enter direct-request coalescing or arm a name/reply
        # window. Their captured owner/context survive a process restart.
        # Wait for the transcriber's publication transaction before reading a
        # just-enqueued candidate on this worker's separate connection.
        conn.execute('BEGIN IMMEDIATE')
        try:
            pending = conn.execute(
                "SELECT 1 FROM session_claims WHERE segment_id=? AND status='candidate'",
                (item['seg']['id'],),
            ).fetchone()
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if not pending:
            return
        owner = item['owner']
        try:
            if stop.is_set() or not state.activity_allowed(item['seg']['started_at']) or time.time() > owner['expires_at']:
                return
            prompt = (config.REPO / 'prompts/work-followup.md').read_text() + '\n\n' + json.dumps({
                'work': owner, 'utterance': item['text'],
            })
            answer = self.run_codex(prompt, schema=FOLLOWUP_SCHEMA, read_only=True,
                                    request_id=item['seg']['id'], cancelled=lambda: stop.is_set()
                                    or not state.activity_allowed(item['seg']['started_at']))
            latency.event('work_followup_decision', segment_id=item['seg']['id'],
                          session_id=owner['session_id'], accepted=answer.get('accept') is True,
                          seconds_since_conversation=owner.get('seconds_since_conversation'))
            if answer.get('accept') is not True or stop.is_set():
                return
            sid, _ = self.sessions.route(
                {'action': 'continue_session', 'session_id': owner['session_id']},
                item['text'], [item],
            )
            log.info('work follow-up admitted: %s owner=%s', item['seg']['id'], sid)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, codex_runner.RateLimitedError):
                self.service_failure(exc)
            log.exception('work follow-up not admitted: %s', item['seg']['id'])
        finally:
            # A successful route atomically changes the candidate claim to
            # handled. Otherwise release it for slow triage, including errors.
            self.sessions.release_candidate(conn, item)

    def collect_chat_candidates(self, conn, items, stop):
        """Coalesce speech without admitting it or opening an unchecked window."""
        deadline = self.collection_deadline(items)
        limit = time.monotonic() + 15
        while not stop.is_set() and self.q.empty() and not state.is_paused():
            for _ in range(self.candidates.qsize()):
                try:
                    candidate = self.candidates.get_nowait()
                except queue.Empty:
                    break
                if (candidate.get('chat', {}).get('id') == items[0]['chat']['id']
                        and 0 <= candidate['seg']['started_at'] - items[-1]['seg']['ended_at']
                        <= self.cfg['fast_name_window_s'] and len(items) < 8):
                    items.append(candidate)
                    deadline = max(deadline, self.collection_deadline(items))
                else:
                    self.candidates.put(candidate)
            pending = conn.execute("SELECT 1 FROM segments WHERE status='pending' LIMIT 1").fetchone()
            deadline = max(deadline, self.last_voice_mono() + self.cfg['fast_coalesce_s'])
            if time.monotonic() >= limit or (time.monotonic() >= deadline and not self.speech_active() and not pending):
                break
            stop.wait(.05)
        return items

    def respond_chat_candidate(self, conn, item, stop):
        items = [item]
        promoted = False
        try:
            for _ in range(3):
                self.collect_chat_candidates(conn, items, stop)
                # The publication transaction may still be running on the
                # transcriber connection. This also excludes replayed claims.
                conn.execute('BEGIN IMMEDIATE')
                try:
                    valid = not stop.is_set() and self.q.empty() and all(
                        chat_followup.valid(conn, self.cfg, i) and conn.execute(
                            "SELECT 1 FROM session_claims WHERE segment_id=? AND status='candidate'",
                            (i['seg']['id'],),
                        ).fetchone() for i in items)
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
                if not valid:
                    return
                text = ' '.join(i['text'] for i in items)
                t0 = time.monotonic()
                answer = self.jev.classify(item['chat']['conversation'], text,
                                          item['seg']['started_at'] - item['chat']['created_at'])
                latency.event('chat_followup_decision', segment_ids=[i['seg']['id'] for i in items],
                              conversation_id=item['chat']['id'], elapsed_s=time.monotonic() - t0,
                              **answer)
                count = len(items)
                if self.speech_active() or not self.candidates.empty() or conn.execute(
                    "SELECT 1 FROM segments WHERE status='pending' LIMIT 1"
                ).fetchone():
                    self.collect_chat_candidates(conn, items, stop)
                if len(items) != count:
                    continue  # Judge the complete request before allowing work.
                if answer.get('accept') is not True:
                    return
                break
            else:
                return
            conn.execute('BEGIN IMMEDIATE')
            try:
                if stop.is_set() or not self.q.empty() or not all(chat_followup.valid(conn, self.cfg, i) for i in items):
                    conn.rollback()
                    return
                for i in items:
                    i['chat_admission'] = i.pop('chat')
                    i['conversation'] = i['chat_admission']['conversation']
                    i['why'] = 'chat-followup'
                    conn.execute("UPDATE session_inputs SET item=? WHERE segment_id=? AND status='pending'",
                                 (json.dumps(i), i['seg']['id']))
                    conn.execute("UPDATE session_claims SET status='pending' WHERE segment_id=? AND status='candidate'",
                                 (i['seg']['id'],))
                chat_followup.clear(conn)
                conn.commit()
                promoted = True
            except BaseException:
                conn.rollback()
                raise
            self.request_until = items[-1]['seg']['ended_at'] + self.cfg['fast_name_window_s']
            self.cues.emit('accepted')
            self.respond(conn, items, stop)
        except JevError as exc:
            # Fail silently for speech that was never established as addressed.
            log.warning('chat follow-up not admitted: %s (%s)', item['seg']['id'], exc)
        finally:
            if not promoted:
                for i in items:
                    self.sessions.release_candidate(conn, i)

    def collection_deadline(self, items) -> float:
        """Convert audio silence already elapsed to a bounded monotonic wait."""
        now = time.monotonic()
        end = max((item['seg']['ended_at'] for item in items), default=time.time())
        remaining = max(0.0, self.cfg['fast_coalesce_s'] - max(0.0, time.time() - end))
        return now + remaining

    def collect(self, conn, items: list[dict], stop: threading.Event) -> list[dict]:
        """Wait through ongoing speech and its transcription, not just queue gaps."""
        if items and items[0].get('action_request_id'):
            return items  # An internal transfer must retain its original request identity.
        latency.event("collect_start", segment_ids=[i["seg"]["id"] for i in items])
        deadline = self.collection_deadline(items)
        while not stop.is_set():
            if state.is_paused():
                break
            pending = conn.execute("SELECT 1 FROM segments WHERE status='pending' LIMIT 1").fetchone()
            deadline = max(deadline, self.last_voice_mono() + self.cfg['fast_coalesce_s'])
            if time.monotonic() >= deadline and not self.speech_active() and not pending and self.q.empty():
                break
            try:
                items.append(self.q.get(timeout=.1))
                deadline = max(deadline, self.collection_deadline(items))
            except queue.Empty:
                pass
        latency.event("collect_end", segment_ids=[i["seg"]["id"] for i in items])
        return items

    def recent_conversation(self) -> list[dict]:
        """Bounded addressed exchanges, shared by routing and the task owner."""
        now = time.time()
        return [
            {"at": stamp(ts), "timestamp": ts, "speaker": who, "text": text[:4000]}
            for ts, who, text in self.history
            if 0 <= now - ts < 600
        ]

    def conversation_for(self, items):
        entries = [entry for item in items for entry in item.get('conversation', [])]
        entries.extend(self.recent_conversation())
        # Preserve a candidate's durable context across app restarts, without
        # repeating entries that are also still in the in-memory history.
        now = time.time()
        unique = {(e['at'], e['speaker'], e['text']): e for e in entries
                  if 0 <= now - e.get('timestamp', now) < 600}
        # Insertion order preserves chronology across midnight, including legacy
        # context snapshots which have only a display time, not a timestamp.
        return list(unique.values())[-8:]

    def build_prompt(self, conn, text: str, *, conversation=None) -> str:
        now = time.time()
        if conversation is None:
            conversation = self.recent_conversation()
        convo = [f"- {entry['at']} {entry['speaker']}: {entry['text']}" for entry in conversation]
        asked = []
        for r in conn.execute(
            "SELECT started_at, triage_json FROM thoughts WHERE status='done' AND started_at>=? ORDER BY started_at",
            (now - 900,),
        ):
            for it in json.loads(r["triage_json"] or "{}").get("items", []):
                if it.get("action") == "ask" and it.get("question"):
                    asked.append(f"- {stamp(r['started_at'])} Ramble asked: {it['question']}")
        return "\n".join(
            [
                config.render_prompt(FAST_PROMPT.read_text(), self.cfg),
                action_routing.guidance(),
                instructions.prompt_block(),
                "Work sessions (exact session_id for continuation/control): " + json.dumps(self.sessions.store.context() if self.sessions else []),
                "Interactive work is " + ("enabled." if self.sessions else "unavailable; use the legacy handoff for work."),
                *( [model_settings.work_routing_context(self.cfg)]
                   if self.sessions and direct_routing.needs_router(text) and not action_routing.intent(text) else [] ),
                "---",
                f"Now: {time.strftime('%A %Y-%m-%d %-I:%M %p %Z')}",
                f"Reminder timezone: {reminders.TIMEZONE}. Current ISO time: {reminders.datetime.now(reminders.ZoneInfo(reminders.TIMEZONE)).isoformat()}",
                "Recent conversation with Ramble (last 10 min):",
                *(convo or ["- none"]),
                "Recent captured speech (including requests that missed the name trigger):",
                *(f"- {stamp(r['started_at'])}: {r['text']}" for r in conn.execute(
                    "SELECT started_at, text FROM (SELECT started_at, text FROM segments WHERE status='transcribed' AND started_at>=? ORDER BY started_at DESC LIMIT 12) ORDER BY started_at",
                    (now - 600,),
                )),
                "Questions the slow lane asked recently:",
                *(asked or ["- none"]),
                "",
                f"the user just said (speech-to-text): {text}",
            ]
        )

    def run_codex(self, prompt: str, *, schema=None, read_only=False,
                  request_id=None, on_wait=None, cancelled=None) -> dict:
        selected = model_settings.selection(self.cfg, "fast")
        timeout_s = model_settings.fast_timeout(self.cfg, selected)
        work = config.TRIAGE_DIR / "tmp"
        work.mkdir(parents=True, exist_ok=True)
        # Isolate each invocation: simultaneous requests must never read or
        # delete each other's result, even when they start in the same second.
        with tempfile.TemporaryDirectory(prefix="fast-", dir=work) as request_dir:
            schema_path = Path(request_dir) / "schema.json"
            schema_path.write_text(json.dumps(schema or REPLY_SCHEMA))
            out = Path(request_dir) / "reply.json"
            cmd = [
                "codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
                *( ["--sandbox", "read-only", "-c", 'web_search="disabled"'] if read_only
                   else ["--dangerously-bypass-approvals-and-sandbox"] ),
                "--ignore-user-config", "--disable", "hooks",
                "-C", str(config.HOME),  # Runtime preferences are explicitly included in the prompt.
                "-m", selected["model"],
                "-c", f'model_reasoning_effort="{selected["effort"]}"',
                "--output-schema", str(schema_path), "-o", str(out), "-",
            ]
            env = clean_env()
            trace = FastTrace(config.TRIAGE_DIR / "fast", request_id=request_id,
                              model=selected['model'], effort=selected['effort'],
                              timeout_s=timeout_s, prompt_bytes=len(prompt.encode()),
                              purpose='followup' if read_only else 'reply',
                              executable=shutil.which('codex', path=env.get('PATH')),
                              api_key_env_present=bool(env.get('OPENAI_API_KEY')))
            outcome, details = 'failed', {}
            try:
                p = codex_runner.run(cmd, input=prompt, env=env,
                                     timeout=timeout_s, cwd=str(config.HOME),
                                     trace=trace, on_wait=on_wait, cancelled=cancelled)
                details['returncode'] = p.returncode
                result = out.read_text() if out.exists() else ""
                if p.returncode != 0 or not result.strip():
                    details.update(diagnostic(p.stderr or ''))
                    raise RuntimeError(f"Codex exited {p.returncode} without a usable reply; trace {trace.call_id}")
                answer = json.loads(result)
                if not isinstance(answer, dict):
                    raise ValueError('Codex reply is not an object')
                outcome = 'completed'
                return answer
            except codex_runner.RateLimitedError as exc:
                outcome = 'rate_limited'
                details['retry_after_s'] = exc.retry_after_s
                raise
            except subprocess.TimeoutExpired:
                outcome = 'timeout'
                raise
            except codex_runner.CancelledError:
                outcome = 'cancelled'
                raise
            except Exception as exc:
                details['error_type'] = type(exc).__name__
                raise
            finally:
                trace.finish(outcome, **details)
                usage.capture_fast(trace.path)

    def respond(self, conn, items: list[dict], stop: threading.Event | None = None) -> None:
        work = self.cues.start_work(items[0]["seg"]["started_at"])
        try:
            with latency.request(items[0]["seg"]["id"]):
                self._respond(conn, items, stop, work_token=work)
        except BaseException:
            conn.execute("UPDATE action_requests SET status='unconfirmed',reply=?,updated_at=? "
                         "WHERE id=? AND status='running'",
                         ('The action result could not be confirmed. Check its current state before trying again.',
                          time.time(), reminders.voice_key(items)))
            self.cues.end_work(self.cues.response_token(work, [i['seg']['started_at'] for i in items]))
            raise
        finally:
            self.cues.end_work(self.cues.response_token(work, [i['seg']['started_at'] for i in items]),
                               keep_pending=True)

    def service_failure(self, exc) -> str:
        # Categorize without copying model/tool output into a spoken response.
        detail = str(getattr(exc, "stderr", "") or exc).lower()
        if "429" in detail or "too many requests" in detail or "rate limit" in detail:
            reason = "Codex is rate limited"
        elif isinstance(exc, subprocess.TimeoutExpired):
            reason = "Codex timed out before finishing"
        else:
            reason = "the Codex response failed"
        self.unavailable_reply = f"I heard you, but {reason}. I couldn't confirm the result."
        cooldown = max(self.cfg.get("fast_failure_cooldown_s", 30),
                       getattr(exc, "retry_after_s", None) or 0)
        self.unavailable_until = time.monotonic() + cooldown
        return self.unavailable_reply

    def _respond(self, conn, items: list[dict], stop: threading.Event | None = None,
                 *, work_token=None) -> None:
        stop = stop if stop is not None else threading.Event()
        text = ' '.join(i['text'] for i in items)
        if direct_routing.needs_router(text) or action_routing.intent(text):
            # A later coalesced segment may explicitly ask for a different
            # destination. Its request takes precedence over the earlier pin.
            for item in items:
                if item.pop('direct_owner', None) and self.sessions:
                    conn.execute("UPDATE session_inputs SET item=? WHERE segment_id=? AND status='pending'",
                                 (json.dumps(item), item['seg']['id']))
        owner = items[0].get('direct_owner')
        if (self.sessions and owner and not direct_routing.needs_router(text) and not action_routing.intent(text)
                and all(i.get('direct_owner', {}).get('session_id') == owner['session_id'] for i in items)):
            if stop.is_set():
                self.cues.end_work(work_token)
                return
            try:
                self.sessions.route({'action': 'continue_session', 'session_id': owner['session_id']}, text, items)
            except (ValueError, OSError, sqlite3.Error) as exc:
                # A captured owner becoming stale is not permission to redirect
                # the message or create another session through the model.
                self.sessions.mark_inputs(conn, items, error=str(exc))
                log.warning('Direct work input not delivered: %s', exc)
                self.cues.emit('error')
            else:
                self.sessions.mark_inputs(conn, items, owner['session_id'])
                chat_followup.clear(conn)
                latency.event('direct_work_input', session_id=owner['session_id'],
                              segment_ids=[i['seg']['id'] for i in items])
            # The owner supplies the answer; no generic queued acknowledgement
            # or fast-model wait notice belongs on this path.
            self.cues.end_work(work_token)
            return
        t0 = time.time()
        failure = None
        waiting_job = 'waiting-' + reminders.voice_key(items)
        waiting_sent = False

        def request_allowed():
            sid = items[0].get('action_session_id')
            if sid and self.sessions:
                session = self.sessions.store.get(sid)
                if not session or session['epoch'] != items[0].get('action_epoch'):
                    return False
            return all(state.activity_allowed(i['seg']['started_at'])
                       and chat_followup.admitted_valid(conn, i) for i in items)

        def stale_request():
            if request_allowed():
                return False
            if self.sessions:
                self.sessions.mark_inputs(conn, items, error='Conversation changed; stale follow-up cancelled.')
            if items[0].get('action_request_id'):
                action_routing.finish(conn, items[0]['action_request_id'], 'cancelled', 'Action cancelled before execution.')
            return True

        def waiting(elapsed):
            nonlocal waiting_sent
            if waiting_sent or elapsed < self.cfg.get('fast_wait_notice_s', 12):
                return
            if not self.sessions or not self.sessions.speech:
                return
            waiting_sent = True
            try:
                tid = conn.execute('SELECT thought_id FROM segments WHERE id=?', (items[0]['seg']['id'],)).fetchone()
                self.sessions.speech.enqueue("I'm still waiting for Codex to finish.", kind='progress',
                                             job_id=waiting_job, thought_id=tid[0] if tid else None,
                                             since=items[0]['seg']['started_at'])
                latency.event('answer_wait_notice', elapsed_s=elapsed, speech_job_id=waiting_job)
            except Exception:  # A delivery failure must not abandon the answer.
                log.exception('Could not queue slow-response notice')

        def cancel_waiting():
            if waiting_sent:
                conn.execute("UPDATE session_speech SET status='cancelled' WHERE id=? AND status='pending'", (waiting_job,))

        while not stop.is_set():
            if stale_request():
                return
            text = " ".join(i["text"] for i in items)
            seg = items[-1]["seg"]
            target = action_routing.intent(text)
            conversation = self.conversation_for(items)
            if target and not action_routing.supported(target):
                failure = action_routing.unavailable(target)
                ans = {'reply': failure, 'handoff': False}
                break
            if time.monotonic() < self.unavailable_until:
                failure = self.unavailable_reply
                ans = {"reply": failure, "handoff": False}
                self.cues.emit("error")
                break
            try:
                with latency.span("answer", segment_ids=[i["seg"]["id"] for i in items]):
                    # Save exactly the conversation used by the final routing
                    # decision, including when speech causes an answer rerun.
                    prompt = self.build_prompt(conn, text, conversation=conversation)
                    options = {}
                    if target:
                        options['schema'] = action_routing.reply_schema(REPLY_SCHEMA, target)
                        prompt += ('\nThis accepted request belongs to the action executor: '
                                   + json.dumps(target) + '. Return resolution=action with the structured action, '
                                   'or resolution=needs_input with a question for missing arguments. '
                                   'If a required lookup fails, return resolution=failed with the specific failure. '
                                   'Do not delegate, promise completion, or ask the user to repeat the request.')
                    ans = self.run_codex(prompt, **options, request_id=items[0]['seg']['id'],
                                         on_wait=waiting, cancelled=lambda: stop.is_set()
                                         or not request_allowed())
            except codex_runner.CancelledError:
                latency.event('answer_cancelled')
                if self.sessions:
                    self.sessions.mark_inputs(conn, items, None,
                                              error='Codex request cancelled; result unconfirmed, not replayed.')
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("fast answer failed")
                if not state.activity_allowed(items[0]["seg"]["started_at"]):
                    return
                self.cues.emit("error")
                failure = self.service_failure(exc)
                ans = {"reply": failure, "handoff": False}
                # Leave later utterances queued. A failure is not a stale answer
                # worth regenerating each time the user asks whether we heard him.
                break
            finally:
                cancel_waiting()
            if stale_request():
                return
            count = len(items)
            if self.speech_active() or not self.q.empty() or conn.execute(
                "SELECT 1 FROM segments WHERE status='pending' LIMIT 1"
            ).fetchone():
                self.collect(conn, items, stop)
            if stop.is_set():
                return
            if len(items) == count:
                break
            log.info("request continued during answer generation; updating reply")
        else:
            return
        if stop.is_set() or stale_request():
            return
        target = action_routing.intent(text)
        if target and not failure and not action_routing.validate_answer(ans, target):
            failure = "I couldn't resolve the action's details. No action was taken."
            ans = {'reply': failure, 'handoff': False, 'route': None,
                   'time_entry': None, 'reminder': None}
        if target and ans.get('resolution') == 'failed':
            failure = ans['reply']
        # A model may be rerun while speech continues. Execute mutations only
        # after coalescing finishes, never inside the model's shell lookups.
        if ans.get("reminder") is not None and ans.get("time_entry") is not None:
            ans.update(reminder=None, time_entry=None, handoff=False,
                       reply="Please give me the reminder and time entry changes one at a time.")
        action_kind = "time entry" if ans.get("time_entry") is not None else "reminder"
        action = ans.get("time_entry") if action_kind == "time entry" else ans.get("reminder")
        action_record = None
        if target or action is not None:
            if target is None:
                target = ('time_entry' if action_kind == 'time entry' else 'reminder', action.get('operation'))
            sid = items[0].get('action_session_id')
            epoch = items[0].get('action_epoch')
            if self.sessions and sid is None:
                current = next((s for s in self.sessions.store.sessions() if s['foreground']), None)
                if current:
                    sid, epoch = current['id'], current['epoch']
            action_record = action_routing.enqueue(conn, items, session_id=sid, epoch=epoch, target=target)
            if action_record['status'] != 'pending':
                # The same accepted request can never become a second write.
                if self.sessions:
                    self.sessions.mark_inputs(conn, items, action_record['session_id'],
                                              error=action_record['reply'] if action_record['status'] in {'failed', 'unconfirmed'} else None)
                return
            if action is not None and not action_routing.supported(target):
                failure = action_routing.unavailable(target)
                ans = {'reply': failure, 'handoff': False, 'route': None}
                action = None
        if action is not None:
            if stop.is_set() or stale_request():
                return
            key = reminders.voice_key(items)
            ans["handoff"] = False
            action_routing.finish(conn, action_record['id'], 'running', 'Executing action; completion not yet confirmed.')
            try:
                adapter = time_entries.TimeEntries() if action_kind == "time entry" else reminders.Reminders()
                result = adapter.execute(action, key)
                ans["reply"] = result["reply"]
                outcome = "confirmed"
                action_status = 'completed'
            except reminders.UncertainError as exc:
                ans['reply'] = str(exc)
                outcome = 'unconfirmed; do not retry automatically'
                action_status = 'unconfirmed'
                failure = ans['reply']
                self.cues.emit('error')
            except reminders.ReminderError as exc:
                ans["reply"] = str(exc)
                outcome = "unconfirmed; do not retry automatically"
                action_status = 'failed'
                failure = ans['reply']
                self.cues.emit("error")
            except (OSError, sqlite3.Error):
                ans["reply"] = f"I couldn't confirm that {action_kind} change. Check Stoic before trying again."
                outcome = "unconfirmed; local receipt unavailable; do not retry automatically"
                action_status = 'unconfirmed'
                failure = ans['reply']
                self.cues.emit("error")
            action_routing.finish(conn, action_record['id'], action_status, ans['reply'])
            # Save the action receipt independently of playback. If speaking
            # fails or a pause begins, triage must not repeat a completed write.
            note = (f"[{stamp(time.time())}] (Ramble {action_kind} action {key}: {outcome}; "
                    f"{ans['reply']} [{action_kind} handled: never dispatch or repeat this request])")
            tids = {row[0] for item in items for row in conn.execute(
                "SELECT thought_id FROM segments WHERE id=? AND thought_id IS NOT NULL", (item["seg"]["id"],))}
            for tid in tids:
                conn.execute("UPDATE thoughts SET text=text || char(10) || ? WHERE id=?", (note, tid))
        elif action_record:
            action_routing.finish(conn, action_record['id'], 'failed' if failure else 'needs_input', ans['reply'])
        routed_sid = None
        if self.sessions:
            if action is None and ans.get('route'):
                try:
                    routed_sid, ans['reply'] = self.sessions.route(
                        ans['route'], text, items, conversation=conversation,
                    )
                except (ValueError, OSError, sqlite3.Error) as exc:
                    ans['reply'] = str(exc)
                    failure = ans['reply']
                ans['handoff'] = False
            elif action is None and ans.get('handoff'):
                # An older classifier may only know handoff. Preserve the default
                # of collaborative work in the durable session path.
                try:
                    routed_sid, ans['reply'] = self.sessions.route(
                        {'action':'work'}, text, items, conversation=conversation,
                    )
                except (ValueError, OSError, sqlite3.Error) as exc:
                    ans['reply'] = str(exc)
                    failure = ans['reply']
                ans['handoff'] = False
            self.sessions.mark_inputs(conn, items, routed_sid, error=failure)
        reply = ans["reply"].strip()
        work_token = self.cues.response_token(work_token, [i['seg']['started_at'] for i in items])
        latency.event("reply_ready", segment_ids=[i["seg"]["id"] for i in items])
        log.info("fast reply in %.1fs (handoff=%s): %s", time.time() - t0, ans["handoff"], reply)
        log.info("reply ready %.1fs after he stopped talking", time.time() - seg["ended_at"])
        spoken, err = True, None
        speech_job_id = None
        if self.sessions and self.sessions.speech:
            tid = conn.execute('SELECT thought_id FROM segments WHERE id=?', (seg['id'],)).fetchone()
            job_id = 'reply-' + reminders.voice_key(items)
            # Bind before enqueue: a speech worker may pick up the row at once.
            self.cues.wait_for_speech(work_token, job_id)
            speech_job_id = self.sessions.speech.enqueue(reply, thought_id=tid[0] if tid else None,
                                         job_id=job_id,
                                         since=items[0]['seg']['started_at'])
            spoken = None
        elif self.cfg.get("fast_speak", True) and state.audio_allowed(items[0]['seg']['started_at']):
            try:
                from . import speech

                speech.speak(reply, self.cfg, on_playback_start=lambda: self.cues.end_work(work_token))
                log.info("finished speaking %.1fs after he stopped talking", time.time() - seg["ended_at"])
            except state.PausedError:
                log.info("fast reply cancelled by pause")
                return
            except Exception as e:  # noqa: BLE001
                spoken, err = False, str(e)[:200]
                self.cues.emit("error")
                log.exception("could not speak fast reply")
        elif not state.audio_allowed(items[0]['seg']['started_at']):
            spoken, err = False, "Voice responses are muted or this reply was cancelled by a voice switch"
        self.history.append((seg["started_at"], "the user", text))
        self.history.append((time.time(), "Ramble", reply))
        if self.sessions:
            if routed_sid or failure:
                chat_followup.clear(conn)
            else:
                chat_followup.save(conn, self.cfg, items[0]['seg']['id'],
                                   (conversation + self.recent_conversation()[-2:])[-8:], speech_job_id)
        # Leave a trace in the thought so the slow lane knows what actually reached him.
        tid = conn.execute("SELECT thought_id FROM segments WHERE id=?", (seg["id"],)).fetchone()
        if tid and tid[0]:
            handoff = " [handoff: dispatch this]" if ans["handoff"] else ""
            if spoken is None:
                note = f"[{stamp(time.time())}] (Ramble reply queued, delivery not yet confirmed: {reply})"
            elif spoken:
                note = f"[{stamp(time.time())}] (Ramble replied: {reply}{handoff})"
            else:
                note = f"[{stamp(time.time())}] (Ramble's spoken reply FAILED, he did not hear it: {reply}{handoff}; error: {err})"
            conn.execute("UPDATE thoughts SET text=text || char(10) || ? WHERE id=?", (note, tid[0]))
