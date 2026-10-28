"""Routing regressions from captured requests; no mic, playback, or agents."""

import sqlite3
import subprocess
import unittest
import threading
import queue
from unittest.mock import ANY, patch

from ramble import config, db
from ramble.fastlane import FastLane


class FastLaneTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        self.fast = FastLane(dict(config.DEFAULTS, fast_speak=False, dispatch_daily_cap=0))

    def offer(self, text, start=100, end=103):
        return self.fast.offer(self.conn, {"id": str(start), "started_at": start, "ended_at": end}, text)

    def test_observed_name_variants_trigger_reply(self):
        for name in ("Ramble", "Rambo", "Bramble", "Gramble", "Grable", "Granville", "Crabble", "Grambles", "Scramble", "Mr. Abel", "Rabble"):
            with self.subTest(name=name):
                self.fast = FastLane(self.fast.cfg)
                self.assertTrue(self.offer(f"{name}, how many agents are running right now?"))
                self.assertEqual(self.fast.q.get_nowait()["why"], "named")

    def test_saved_companion_request_triggers_reply(self):
        self.assertTrue(self.offer("Grable, can you update the companion app and show me something interesting? I just want to validate it on my iPad."))

    def test_device_recording_is_addressed_without_a_wake_name(self):
        segment = {"id": "device", "started_at": 100, "ended_at": 103, "source": "companion"}
        with patch.object(self.fast.cues, 'emit') as cue:
            self.assertTrue(self.fast.offer(self.conn, segment, 'Show me the latest report'))
        self.assertEqual(self.fast.q.get_nowait()['why'], 'companion')
        cue.assert_not_called()

    def test_continuation_before_first_spoken_reply(self):
        self.assertTrue(self.offer("Ramble, is there any ElevenLabs"))
        self.assertTrue(self.offer("If so, are you able to determine", 107, 110))
        self.fast.q.get_nowait()
        self.assertEqual(self.fast.q.get_nowait()["why"], "continuation")
        self.assertTrue(self.offer("Do we have an API key?", 117, 120))

    def test_continuation_window_expires(self):
        self.offer("Ramble, tell me the time")
        self.assertFalse(self.offer("Unrelated speech", 112, 114))

    def test_bare_name_waits_for_request(self):
        self.assertFalse(self.offer("Grable?"))
        self.assertTrue(self.fast.q.empty())
        self.assertTrue(self.offer("Show me something interesting", 107, 110))

    def test_observed_rabble_wake_waits_for_request(self):
        self.assertFalse(self.offer("Hey, Rabble."))
        self.assertTrue(self.fast.q.empty())
        self.assertTrue(self.offer("Tell me a joke", 107, 110))
        self.assertEqual(self.fast.q.get_nowait()["why"], "continuation")

    def test_rabble_as_ordinary_noun_is_not_an_address(self):
        self.assertFalse(self.offer("The rabble gathered outside."))
        self.assertTrue(self.fast.q.empty())

    def test_single_word_request_is_not_mistaken_for_bare_name(self):
        self.assertTrue(self.offer("Ramble, memory?"))
        self.assertFalse(self.fast.q.empty())

    def test_unaddressed_speech_stays_passive(self):
        for text in ("I am rambling about my day", "Please scramble the eggs", "Show me something interesting"):
            self.assertFalse(self.offer(text))
        self.assertTrue(self.fast.q.empty())

    def test_spoken_followup_still_works(self):
        self.conn.execute("INSERT INTO speech_playbacks VALUES ('reply',90,98,'unused.wav')")
        self.assertTrue(self.offer("On my iPad, please"))
        self.assertEqual(self.fast.q.get_nowait()["why"], "follow-up")

    def test_disabled_fast_lane_does_not_queue(self):
        self.fast.cfg["fast_enabled"] = False
        self.assertFalse(self.offer("Ramble, tell me the time"))

    def test_request_is_acknowledged_once_across_continuations(self):
        with patch.object(self.fast.cues, "emit") as cue:
            self.offer("Ramble, do you have memory?")
            self.offer("Do you remember prior requests?", 106, 109)
            cue.assert_called_once_with("accepted")

    def test_model_response_is_updated_if_request_continues(self):
        self.fast.cfg.update(fast_speak=True, fast_coalesce_s=.001)
        first = {"seg": {"id": "first", "started_at": 100, "ended_at": 103}, "text": "Ramble, can you show me"}
        second = {"seg": {"id": "second", "started_at": 104, "ended_at": 107}, "text": "the page on my iPad?"}
        self.fast.offer(self.conn, first['seg'], first['text'])
        self.fast.q.get_nowait()
        prompts = []
        def answer(prompt, **kwargs):
            self.assertTrue(self.fast.cues.work_active())
            prompts.append(prompt)
            if len(prompts) == 1:
                self.fast.offer(self.conn, second['seg'], second['text'])
                return {"reply": "Show you what?", "handoff": False}
            return {"reply": "Here is the page.", "handoff": False}
        def start_playback(*args, **kwargs):
            self.assertTrue(self.fast.cues.work_active())
            kwargs['on_playback_start']()
            self.assertFalse(self.fast.cues.work_active())
        with patch.object(self.fast, "run_codex", side_effect=answer), \
                patch("ramble.speech.speak", side_effect=start_playback) as speak:
            self.fast.respond(self.conn, [first])
        self.assertEqual(len(prompts), 2)
        self.assertIn("Ramble, can you show me the page on my iPad?", prompts[-1])
        speak.assert_called_once_with("Here is the page.", self.fast.cfg, on_playback_start=ANY)
        self.assertNotIn("Show you what?", str(list(self.fast.history)))
        self.assertFalse(self.fast.cues.work_active())

    def test_working_sound_runs_during_generation_and_stops_before_reply(self):
        self.fast.cfg["fast_speak"] = True
        item = {"seg": {"id": "first", "started_at": 100, "ended_at": 103},
                "text": "Ramble, tell me the time"}
        def answer(*args, **kwargs):
            self.assertTrue(self.fast.cues.work_active())
            return {"reply": "It is noon.", "handoff": False}
        def speak(*args, **kwargs):
            self.assertTrue(self.fast.cues.work_active())  # Synthesis can still take time.
            kwargs['on_playback_start']()
            self.assertFalse(self.fast.cues.work_active())
        with patch.object(self.fast, "run_codex", side_effect=answer), \
                patch("ramble.speech.speak", side_effect=speak) as speaker:
            self.fast.respond(self.conn, [item])
        speaker.assert_called_once()
        self.assertFalse(self.fast.cues.work_active())

    def test_working_sound_stops_for_cancellation_and_unexpected_error(self):
        from ramble.codex_runner import CancelledError
        self.fast.cfg["fast_speak"] = True
        item = {"seg": {"id": "first", "started_at": 100, "ended_at": 103},
                "text": "Ramble, tell me the time"}
        with patch.object(self.fast, "run_codex", side_effect=CancelledError("stopped")), \
                patch("ramble.speech.speak") as speaker:
            self.fast.respond(self.conn, [item])
        speaker.assert_not_called()
        self.assertFalse(self.fast.cues.work_active())
        with patch.object(self.fast, "_respond", side_effect=ValueError("unexpected")):
            with self.assertRaises(ValueError):
                self.fast.respond(self.conn, [item])
        self.assertFalse(self.fast.cues.work_active())

    def test_passive_candidate_worker_never_arms_working_sound(self):
        self.fast.cfg["fast_speak"] = True
        self.fast.candidates.put({"text": "Background comment"})
        stop = threading.Event()
        def classify(*args):
            self.assertFalse(self.fast.cues.work_active())
            stop.set()
        with patch("ramble.fastlane.db.connect", return_value=self.conn), \
                patch.object(self.fast, "respond_candidate", side_effect=classify), \
                patch.object(self.fast.cues, "start_work") as start:
            self.fast.run_forever(stop)
        start.assert_not_called()

    def test_collection_waits_for_active_speech(self):
        self.fast.cfg["fast_coalesce_s"] = 0
        with patch.object(self.fast, "speech_active", side_effect=[True, False]) as active:
            self.fast.collect(self.conn, [], threading.Event())
        self.assertEqual(active.call_count, 2)

    def collect_with_clock(self, ended_at, *, on_poll=None):
        elapsed = [0.0]
        polls = []
        def poll(timeout):
            elapsed[0] += timeout
            polls.append(elapsed[0])
            if on_poll:
                item = on_poll(elapsed[0])
                if item:
                    return item
            raise queue.Empty
        item = {'seg': {'id': 'first', 'started_at': ended_at - 2, 'ended_at': ended_at}}
        with patch('ramble.fastlane.time.time', side_effect=lambda: 1000 + elapsed[0]), \
                patch('ramble.fastlane.time.monotonic', side_effect=lambda: 100 + elapsed[0]), \
                patch.object(self.fast.q, 'get', side_effect=poll):
            items = self.fast.collect(self.conn, [item], threading.Event())
        return elapsed[0], polls, items

    def test_slow_transcription_has_already_satisfied_collection_silence(self):
        elapsed, polls, _ = self.collect_with_clock(987)
        self.assertEqual(polls, [])
        self.assertEqual(elapsed, 0)

    def test_collection_waits_only_for_remaining_silence(self):
        elapsed, _, _ = self.collect_with_clock(999.5)
        self.assertAlmostEqual(elapsed, .7, delta=.11)

    def test_new_voice_activity_extends_silence_without_a_transcript_yet(self):
        self.fast.last_voice_mono = lambda: 99.9
        elapsed, _, _ = self.collect_with_clock(987)
        self.assertAlmostEqual(elapsed, 1.1, delta=.11)

    def test_pending_transcription_still_blocks_an_expired_deadline(self):
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES ('pending',990,991,1,'unused')")
        def complete(elapsed):
            if elapsed >= .3:
                self.conn.execute("UPDATE segments SET status='transcribed'")
                return {'seg': {'id': 'second', 'started_at': 992, 'ended_at': 993}}
        elapsed, _, items = self.collect_with_clock(987, on_poll=complete)
        self.assertAlmostEqual(elapsed, .3)
        self.assertEqual([i['seg']['id'] for i in items], ['first', 'second'])

    def test_future_import_timestamp_does_not_create_an_unbounded_wait(self):
        elapsed, _, _ = self.collect_with_clock(2000)
        self.assertAlmostEqual(elapsed, 1.2, delta=.11)

    def test_recent_unaddressed_speech_is_available_for_memory(self):
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text) VALUES ('heard',?,?,1,'unused','transcribed','Mr. Abel, do you remember prior requests?')", (999, 1000))
        with patch("ramble.fastlane.time.time", return_value=1001):
            prompt = self.fast.build_prompt(self.conn, "Ramble, did you get that?")
        self.assertIn("Mr. Abel, do you remember prior requests?", prompt)

    def test_conversation_snapshot_excludes_expired_and_future_messages(self):
        self.fast.history.extend([
            (400, "the user", "An old topic"),
            (998, "the user", "I'm checking the latency"),
            (999, "Ramble", "A very elaborate stopwatch"),
            (1001, "the user", "A later request"),
        ])
        with patch("ramble.fastlane.time.time", return_value=1000):
            conversation = self.fast.recent_conversation()
            prompt = self.fast.build_prompt(self.conn, "Let's talk that through", conversation=conversation)
        self.assertEqual([entry["text"] for entry in conversation], [
            "I'm checking the latency", "A very elaborate stopwatch",
        ])
        self.assertIn("I'm checking the latency", prompt)
        self.assertNotIn("An old topic", prompt)
        self.assertNotIn("A later request", prompt)

    def test_conversation_snapshot_is_bounded_and_independent_of_later_history(self):
        self.fast.history.extend((990 + n, "the user", f"message-{n}: " + "x" * 5000) for n in range(10))
        with patch("ramble.fastlane.time.time", return_value=1000):
            conversation = self.fast.recent_conversation()
        self.fast.history.clear()
        self.assertEqual(len(conversation), 8)
        self.assertTrue(conversation[0]["text"].startswith("message-2:"))
        self.assertTrue(all(len(entry["text"]) <= 4000 for entry in conversation))

    def test_model_failure_emits_error_cue(self):
        item = {"seg": {"id": "first", "started_at": 100, "ended_at": 103}, "text": "Ramble, tell me the time"}
        with patch.object(self.fast, "run_codex", side_effect=RuntimeError("offline")), patch.object(self.fast.cues, "emit") as cue, self.assertLogs("ramble.fast", level="ERROR"):
            self.fast.respond(self.conn, [item])
        cue.assert_called_once_with("error")

    def test_timeout_does_not_restart_for_each_later_utterance(self):
        first = {"seg": {"id": "first", "started_at": 100, "ended_at": 103}, "text": "Ramble, show the brief"}
        second = {"seg": {"id": "second", "started_at": 104, "ended_at": 107}, "text": "Ramble, did you hear me?"}

        def timeout(prompt, **kwargs):
            self.fast.q.put(second)
            raise subprocess.TimeoutExpired("codex", 30)

        with patch.object(self.fast, "run_codex", side_effect=timeout) as model, self.assertLogs("ramble.fast", level="ERROR"):
            self.fast.respond(self.conn, [first])
            self.assertEqual(self.fast.q.qsize(), 1)
            self.fast.respond(self.conn, [self.fast.q.get_nowait()])
        model.assert_called_once()
        replies = [text for _, who, text in self.fast.history if who == "Ramble"]
        self.assertEqual(len(replies), 2)
        self.assertTrue(all("timed out" in reply and "couldn't confirm" in reply for reply in replies))

    def test_rate_limit_is_explained_without_exposing_process_output(self):
        item = {"seg": {"id": "first", "started_at": 100, "ended_at": 103}, "text": "Ramble, hello"}
        error = subprocess.TimeoutExpired("codex", 30, stderr=b"secret-data: 429 Too Many Requests")
        with patch.object(self.fast, "run_codex", side_effect=error), self.assertLogs("ramble.fast", level="ERROR"):
            self.fast.respond(self.conn, [item])
        reply = self.fast.history[-1][2]
        self.assertIn("rate limited", reply)
        self.assertNotIn("secret-data", reply)

    def test_requests_resume_after_failure_cooldown(self):
        item = {"seg": {"id": "first", "started_at": 100, "ended_at": 103}, "text": "Ramble, hello"}
        self.fast.unavailable_until = 99
        with patch("ramble.fastlane.time.monotonic", return_value=100), patch.object(
            self.fast, "run_codex", return_value={"reply": "Hello.", "handoff": False}
        ) as model:
            self.fast.respond(self.conn, [item])
        model.assert_called_once()
        self.assertEqual(self.fast.history[-1][2], "Hello.")

    def test_server_retry_delay_prevents_new_model_calls(self):
        from ramble.codex_runner import RateLimitedError
        item = {"seg": {"id": "first", "started_at": 100, "ended_at": 103}, "text": "Ramble, hello"}
        with patch("ramble.fastlane.time.monotonic", return_value=100), patch.object(
            self.fast, "run_codex", side_effect=RateLimitedError(496)
        ) as model, self.assertLogs("ramble.fast", level="ERROR"):
            self.fast.respond(self.conn, [item])
            self.assertEqual(self.fast.unavailable_until, 596)
            self.fast.respond(self.conn, [item])
        model.assert_called_once()
        self.assertIn("rate limited", self.fast.history[-1][2])


if __name__ == "__main__":
    unittest.main()
