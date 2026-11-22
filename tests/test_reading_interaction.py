"""Reading conversations preserve source positions; no audio or model processes."""
import json
from pathlib import Path
import tempfile
import threading
import time
import wave
import unittest
from unittest.mock import Mock, patch

from ramble import config, reading_interaction as interaction, state, voice_control
from ramble.reading import ReadingStore


class ReadingInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = ReadingStore(self.home)
        self.article = "The engine stores the source exactly. A completed passage advances the position. A pause preserves it."
        self.current = self.store.start(self.article, title="Reading design", request_id="article-1")["reading"]
        self.stop = threading.Event()
        self.cfg = dict(config.DEFAULTS, fast_speak=False, dispatch_daily_cap=0)

    def submit(self, question="Why does a pause preserve the position?", **kwargs):
        return interaction.submit_question(question, home=self.home, **kwargs)

    def worker(self, answer=None, **kwargs):
        return interaction.ReadingQuestions(self.cfg, answer or Mock(return_value={"answer": "The current passage is retained."}),
                                             home=self.home, **kwargs)

    def test_question_pauses_and_retains_exact_source_and_position(self):
        request = self.submit(request_id="question-1")
        self.assertEqual(request["status"], "pending")
        paused = self.store.snapshot()["reading"]
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["text"], self.article)
        self.assertEqual(request["position"], paused["position"])
        self.assertEqual(request["revision"], paused["revision"])
        self.assertEqual(request["reading_id"], self.current["id"])

    def test_duplicate_submit_does_not_pause_again_after_resume(self):
        self.submit(request_id="question-1")
        self.store.control("resume")
        before = self.store.snapshot()["reading"]
        self.submit(request_id="question-1")
        self.assertEqual(self.store.snapshot()["reading"]["revision"], before["revision"])
        self.assertEqual(self.store.snapshot()["reading"]["status"], before["status"])
        with self.assertRaises(ValueError):
            self.submit("A different question", request_id="question-1")

    def test_answer_is_separate_and_does_not_resume_or_advance(self):
        self.submit()
        before = self.store.snapshot()["reading"]
        model = Mock(return_value={"answer": "It remembers the interrupted passage."})
        self.assertTrue(self.worker(model).tick(self.stop))
        result = interaction.snapshot(self.home)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "It remembers the interrupted passage.")
        after = self.store.snapshot()["reading"]
        self.assertEqual(after["text"], self.article)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(after["status"], "paused")
        self.assertEqual(after["position"], before["position"])
        self.assertTrue(model.call_args.kwargs["read_only"])
        self.assertIn(self.article, model.call_args.args[0])

    def test_seek_does_not_claim_unplayed_passages_were_heard(self):
        self.store.control("seek", passage_index=1)
        request = self.submit("", recap=True)
        model = Mock(return_value={"answer": "The source stays exact."})
        self.worker(model).tick(self.stop)
        payload = json.loads(model.call_args.args[0].splitlines()[-1])
        self.assertEqual(payload["request_kind"], "recap")
        self.assertEqual(payload["completed_passages"], 0)
        self.assertEqual(payload["recent_completed_text"], "")
        self.assertEqual(payload["reading_position"], 1)
        self.assertEqual(request["question"], "")

    def test_replacement_during_answer_discards_result_and_never_speaks(self):
        self.submit()
        def answer(prompt, **kwargs):
            self.store.start("A different article.", request_id="replacement")
            self.assertTrue(kwargs["cancelled"]())
            return {"answer": "Stale answer"}
        speaker = Mock()
        self.cfg["fast_speak"] = True
        self.worker(answer, speak=speaker).tick(self.stop)
        self.assertEqual(interaction.snapshot(self.home)["status"], "cancelled")
        self.assertIsNone(interaction.snapshot(self.home)["answer"])
        speaker.assert_not_called()

    def test_resume_before_processing_cancels_question(self):
        self.submit()
        self.store.control("resume")
        model = Mock()
        self.worker(model).tick(self.stop)
        model.assert_not_called()
        self.assertEqual(interaction.snapshot(self.home)["status"], "cancelled")

    def test_new_question_supersedes_in_flight_answer(self):
        self.submit(request_id="first")
        def answer(prompt, **kwargs):
            self.submit("Explain the next point.", request_id="second")
            self.assertTrue(kwargs["cancelled"]())
            return {"answer": "Old answer"}
        self.worker(answer).tick(self.stop)
        result = interaction.snapshot(self.home)
        self.assertEqual(result["request_id"], "second")
        self.assertEqual(result["status"], "pending")

    def test_seek_immediately_before_publication_discards_answer(self):
        self.submit()
        worker = self.worker()
        original = worker.finish
        def finish(request, status, **kwargs):
            if status == "answered":
                self.store.control("seek", passage_index=1)
            original(request, status, **kwargs)
        worker.finish = finish
        worker.tick(self.stop)
        result = interaction.snapshot(self.home)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["answer"])

    def test_responder_cannot_publish_against_obsolete_revision(self):
        self.store.control("pause")
        previous = self.store.snapshot()["reading"]
        self.store.control("seek", passage_index=1)
        with self.assertRaises(ValueError):
            interaction.record_answer("Question", "Stale response", previous,
                                      home=self.home, request_id="stale-voice")
        self.assertIsNone(interaction.snapshot(self.home))

    def test_failed_model_keeps_position_and_visible_retry(self):
        self.submit()
        self.worker(Mock(side_effect=TimeoutError("fixture failure"))).tick(self.stop)
        self.assertEqual(interaction.snapshot(self.home)["status"], "failed")
        self.assertEqual(self.store.snapshot()["reading"]["status"], "paused")

    def test_audio_controls_use_the_selected_home(self):
        worker = self.worker()
        self.assertTrue(worker.audio_allowed(1))
        voice_control.write(False, home=self.home)
        self.assertFalse(worker.audio_allowed(1))
        voice_control.write(True, home=self.home)
        self.assertFalse(worker.audio_allowed(1))

    def test_controls_require_a_complete_phrase(self):
        for text, action in [("Ramble, hold on", "pause"), ("Ramble, keep going", "resume"),
                             ("Where were we?", "recap"), ("Read that again", "repeat")]:
            self.assertEqual(interaction.local_control(text), action)
        for text in ["He said hold on", 'If I say "pause reading", what happens?',
                     "Keep going with the migration", "Explain how resume works"]:
            self.assertIsNone(interaction.local_control(text))

    def test_repeat_uses_last_confirmed_passage_when_between_passages(self):
        current = {**self.current, "position": 1, "active_index": None, "heard_indices": [0]}
        store = Mock(home=self.home)
        store.snapshot.return_value = {"reading": current}
        interaction.apply_control(store, "repeat", request_id="repeat")
        self.assertEqual(store.control.call_args_list[0].kwargs["passage_index"], 0)

    def test_spoken_question_answer_opens_normal_followup_origin(self):
        self.submit()
        speaker = Mock()
        self.cfg["fast_speak"] = True
        self.worker(speak=speaker).tick(self.stop)
        self.assertEqual(speaker.call_args.kwargs["origin"], "reply")

    def test_repeat_after_partial_sentence_uses_last_started_passage(self):
        current = {**self.current, "position": 1, "active_index": None,
                   "last_started_index": 1, "heard_indices": [0]}
        store = Mock(home=self.home)
        store.snapshot.return_value = {"reading": current}
        interaction.apply_control(store, "repeat", request_id="repeat-interrupted")
        self.assertEqual(store.control.call_args_list[0].kwargs["passage_index"], 1)

    def test_stale_reader_rejects_question_without_pausing_new_article(self):
        self.store.start("Replacement source.")
        before = self.store.snapshot()["reading"]
        with self.assertRaises(ValueError):
            self.submit(reading_id=self.current["id"])
        self.assertEqual(self.store.snapshot()["reading"]["revision"], before["revision"])


class ReadingVoiceTests(unittest.TestCase):
    def setUp(self):
        from ramble.fastlane import FastLane
        from ramble.session_store import SessionStore
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.sessions = SessionStore(self.home)
        self.fast = FastLane(dict(config.DEFAULTS, fast_speak=False))
        self.fast.reading = ReadingStore(self.home)
        self.fast.reading.start("First sentence. Second sentence.")

    def test_named_pause_executes_without_model_or_queue(self):
        with self.sessions.connect() as conn, patch("ramble.state.activity_allowed", return_value=True):
            self.assertTrue(self.fast.offer(conn, {"id": "pause", "started_at": 1, "ended_at": 2}, "Ramble, pause reading"))
        self.assertEqual(self.fast.reading.snapshot()["reading"]["status"], "paused")
        self.assertTrue(self.fast.q.empty())

    def test_ambient_control_does_not_pause(self):
        with self.sessions.connect() as conn, patch("ramble.state.activity_allowed", return_value=True):
            self.assertFalse(self.fast.offer(conn, {"id": "ambient", "started_at": 1, "ended_at": 2}, "pause reading"))
        self.assertNotEqual(self.fast.reading.snapshot()["reading"]["status"], "paused")

    def test_old_reading_does_not_capture_generic_continue(self):
        self.fast.reading.control("pause")
        old = {**self.fast.reading.snapshot()["reading"], "updated_at": 0}
        with self.sessions.connect() as conn, patch("ramble.state.activity_allowed", return_value=True), \
                patch.object(self.fast.reading, "snapshot", return_value={"reading": old}), \
                patch.object(self.fast.reading, "control") as control:
            self.assertTrue(self.fast.offer(conn, {"id": "continue", "started_at": 1, "ended_at": 2}, "Ramble, continue"))
            control.assert_not_called()
        self.assertFalse(self.fast.q.empty())

    def test_companion_question_uses_reading_without_dispatch(self):
        with self.sessions.connect() as conn, patch("ramble.state.activity_allowed", return_value=True):
            self.assertTrue(self.fast.offer(conn, {"id": "ask", "started_at": 1, "ended_at": 2,
                                                   "source": "companion"}, "Why does that follow?"))
            item = self.fast.q.get_nowait()
            with patch.object(self.fast, "run_codex", return_value={
                    "reply": "The second sentence follows from the first.", "handoff": False,
                    "reading_answer": True}) as model:
                self.fast.respond(conn, [item])
            model.assert_called_once()
            self.assertIn("First sentence. Second sentence.", model.call_args.args[0])
        self.assertEqual(interaction.snapshot(self.home)["question"], "Why does that follow?")
        self.assertEqual(interaction.snapshot(self.home)["status"], "answered")
        self.assertEqual(self.fast.reading.snapshot()["reading"]["status"], "paused")

    def test_unrelated_question_keeps_normal_response_path(self):
        with self.sessions.connect() as conn, patch("ramble.state.activity_allowed", return_value=True):
            self.fast.offer(conn, {"id": "weather", "started_at": 1, "ended_at": 2,
                                  "source": "companion"}, "What is the weather?")
            item = self.fast.q.get_nowait()
            with patch.object(self.fast, "run_codex", return_value={
                    "reply": "It is clear.", "handoff": False, "reading_answer": False}) as model:
                self.fast.respond(conn, [item])
            model.assert_called_once()
        self.assertIsNone(interaction.snapshot(self.home))

    def test_new_voice_request_cancels_old_app_answer_before_model_finishes(self):
        interaction.submit_question("Explain the first point.", home=self.home, request_id="old")
        speaker = Mock()
        def answer(prompt, **kwargs):
            with self.sessions.connect() as conn, patch("ramble.state.activity_allowed", return_value=True):
                self.fast.offer(conn, {"id": "new", "started_at": 1, "ended_at": 2,
                                      "source": "companion"}, "Explain the other point instead")
            self.assertTrue(kwargs["cancelled"]())
            return {"answer": "An old answer."}
        worker = interaction.ReadingQuestions(dict(config.DEFAULTS, fast_speak=True), answer,
                                               home=self.home, speak=speaker)
        worker.tick(threading.Event())
        speaker.assert_not_called()
        self.assertEqual(interaction.snapshot(self.home)["status"], "cancelled")


class ReadingFlowTests(unittest.TestCase):
    def test_interrupt_return_question_and_continue_preserves_exact_source(self):
        from ramble import speech
        from ramble.reading import ReadingController
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            store = ReadingStore(home)
            controller = ReadingController(dict(config.DEFAULTS, fast_speak=True), store)
            stop = threading.Event()
            starts = []
            def wait_for(predicate):
                deadline = time.monotonic() + 4
                while not predicate():
                    if time.monotonic() >= deadline:
                        self.fail("Reading flow did not reach its expected state")
                    time.sleep(.01)
            def synthesize(text, cfg, *, output):
                with wave.open(str(output), "wb") as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(24000)
                    target.writeframes(b"\x00\x00" * 240)
                return speech.SpeechResult(Path(output), "fixture", .01)
            def play(path, cfg, *, cancelled, on_playback_start, **kwargs):
                on_playback_start()
                current = store.snapshot()["reading"]
                starts.append(current["active_index"])
                if len(starts) in {1, 3}:
                    wait_for(cancelled)
                    raise state.PausedError("Fixture interruption")
            with patch("ramble.speech.synthesize", side_effect=synthesize), \
                    patch("ramble.speech.play_prepared", side_effect=play):
                thread = threading.Thread(target=controller.run_forever, args=(stop,))
                thread.start()
                try:
                    wait_for(lambda: store.snapshot()["controller_online"])
                    source = "The article retains its original words. A question keeps the saved place."
                    store.start(source, request_id="flow")
                    wait_for(lambda: len(starts) == 1)
                    store.control("pause")
                    wait_for(lambda: controller.playback is None)
                    self.assertEqual(store.snapshot()["reading"]["heard_indices"], [])
                    self.assertEqual(store.snapshot()["reading"]["position"], 0)
                    store.control("resume")
                    wait_for(lambda: len(starts) == 3)
                    interaction.submit_question("Why is the source preserved?", home=home)
                    wait_for(lambda: controller.playback is None)
                    worker = interaction.ReadingQuestions(dict(config.DEFAULTS, fast_speak=False),
                        Mock(return_value={"answer": "So an explanation never replaces the author's words."}), home=home)
                    worker.tick(threading.Event())
                    self.assertEqual(interaction.snapshot(home)["status"], "answered")
                    self.assertEqual(store.snapshot()["reading"]["position"], 1)
                    self.assertEqual(store.snapshot()["reading"]["heard_indices"], [0])
                    store.control("resume")
                    wait_for(lambda: store.snapshot()["reading"]["status"] == "completed")
                    self.assertEqual(starts, [0, 0, 1, 1])
                    self.assertEqual(store.snapshot()["reading"]["text"], source)
                    self.assertEqual(store.snapshot()["reading"]["heard_indices"], [0, 1])
                finally:
                    stop.set()
                    thread.join(5)
                    self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
