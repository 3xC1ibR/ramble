"""Transcript-to-native steering, with no workers, microphone or speaker output."""
import json
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import test_work_followup as followup
from ramble.session_backend import SessionBackend
from ramble.sessions import Sessions


class VoiceSteeringTests(unittest.TestCase):
    setUp = followup.WorkFollowupTests.setUp
    transcribe = followup.WorkFollowupTests.transcribe

    def native(self, *, active="thinking-turn", receipt_status="completed"):
        self.conn.execute(
            "UPDATE work_sessions SET status=?,active_turn_id=?",
            ("running" if active else "idle", active),
        )
        # No assistant reply is needed to establish conversation ownership.
        self.conn.execute("UPDATE session_events SET kind='user',payload='{}'")
        self.backend = SessionBackend(self.cfg)
        self.backend.caller = "throwaway-dispatcher"
        self.sessions.backend = self.backend
        self.native_calls = []
        self.receipts = {}

        def cli(args, **kwargs):
            self.assertEqual(args[0], "agent-coord")
            self.native_calls.append((args, kwargs))
            if args[1:3] == ["thread", "request-status"]:
                result = self.receipts[args[args.index("--request-id") + 1]]
            else:
                self.assertEqual(args[1], "session-control")
                self.assertEqual(args[args.index("--thread") + 1], "native-owner")
                self.assertEqual(args[args.index("--from-session") + 1], "throwaway-dispatcher")
                if args[2] == "snapshot":
                    result = {
                        "connected": True, "status": "running" if active else "idle",
                        "active_turn_id": active, "last_turn_id": active,
                        "events": [], "cursor": 0,
                    }
                else:
                    self.assertIn(args[2], {"watch", "submit"})
                    result = {
                        "status": receipt_status if args[2] == "submit" else "completed",
                        "thread_id": "native-owner", "result": {"accepted": True},
                    }
                    self.receipts[args[args.index("--request-id") + 1]] = result
            return subprocess.CompletedProcess(args, 0, json.dumps(result), "")

        self.enterContext(patch("ramble.session_backend.subprocess.run", side_effect=cli))
        self.sessions.speech = Mock()

    def submits(self):
        return [call for call in self.native_calls if call[0][1:3] == ["session-control", "submit"]]

    def direct(self, text):
        self.transcribe(text)
        item = self.fast.q.get_nowait()
        with patch.object(self.fast, "run_codex", side_effect=AssertionError("No routing model")):
            self.fast.respond(self.conn, [item], threading.Event())
        return item

    def test_named_correction_steers_before_first_reply_during_responder_cooldown(self):
        self.native()
        self.fast.unavailable_until = float("inf")
        text = "Ramble, actually focus on the tests first."
        self.direct(text)
        self.sessions.tick()
        args, kwargs = self.submits()[0]
        self.assertEqual(len(self.submits()), 1)
        self.assertEqual(args[args.index("--expected-turn-id") + 1], "thinking-turn")
        self.assertEqual(args[args.index("--message") + 1], "-")
        self.assertTrue(kwargs["input"].endswith("the user says:\n" + text))
        self.sessions.speech.enqueue.assert_not_called()
        self.assertEqual(self.store.get("owner")["active_turn_id"], "thinking-turn")
        self.sessions.tick()
        self.assertEqual(len(self.submits()), 1)

    def test_unnamed_correction_requires_admission_then_steers_same_turn(self):
        self.native()
        text = "Actually, keep the existing design."
        self.transcribe(text)
        candidate = self.fast.candidates.get_nowait()
        self.sessions.tick()
        self.assertEqual(self.submits(), [])
        with patch.object(self.fast, "run_codex", return_value={"accept": True}) as admission:
            self.fast.respond_candidate(self.conn, candidate, threading.Event())
        self.assertTrue(admission.call_args.kwargs["read_only"])
        self.sessions.tick()
        args, kwargs = self.submits()[0]
        self.assertEqual(args[args.index("--expected-turn-id") + 1], "thinking-turn")
        self.assertTrue(kwargs["input"].endswith("the user says:\n" + text))

    def test_idle_followup_continues_same_conversation_without_turn_guard(self):
        self.native(active=None)
        self.direct("Ramble, use yesterday's numbers instead.")
        self.sessions.tick()
        self.assertEqual(len(self.submits()), 1)
        self.assertNotIn("--expected-turn-id", self.submits()[0][0])

    def test_restart_reconciles_queued_steer_without_resubmitting(self):
        self.native(receipt_status="queued")
        self.direct("Ramble, keep the existing design.")
        self.sessions.tick()
        args, _ = self.submits()[0]
        request = args[args.index("--request-id") + 1]
        command = self.conn.execute("SELECT * FROM session_commands WHERE id=?", (request,)).fetchone()
        self.assertEqual(command["status"], "submitted")
        self.assertEqual(json.loads(command["native_payload"])["expected"], "thinking-turn")
        self.receipts[request]["status"] = "completed"
        restored = Sessions(self.cfg, self.backend, self.store)
        restored.tick()
        self.assertEqual(len(self.submits()), 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM session_commands WHERE id=?", (request,),
        ).fetchone()[0], "completed")


if __name__ == "__main__":
    unittest.main()
