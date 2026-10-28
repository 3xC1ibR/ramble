"""Persistent work with a fake native owner. No workers, mic or audio are started."""
import json
import subprocess
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ramble import config, db, input_receipts, state, model_settings
from ramble.fastlane import FastLane, REPLY_SCHEMA
from ramble.router import Router
from ramble.sessions import Sessions
from ramble.session_store import SessionStore
from ramble.speech_queue import SpeechQueue


class FakeBackend:
    def __init__(self):
        self.receipts = {}
        self.calls = []
        self.created = 0
        self.lose_response = False
        self.live = {
            "connected": True,
            "status": "running",
            "active_turn_id": "turn-1",
            "last_turn_id": "turn-1",
            "events": [],
            "cursor": 0,
        }

    def create(self, key, payload):
        if key not in self.receipts:
            self.created += 1
            self.calls.append(("create", key, payload))
            self.receipts[key] = {
                "status": "completed",
                "thread_id": "thread-1",
                "result": {},
            }
        if self.lose_response:
            self.lose_response = False
            raise OSError("lost response")
        return self.receipts[key]

    def command(self, key, thread, action, **kw):
        if key not in self.receipts:
            self.calls.append((action, key, kw))
            self.receipts[key] = {
                "status": "completed",
                "thread_id": thread,
                "result": {"accepted": True},
            }
            if action == "interrupt":
                self.live.update(status="interrupted", active_turn_id=None)
            if action == "submit":
                self.live.update(
                    status="running",
                    active_turn_id=self.live.get("active_turn_id") or "turn-2",
                )
        return self.receipts[key]

    def status(self, key):
        return self.receipts[key]

    def cancel(self, key):
        self.receipts[key]["status"] = "cancelled"
        return self.receipts[key]

    def snapshot(self, thread, after):
        return {
            **self.live,
            "events": [e for e in self.live["events"] if e["seq"] > after],
        }


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="ramble-test-sessions-", dir="/tmp"
        )
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "digest").mkdir()
        for k, v in {
            "HOME": self.home,
            "DB_PATH": self.home / "ramble.db",
            "STATE_PATH": self.home / "state.json",
            "TRIAGE_DIR": self.home / "triage",
            "DIGEST_DIR": self.home / "digest",
        }.items():
            self.enterContext(patch.object(config, k, v))
        (self.home / "config.json").write_text(
            '{"dispatch_daily_cap":0,"fast_speak":false,"speak_questions":false}'
        )
        self.cfg = dict(
            config.DEFAULTS,
            dispatch_daily_cap=1,
            fast_speak=False,
            speak_questions=False,
            audio_cues_enabled=False,
            work_session_root=str(self.home),
        )
        self.store = SessionStore(self.home)
        self.backend = FakeBackend()
        self.sessions = Sessions(self.cfg, self.backend, self.store)
        self.sessions.speech = SpeechQueue(self.cfg, self.store)

    def items(self, identifier="input-1", text="Investigate this with me"):
        return [
            {
                "seg": {
                    "id": identifier,
                    "started_at": time.time() - 2,
                    "ended_at": time.time() - 1,
                },
                "text": text,
                "why": "named",
            }
        ]

    def start(self, kind="work"):
        sid, _ = self.sessions.route(
            {"action": kind, "cwd": str(self.home), "name": "Investigation"},
            "Investigate this with me",
            self.items(),
        )
        self.sessions.tick()
        return sid

    def test_create_replay_and_followup_keep_one_owner(self):
        sid = self.start()
        self.sessions.route({"action": "work"}, "duplicate", self.items())
        routed, _ = self.sessions.route(
            {"action": "continue_session", "session_id": sid},
            "Use yesterday",
            self.items("next"),
        )
        self.sessions.tick()
        self.assertEqual(routed, sid)
        self.assertEqual(self.backend.created, 1)
        steer = [c for c in self.backend.calls if c[0] == "submit"][0]
        self.assertEqual(steer[2]["expected"], "turn-1")
        self.assertIn("Use yesterday", steer[2]["message"])

    def test_lost_create_response_reconciles_without_new_worker(self):
        self.backend.lose_response = True
        sid = self.start()
        self.assertIsNone(self.store.get(sid)["thread_id"])
        Sessions(self.cfg, self.backend, SessionStore(self.home)).tick()
        self.assertEqual(self.backend.created, 1)
        self.assertEqual(self.store.get(sid)["thread_id"], "thread-1")

    def test_zero_cap_and_unknown_owner_cannot_start(self):
        self.sessions.cfg["dispatch_daily_cap"] = 0
        with self.assertRaisesRegex(ValueError, "limit"):
            self.start()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.sessions.route(
                {"action": "continue_session", "session_id": "invented"},
                "go",
                self.items(),
            )
        self.assertEqual(self.backend.created, 0)

    def test_delegated_task_stays_with_owner(self):
        sid = self.start("delegate")
        self.assertEqual(self.store.get(sid)["kind"], "delegated")
        self.assertFalse(self.store.get(sid)["foreground"])
        self.sessions.route(
            {"action": "continue_session", "session_id": sid},
            "Add a comparison",
            self.items("next"),
        )
        self.sessions.tick()
        self.assertEqual(self.backend.created, 1)

    def saved_answer(self, sid, text="[say]Your brief is ready.[/say]\nElections, semiconductors, data centers and AI."):
        with self.store.connect() as c:
            self.store.event(c, sid, "saved-final", "message", text, {"phase": "final_answer"})

    def test_new_session_receives_prior_goal_and_deliverable_context(self):
        sid = self.start("delegate")
        self.saved_answer(sid)
        self.cfg["dispatch_daily_cap"] = 2
        new_sid, _ = self.sessions.route(
            {"action": "delegate", "name": "Morning brief in new session"},
            "Put that in a new agent session.", self.items("new-session"),
        )
        with self.store.connect() as c:
            payload = json.loads(c.execute("SELECT payload FROM session_commands WHERE session_id=?", (new_sid,)).fetchone()[0])
        self.assertIn("Elections, semiconductors, data centers and AI", payload["prompt"])
        self.assertIn(sid, payload["prompt"])
        self.assertIn("Put that in a new agent session.", payload["prompt"])
        self.assertTrue(next(s for s in self.store.context() if s["id"] == sid)["has_saved_result"])

    def test_spoken_model_request_creates_separate_voice_owner_and_survives_restart(self):
        old = self.start()
        self.cfg['dispatch_daily_cap'] = 2
        fast = FastLane(self.cfg)
        fast.sessions = self.sessions
        models = [{'id': 'gpt-6-astra', 'label': 'GPT-6-Astra',
                   'efforts': ['high', 'max', 'ultra'], 'is_default': True}]
        text = 'Ramble, start a new session with GPT Astra ultra'
        with patch.object(model_settings, 'catalog', return_value=models):
            with self.store.connect() as c:
                prompt = fast.build_prompt(c, text)
            self.assertIn('gpt-6-astra', prompt)
            self.assertIn('"ultra"', prompt)
            self.fast_request(fast, 'astra-ultra', text, {
                'reply': 'Opening that session.', 'handoff': False,
                'route': {'action': 'work', 'session_id': None, 'cwd': None,
                          'name': 'Astra Ultra', 'model': 'gpt-6-astra', 'effort': 'ultra'},
            })
        new = next(s for s in self.store.sessions() if s['foreground'])
        self.assertNotEqual(old, new['id'])
        self.assertFalse(self.store.get(old)['foreground'])
        # Change defaults after queueing: replay must keep the spoken pair.
        (self.home / 'config.json').write_text(json.dumps({
            'dispatch_daily_cap': 0, 'fast_speak': False, 'speak_questions': False,
            'work_session_model': 'different-model', 'work_session_effort': 'low',
        }))
        Sessions(self.cfg, self.backend, SessionStore(self.home)).tick()
        payload = [call[2] for call in self.backend.calls if call[0] == 'create'][-1]
        self.assertEqual((payload['model'], payload['effort']), ('gpt-6-astra', 'ultra'))
        self.assertIn('live voice mode', payload['prompt'])
        self.assertIn('30–60 seconds', payload['prompt'])
        self.assertIn('[say]', payload['prompt'])
        self.assertIn(text, payload['prompt'])
        self.assertEqual(self.backend.created, 2)

    def test_invalid_model_pair_leaves_no_session_or_launch_receipt(self):
        models = [{'id': 'test-model', 'efforts': ['max'], 'is_default': True}]
        with patch.object(model_settings, 'catalog', return_value=models):
            with self.assertRaisesRegex(ValueError, 'not supported'):
                self.sessions.route({'action': 'work', 'model': 'test-model', 'effort': 'ultra'},
                                    'Start a new session', self.items())
        self.assertEqual(self.store.sessions(), [])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM session_launches').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)

    def test_existing_session_cannot_silently_ignore_model_selection(self):
        sid = self.start()
        with self.assertRaisesRegex(ValueError, 'new session'):
            self.sessions.route({'action': 'continue_session', 'session_id': sid,
                                 'model': 'gpt-6-astra', 'effort': 'max'},
                                'Use Astra max', self.items('model-change'))
        self.assertEqual(self.backend.created, 1)

    def test_work_route_schema_stays_strict(self):
        schema = REPLY_SCHEMA['properties']['route']
        self.assertEqual(set(schema['properties']), set(schema['required']))
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(schema['properties']['model']['type'], ['string', 'null'])
        self.assertEqual(schema['properties']['effort']['type'], ['string', 'null'])

    def fast_request(self, fast, identifier, text, answer):
        items = self.items(identifier, text)
        with self.store.connect() as c:
            self.sessions.accept_input(c, items[0])
            with patch.object(fast, "run_codex", return_value=answer):
                fast.respond(c, items)

    def test_quick_exchange_reaches_new_owner_after_queue_restart(self):
        fast = FastLane(self.cfg)
        fast.sessions = self.sessions
        first = "Ramble, just respond. I'm checking how long it takes."
        reply = "Here I am. A very elaborate stopwatch."
        self.fast_request(fast, "voice-check", first, {"reply": reply, "handoff": False})
        self.assertEqual(self.store.sessions(), [])
        with self.store.connect() as c:
            c.execute(
                "INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text) VALUES ('ambient',?,?,1,'fake','transcribed','Private unrelated meeting')",
                (time.time() - 2, time.time() - 1),
            )
        current = "Ramble, let's keep talking that through. Ask me questions before changing anything."
        self.fast_request(fast, "discussion", current, {
            "reply": "Let's discuss it.", "handoff": False,
            "route": {"action": "work", "name": "Response latency", "cwd": str(self.home)},
        })
        self.assertEqual(self.backend.created, 0)
        session = self.store.sessions()[0]
        self.assertEqual(session["goal"], current)
        self.assertTrue(session["foreground"])
        self.assertEqual(session["kind"], "interactive")
        fast.history.clear()
        restarted = Sessions(self.cfg, self.backend, SessionStore(self.home))
        restarted.tick()
        created = next(call for call in self.backend.calls if call[0] == "create")
        prompt = created[2]["prompt"]
        self.assertIn(first, prompt)
        self.assertIn(reply, prompt)
        self.assertIn("User request:\n" + current, prompt)
        self.assertIn("reference only", prompt)
        self.assertNotIn("Private unrelated meeting", prompt)
        self.assertNotIn("I'll work through that with you.", prompt)
        restarted.tick()
        self.assertEqual(self.backend.created, 1)
        events = self.store.snapshot(session["id"])["events"]
        self.assertEqual([e["text"] for e in events if e["kind"] == "user"], [current])

    def test_quick_exchange_reaches_existing_owner_without_creating_another(self):
        sid = self.start()
        fast = FastLane(self.cfg)
        fast.sessions = self.sessions
        self.fast_request(fast, "aside", "Ramble, what is the difference between these two options?", {
            "reply": "The first keeps the existing settings. The second changes them.", "handoff": False,
        })
        current = "Let's explore the first option before making changes."
        self.fast_request(fast, "return", current, {
            "reply": "Let's explore it.", "handoff": False,
            "route": {"action": "continue_session", "session_id": sid},
        })
        fast.history.clear()
        Sessions(self.cfg, self.backend, SessionStore(self.home)).tick()
        message = next(call[2]["message"] for call in self.backend.calls if call[0] == "submit")
        self.assertIn("The first keeps the existing settings.", message)
        self.assertIn("the user says:\n" + current, message)
        self.assertEqual(self.backend.created, 1)

    def test_coalesced_transition_uses_final_request_and_router_context_snapshot(self):
        fast = FastLane(dict(self.cfg, fast_coalesce_s=0))
        fast.sessions = self.sessions
        fast.history.extend([
            (time.time() - 4, "the user", "Why does the reply take twenty seconds?"),
            (time.time() - 3, "Ramble", "We can examine the delay."),
        ])
        first = self.items("start-discussion", "Ramble, let's work on that")
        second = self.items("scope", "but only discuss the options for now")[0]
        prompts = []

        def answer(prompt, **kwargs):
            prompts.append(prompt)
            if len(prompts) == 1:
                fast.q.put(second)
            else:
                # The owner must receive the context seen by this decision,
                # even if it is no longer in the in-memory history at routing.
                fast.history.clear()
            return {"reply": "I'll investigate.", "handoff": False, "route": {"action": "work"}}

        with self.store.connect() as c:
            self.sessions.accept_input(c, first[0])
            self.sessions.accept_input(c, second)
            with patch.object(fast, "run_codex", side_effect=answer):
                fast.respond(c, first)
        self.sessions.tick()
        self.assertEqual(len(prompts), 2)
        self.assertEqual(self.backend.created, 1)
        prompt = next(call[2]["prompt"] for call in self.backend.calls if call[0] == "create")
        self.assertIn("Why does the reply take twenty seconds?", prompt)
        self.assertIn("We can examine the delay.", prompt)
        self.assertIn("User request:\nRamble, let's work on that but only discuss the options for now", prompt)
        self.assertNotIn("I'll investigate.", prompt)

    def test_legacy_handoff_carries_chat_context_and_zero_cap_still_blocks_creation(self):
        fast = FastLane(self.cfg)
        fast.sessions = self.sessions
        fast.history.append((time.time() - 2, "the user", "Discuss the response latency with me"))
        self.fast_request(fast, "legacy-discussion", "Ramble, let's work on that", {
            "reply": "Let's work through it.", "handoff": True,
        })
        self.sessions.tick()
        prompt = next(call[2]["prompt"] for call in self.backend.calls if call[0] == "create")
        self.assertIn("Discuss the response latency with me", prompt)
        self.cfg["dispatch_daily_cap"] = 0
        self.fast_request(fast, "blocked-discussion", "Ramble, let's discuss another topic", {
            "reply": "Let's discuss it.", "handoff": False, "route": {"action": "work"},
        })
        self.sessions.tick()
        self.assertEqual(self.backend.created, 1)
        self.assertIn("limit", fast.history[-1][2])

    def test_existing_session_followup_reads_current_visual_policy(self):
        sid = self.start()
        policy_root = self.home / "policy"
        (policy_root / "prompts").mkdir(parents=True)
        (policy_root / "prompts/work-session.md").write_text("New visual guidance for existing owners")
        self.store.control(sid, "message", "visual-followup", message="Show the actual code")
        with patch.object(config, "REPO", policy_root):
            self.sessions.tick()
        submitted = [c[2]["message"] for c in self.backend.calls if c[0] == "submit"]
        self.assertIn("New visual guidance for existing owners", submitted[-1])
        self.assertIn("the user says:\nShow the actual code", submitted[-1])

    def test_explicit_publication_uses_saved_answer_without_launching_work(self):
        sid = self.start("delegate")
        self.saved_answer(sid)
        before = len(self.backend.calls)
        with patch("ramble.sessions.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout='{"ok":true,"revision":"report-1"}')) as publish:
            route = {"action": "publish_result", "session_id": sid}
            result_sid, reply = self.sessions.route(route, "Put the brief in Companion", self.items("show"))
            self.sessions.route(route, "Put the brief in Companion", self.items("show"))
        self.assertEqual(result_sid, sid)
        self.assertIn("saved in Companion", reply)
        self.assertEqual(len(self.backend.calls), before)
        publish.assert_called_once()
        self.assertIn("--thread-id", publish.call_args.args[0])
        self.assertNotIn("--background", publish.call_args.args[0])
        self.assertIn("Elections, semiconductors", publish.call_args.kwargs["input"])
        self.assertNotIn("[say]", publish.call_args.kwargs["input"])
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status,revision FROM session_publications").fetchone()[:], ("published", "report-1"))

    def test_new_final_is_published_once_even_when_speech_is_quiet(self):
        sid = self.start("delegate")
        self.store.control(sid, "stop_talking", "quiet")
        self.backend.live.update(cursor=1, events=[{
            "seq": 1, "kind": "message", "phase": "final_answer", "turn_id": "turn-1",
            "text": "[say]Ready.[/say]\nThe full report.", "recovered": False,
        }])
        with patch("ramble.sessions.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout='{"ok":true,"revision":"auto-report"}')) as publish:
            self.sessions.tick()
            Sessions(self.cfg, self.backend, SessionStore(self.home)).tick()
        publish.assert_called_once()
        self.assertTrue(self.store.get(sid)["quiet"])
        self.assertIn("The full report.", publish.call_args.kwargs["input"])
        self.assertIn("--background", publish.call_args.args[0])

    def test_recovered_or_stopped_final_is_not_automatically_published(self):
        sid = self.start()
        self.backend.live.update(cursor=1, events=[{
            "seq": 1, "kind": "message", "phase": "final_answer", "turn_id": "turn-1",
            "text": "Old answer", "recovered": True,
        }])
        with patch("ramble.sessions.subprocess.run") as publish:
            self.sessions.tick()
            self.store.control(sid, "stop_work", "stop")
            self.backend.live.update(cursor=2, events=[{
                "seq": 2, "kind": "message", "phase": "final_answer", "turn_id": "turn-1",
                "text": "Late answer", "recovered": False,
            }])
            self.sessions.tick()
        publish.assert_not_called()

    def test_lost_publication_receipt_is_not_replayed_after_restart(self):
        sid = self.start()
        self.saved_answer(sid)
        with patch("ramble.sessions.subprocess.run", side_effect=subprocess.TimeoutExpired("publish", 15)) as publish:
            with self.assertRaisesRegex(ValueError, "couldn't confirm"):
                self.sessions.publish_result(sid)
            restarted = Sessions(self.cfg, self.backend, SessionStore(self.home))
            with self.assertRaisesRegex(ValueError, "couldn't confirm"):
                restarted.publish_result(sid)
        publish.assert_called_once()
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM session_publications").fetchone()[0], "unconfirmed")

    def test_restart_between_observation_and_delivery_keeps_unsent_result(self):
        sid = self.start()
        self.backend.live.update(cursor=1, events=[{
            "seq": 1, "kind": "message", "phase": "final_answer", "turn_id": "turn-1",
            "text": "Durable report", "recovered": False,
        }])
        self.sessions._poll(self.store.get(sid))
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM session_publications").fetchone()[0], "pending")
        with patch("ramble.sessions.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout='{"ok":true,"revision":"restarted-report"}')) as publish:
            Sessions(self.cfg, self.backend, SessionStore(self.home)).tick()
        publish.assert_called_once()
        self.assertEqual(publish.call_args.kwargs["input"], "Durable report")

    def test_stop_cancels_unsent_publication(self):
        sid = self.start()
        self.backend.live.update(cursor=1, events=[{
            "seq": 1, "kind": "message", "phase": "final_answer", "turn_id": "turn-1",
            "text": "Do not deliver after Stop", "recovered": False,
        }])
        self.sessions._poll(self.store.get(sid))
        self.store.control(sid, "stop_work", "stop-before-delivery")
        with patch("ramble.sessions.subprocess.run") as publish:
            self.sessions.tick()
        publish.assert_not_called()

    def test_failed_response_is_recorded_and_not_replayed_as_handled_work(self):
        fast = FastLane(self.cfg)
        fast.sessions = self.sessions
        items = self.items("failure", "Ramble, show the morning brief")
        with self.store.connect() as c:
            self.sessions.accept_input(c, items[0])
            with patch.object(fast, "run_codex", side_effect=subprocess.TimeoutExpired("codex", 30)), self.assertLogs("ramble.fast", level="ERROR"):
                fast.respond(c, items)
            self.assertEqual(c.execute("SELECT status FROM session_claims WHERE segment_id='failure'").fetchone()[0], "failed")
            row = c.execute("SELECT status,error FROM session_inputs WHERE segment_id='failure'").fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertIn("timed out", row["error"])
        self.assertEqual(self.sessions.pending_inputs(), [])
        self.assertEqual(self.backend.created, 0)

    def test_stop_cancels_pending_followup_and_survives_restart(self):
        sid = self.start()
        self.sessions.route(
            {"action": "continue_session", "session_id": sid},
            "Unsent",
            self.items("next"),
        )
        self.store.control(sid, "stop_work", "stop-once")
        self.sessions.tick()
        self.assertEqual(self.store.get(sid)["status"], "cancelled")
        self.assertFalse(any(c[0] == "submit" for c in self.backend.calls))
        Sessions(self.cfg, self.backend, self.store).tick()
        self.assertEqual(self.backend.created, 1)
        self.assertEqual(self.store.get(sid)["status"], "cancelled")

    def test_explicit_resume_uses_same_thread_and_drops_old_turn_speech(self):
        sid = self.start()
        self.store.control(sid, "stop_work", "stop")
        self.sessions.tick()
        self.store.control(
            sid, "message", "resume", message="Continue with yesterday instead"
        )
        self.sessions.tick()
        self.assertEqual(self.store.get(sid)["status"], "running")
        self.assertEqual(self.backend.created, 1)
        self.backend.live.update(
            cursor=2,
            events=[
                {
                    "seq": 1,
                    "kind": "message",
                    "turn_id": "turn-1",
                    "text": "[say]Old late output[/say]",
                },
                {
                    "seq": 2,
                    "kind": "message",
                    "turn_id": "turn-2",
                    "text": "[say]New finding[/say]",
                },
            ],
        )
        self.sessions.tick()
        with self.store.connect() as c:
            self.assertEqual(
                [r[0] for r in c.execute("SELECT text FROM session_speech")],
                ["New finding"],
            )

    def test_named_existing_owner_is_adopted_without_creation(self):
        self.backend.settings = lambda thread: {
            "thread_id": thread,
            "name": "Existing investigation",
            "cwd": str(self.home),
            "client": "codex",
            "archived": False,
        }
        sid, _ = self.sessions.route(
            {"action": "continue_session", "session_id": "thread:existing"},
            "Use yesterday",
            self.items(),
        )
        self.sessions.tick()
        self.assertEqual(self.backend.created, 0)
        self.assertEqual(self.store.get(sid)["thread_id"], "existing")
        self.assertTrue(
            any(c[0] == "watch" and c[2].get("adopt") for c in self.backend.calls)
        )

    def test_stop_before_creation_never_launches(self):
        sid, _ = self.sessions.route({"action": "work"}, "task", self.items())
        self.store.control(sid, "stop_work", "stop")
        self.sessions.tick()
        self.assertEqual(self.backend.created, 0)
        self.assertEqual(self.store.get(sid)["status"], "cancelled")

    def test_stop_during_timed_pause_still_interrupts(self):
        sid = self.start()
        state.write(True, time.time() + 100)
        self.store.control(sid, "stop_work", "stop")
        self.sessions.tick()
        self.assertEqual(self.store.get(sid)["status"], "cancelled")

    def test_message_retry_preserves_request_identity_after_submission(self):
        sid = self.start()
        first = self.store.control(sid, "message", "followup", message="Use yesterday")
        self.sessions.tick()
        again = self.store.control(sid, "message", "followup", message="Use yesterday")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(again["status"], "completed")
        with self.assertRaises(ValueError):
            self.store.control(sid, "message", "followup", message="Different")

    def test_milestone_arrives_while_work_keeps_running_and_deduplicates(self):
        sid = self.start()
        self.backend.live.update(
            cursor=1,
            events=[
                {
                    "seq": 1,
                    "kind": "message",
                    "turn_id": "turn-1",
                    "phase": "commentary",
                    "text": "[say]Found the bottleneck.[/say] Details",
                    "recovered": False,
                }
            ],
        )
        self.sessions.tick()
        self.sessions.tick()
        with self.store.connect() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM session_speech").fetchone()[0], 1
            )
        self.assertEqual(self.store.get(sid)["status"], "running")
        self.sessions.speech.tick(threading.Event())
        with self.store.connect() as c:
            self.assertEqual(
                c.execute("SELECT status FROM session_speech").fetchone()[0],
                "suppressed",
            )
        self.assertFalse(any(c[0] == "interrupt" for c in self.backend.calls))

    def test_recovered_messages_are_visible_but_not_spoken(self):
        sid = self.start()
        self.backend.live.update(
            cursor=1,
            events=[
                {
                    "seq": 1,
                    "kind": "message",
                    "text": "[say]Old answer[/say]",
                    "recovered": True,
                }
            ],
        )
        self.sessions.tick()
        with self.store.connect() as c:
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM session_speech").fetchone()[0], 0
            )
        self.assertTrue(
            any(e["kind"] == "message" for e in self.store.snapshot(sid)["events"])
        )

    def test_stop_talking_does_not_interrupt_work(self):
        sid = self.start()
        self.sessions.speech.enqueue("A finding", session_id=sid, kind="progress")
        self.store.control(sid, "stop_talking", "quiet")
        self.sessions.tick()
        self.assertEqual(self.store.get(sid)["status"], "running")
        with self.store.connect() as c:
            self.assertEqual(
                c.execute("SELECT status FROM session_speech").fetchone()[0],
                "cancelled",
            )
        self.assertFalse(any(c[0] == "interrupt" for c in self.backend.calls))

    def test_thinking_work_tracks_queue_progress_completion_and_restart(self):
        sid, _ = self.sessions.route(
            {"action": "work", "cwd": str(self.home)}, "Investigate", self.items()
        )
        queued = self.sessions.thinking_work()
        self.assertIsNotNone(queued)
        self.sessions.tick()
        self.assertEqual(self.sessions.thinking_work(), queued)
        self.backend.live.update(events=[{
            "seq": 1, "kind": "message", "text": "[say]Still checking.[/say]",
            "phase": "commentary", "turn_id": "turn-1",
        }], cursor=1)
        self.sessions.tick()
        self.assertEqual(self.sessions.thinking_work(), queued)
        restarted = Sessions(self.cfg, self.backend, SessionStore(self.home))
        self.assertEqual(restarted.thinking_work(), queued)
        self.backend.live.update(status="idle", active_turn_id=None)
        self.sessions.tick()
        self.assertIsNone(self.sessions.thinking_work())
        self.sessions.route({"action": "continue_session", "session_id": sid},
                            "Check again", self.items("next"))
        self.assertIsNotNone(self.sessions.thinking_work())
        self.assertNotEqual(self.sessions.thinking_work(), queued)

    def test_thinking_work_stops_for_quiet_stop_focus_failure_and_disconnect(self):
        sid = self.start()
        self.assertIsNotNone(self.sessions.thinking_work())
        for status in ("failed", "uncertain", "disconnected", "stopping", "cancelled"):
            with self.store.connect() as conn:
                conn.execute("UPDATE work_sessions SET status=? WHERE id=?", (status, sid))
            self.assertIsNone(self.sessions.thinking_work(), status)
        with self.store.connect() as conn:
            conn.execute("UPDATE work_sessions SET status='running',connected=0 WHERE id=?", (sid,))
        self.assertIsNone(self.sessions.thinking_work())
        with self.store.connect() as conn:
            conn.execute("UPDATE work_sessions SET connected=1,foreground=0 WHERE id=?", (sid,))
        self.assertIsNone(self.sessions.thinking_work())
        with self.store.connect() as conn:
            conn.execute("UPDATE work_sessions SET foreground=1,error='Connection failed' WHERE id=?", (sid,))
        self.assertIsNone(self.sessions.thinking_work())
        with self.store.connect() as conn:
            conn.execute("UPDATE work_sessions SET error=NULL WHERE id=?", (sid,))
        self.assertIsNotNone(self.sessions.thinking_work())
        self.store.control(sid, "stop_talking", "quiet-cue")
        self.assertIsNone(self.sessions.thinking_work())
        self.store.control(sid, "resume_talking", "resume-cue")
        self.assertIsNotNone(self.sessions.thinking_work())
        self.store.control(sid, "stop_work", "stop-cue")
        self.assertIsNone(self.sessions.thinking_work())

    def test_background_delegation_does_not_start_thinking_pulse(self):
        self.start("delegate")
        self.assertIsNone(self.sessions.thinking_work())

    def test_claimed_input_is_not_sent_to_slow_triage(self):
        with self.store.connect() as c:
            c.execute(
                "INSERT INTO thoughts(id,started_at,ended_at,text) VALUES ('t',1,2,'Owned words and ambient words')"
            )
            for identity, text in [
                ("owned", "Owned words"),
                ("ambient", "Ambient words"),
            ]:
                c.execute(
                    "INSERT INTO segments(id,started_at,ended_at,duration,audio_path,text,thought_id) VALUES (?,1,2,1,?,?,?)",
                    (identity, "fake", text, "t"),
                )
            c.execute(
                "INSERT INTO session_claims(segment_id,status) VALUES ('owned','pending')"
            )
            thought = c.execute("SELECT * FROM thoughts WHERE id='t'").fetchone()
            router = Router(self.cfg, threading.Event())
            prompt = router.build_input(c, thought)
            self.assertIn("Ambient words", prompt)
            self.assertNotIn("Owned words", prompt)
            c.execute(
                "INSERT INTO session_claims(segment_id,status) VALUES ('ambient','handled')"
            )
            with patch.object(
                router,
                "run_triage",
                side_effect=AssertionError("must not classify owned work"),
            ):
                router.triage(c, thought)
                c.execute("UPDATE session_claims SET status='failed' WHERE segment_id='owned'")
                router.triage(c, thought)
            summary = c.execute("SELECT summary FROM thoughts WHERE id='t'").fetchone()[0]
            self.assertIn("failed", summary)
            self.assertIn("may not be done", summary)

    def claimed_thought(self, c, identity, text='Yeah.'):
        c.execute("INSERT INTO thoughts(id,started_at,ended_at,text) VALUES (?,1,2,?)", (identity, text))
        c.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,text,thought_id) "
                  "VALUES (?,1,2,1,'fake',?,?)", (identity, text, identity))
        item = self.items(identity, text)[0]
        self.sessions.accept_input(c, item)
        return item

    def test_pending_thought_becomes_failed_after_response_without_retriage(self):
        with self.store.connect() as c:
            items = [self.claimed_thought(c, 'request', 'Show the exact prompt first.'),
                     self.claimed_thought(c, 'tail', 'Yeah.')]
            router = Router(self.cfg, threading.Event())
            with patch.object(router, 'run_triage', side_effect=AssertionError('must not classify')), \
                 patch.object(router, 'dispatch', side_effect=AssertionError('must not dispatch')):
                for item in items:
                    tid = item['seg']['id']
                    router.triage(c, c.execute('SELECT * FROM thoughts WHERE id=?', (tid,)).fetchone())
                    self.assertEqual(c.execute('SELECT summary FROM thoughts WHERE id=?', (tid,)).fetchone()[0], input_receipts.PENDING)
                self.sessions.mark_inputs(c, items, error='Codex timed out')
            for row in c.execute('SELECT summary,triage_json FROM thoughts'):
                self.assertEqual(row['summary'], input_receipts.FAILED)
                decision = json.loads(row['triage_json'])
                self.assertEqual(decision['summary'], row['summary'])
                self.assertEqual(decision['items'][0]['action'], 'log')
                self.assertIn('No automatic retry', decision['items'][0]['text'])
            self.assertEqual(self.sessions.pending_inputs(), [])
            self.assertEqual(self.backend.calls, [])

    def test_startup_receipt_repair_corrects_legacy_success_without_replaying(self):
        with self.store.connect() as c:
            item = self.claimed_thought(c, 'old')
            self.sessions.mark_inputs(c, [item], error='timeout')
            c.execute("UPDATE thoughts SET status='done',classification='log',summary=?,triage_json=?",
                      ('Spoken request to Ramble, answered directly.',
                       '{"classification":"log","items":[],"summary":"Spoken request to Ramble, answered directly."}'))
            input_receipts.refresh(c)
            self.assertEqual(c.execute('SELECT summary FROM thoughts').fetchone()[0], input_receipts.FAILED)
            before = c.total_changes
            input_receipts.refresh(c)
            self.assertEqual(c.total_changes, before, 'repair must be idempotent')
            self.assertEqual(self.sessions.pending_inputs(), [])
            self.assertEqual(self.backend.calls, [])

    def test_receipts_do_not_claim_work_or_speech_completion(self):
        with self.store.connect() as c:
            item = self.claimed_thought(c, 'queued')
            self.sessions.mark_inputs(c, [item], 'work-owner')
            self.assertEqual(input_receipts.decision(c, 'queued')['summary'], input_receipts.ROUTED)
            self.sessions.mark_inputs(c, [item])
            self.assertEqual(input_receipts.decision(c, 'queued')['summary'], input_receipts.PREPARED)

    def test_input_batch_receipt_is_durable_idempotent_and_atomic(self):
        from ramble.reminders import voice_key
        with self.store.connect() as c:
            items = [self.claimed_thought(c, 'first'), self.claimed_thought(c, 'last')]
            self.sessions.mark_inputs(c, items, 'owner')
            row = c.execute('SELECT * FROM session_input_batches').fetchone()
            self.assertEqual(row['id'], voice_key(items))
            self.assertEqual(json.loads(row['segment_ids']), ['first', 'last'])
            self.assertEqual((row['status'], row['session_id']), ('handled', 'owner'))
            self.sessions.mark_inputs(c, items, error='unconfirmed')
            rows = c.execute('SELECT * FROM session_input_batches').fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0]['status'], rows[0]['session_id']), ('failed', None))
            self.assertEqual(rows[0]['created_at'], row['created_at'])
            with patch.object(input_receipts, 'refresh', side_effect=RuntimeError('write failed')):
                with self.assertRaises(RuntimeError):
                    self.sessions.mark_inputs(c, items, 'other-owner')
            self.assertEqual(c.execute('SELECT status FROM session_input_batches').fetchone()[0], 'failed')
            self.assertEqual([r[0] for r in c.execute('SELECT status FROM session_claims')], ['failed', 'failed'])

    def test_receipt_repair_preserves_mixed_thoughts_and_real_triage(self):
        with self.store.connect() as c:
            for tid, summary in [('mixed', input_receipts.PENDING), ('custom', 'Keep this idea.'),
                                 ('dispatch', input_receipts.PENDING), ('candidate', input_receipts.PENDING)]:
                self.claimed_thought(c, tid)
                c.execute("UPDATE thoughts SET status='done',classification='log',summary=? WHERE id=?", (summary, tid))
            c.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,thought_id) "
                      "VALUES ('ambient',1,2,1,'fake','mixed')")
            c.execute("INSERT INTO dispatches(id,thought_id,created_at,name,cwd,prompt,status) "
                      "VALUES ('d','dispatch',1,'Test','/tmp','Test','created')")
            c.execute("UPDATE session_claims SET status='candidate' WHERE segment_id='candidate'")
            before = c.total_changes
            input_receipts.refresh(c)
            self.assertEqual(c.total_changes, before)

    def test_progress_playback_does_not_arm_ambient_followups(self):
        now = time.time()
        with self.store.connect() as c:
            c.execute(
                "INSERT INTO speech_playbacks VALUES (?,?,?,?)",
                ("progress", now - 4, now - 1, "fake"),
            )
            c.execute("INSERT INTO speech_origins VALUES ('progress','work')")
            fast = FastLane(self.cfg)
            self.assertFalse(fast._in_followup(c, now))
            c.execute("UPDATE speech_origins SET origin='reply'")
            self.assertTrue(fast._in_followup(c, now))

    def test_schema_requires_every_property_and_forbids_extra_fields(self):
        def check(schema):
            if "properties" in schema:
                self.assertEqual(set(schema["properties"]), set(schema["required"]))
                self.assertFalse(schema["additionalProperties"])
                for child in schema["properties"].values():
                    check(child)

        check(REPLY_SCHEMA)
