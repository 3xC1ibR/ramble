"""Recognition regressions without a microphone, model downloads, or dispatch."""

import json
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

from ramble import config
from ramble.transcribe import Transcriber


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(config.DEFAULTS)
        memory = patch("ramble.transcribe.RecognitionMemory")
        self.memory = memory.start().return_value
        self.memory.snapshot.return_value = {"mlx_active_bytes": 123}
        self.addCleanup(memory.stop)
        self.transcriber = Transcriber(self.cfg, threading.Event())
        self.engine = Mock()
        self.module = patch.dict(sys.modules, {"mlx_whisper": types.SimpleNamespace(transcribe=self.engine)})
        self.module.start()
        self.addCleanup(self.module.stop)
        self.audio = patch("ramble.transcribe.sf.read", return_value=(np.zeros(16000, dtype=np.float32), 16000))
        self.audio.start()
        self.addCleanup(self.audio.stop)

    def test_observed_name_corrections_keep_hinted_transcript(self):
        for alias in ("Grable", "Mr. Abel", "Scramble", "Rabble"):
            with self.subTest(alias=alias):
                corrected = {"text": "Ramble, do you remember prior requests?"}
                self.engine.side_effect = [corrected, {"text": f"{alias}, do you remember prior requests?"}]
                self.assertIs(self.transcriber.transcribe_path("unused.flac"), corrected)

    def test_hint_cannot_turn_right_but_into_a_wake_name(self):
        actual = {"text": "Right, but what's the easiest way?"}
        self.engine.side_effect = [{"text": "Ramble, what's the easiest way?"}, actual]
        self.assertIs(self.transcriber.transcribe_path("unused.flac"), actual)

    def test_passive_speech_needs_only_one_pass(self):
        actual = {"text": "Come on, why are you being weird?"}
        self.engine.return_value = actual
        self.assertIs(self.transcriber.transcribe_path("unused.flac"), actual)
        self.engine.assert_called_once()

    def test_disabling_hint_keeps_single_pass_and_language_override(self):
        self.cfg.update(whisper_initial_prompt=None, language=None)
        self.engine.return_value = {"text": "Ramble, hello."}
        self.transcriber.transcribe_path("unused.flac")
        self.engine.assert_called_once()
        self.assertIsNone(self.engine.call_args.kwargs["initial_prompt"])
        self.assertIsNone(self.engine.call_args.kwargs["language"])

    def test_prompted_and_confirmation_pass_share_language_and_audio(self):
        self.engine.side_effect = [{"text": "Ramble, hello."}, {"text": "Grable, hello."}]
        self.transcriber.transcribe_path("unused.flac")
        first, second = self.engine.call_args_list
        self.assertEqual(first.kwargs["initial_prompt"], self.cfg["whisper_initial_prompt"])
        self.assertIsNone(second.kwargs["initial_prompt"])
        self.assertIs(first.args[0], second.args[0])
        self.assertEqual(first.kwargs["language"], "en")
        self.assertEqual(second.kwargs["language"], "en")

    def test_each_pass_has_segment_timing_without_transcript(self):
        self.engine.side_effect = [{"text": "Ramble, hello."}, {"text": "Grable, hello."}]
        with self.assertLogs("ramble.latency", level="INFO") as logs:
            self.transcriber.transcribe_path("unused.flac", segment_id="source-1")
        events = [json.loads(line.split("latency ", 1)[1]) for line in logs.output]
        ends = [e for e in events if e["stage"] == "transcription_decode_end"]
        self.assertEqual([e["decode_pass"] for e in ends], ["hinted", "confirmation"])
        self.assertTrue(all(e["segment_id"] == "source-1" and e["ok"] for e in ends))
        self.assertIsNone(ends[0]["idle_s"])
        self.assertGreaterEqual(ends[1]["idle_s"], 0)
        self.assertNotIn("Ramble, hello", " ".join(logs.output))
        stats = [e for e in events if e["stage"] == "transcription_decode_memory"]
        self.assertEqual(len(stats), 2)
        self.assertTrue(all("process_major_faults" in e for e in stats))

    def test_decode_failure_is_timed_and_keeps_original_error(self):
        self.engine.side_effect = RuntimeError("decode failed")
        with self.assertLogs("ramble.latency", level="INFO") as logs, self.assertRaisesRegex(RuntimeError, "decode failed"):
            self.transcriber.transcribe_path("unused.flac", segment_id="source-2")
        events = [json.loads(line.split("latency ", 1)[1]) for line in logs.output]
        end = next(e for e in events if e["stage"] == "transcription_decode_end")
        self.assertFalse(end["ok"])
        self.assertEqual(end["segment_id"], "source-2")

    def test_worker_releases_residency_when_off_and_closes_on_stop(self):
        stop = threading.Event()
        conn = Mock()
        conn.execute.return_value.fetchone.side_effect = [None, (0,)]
        self.transcriber.wake.wait = Mock(side_effect=lambda _: stop.set())
        with patch("ramble.transcribe.db.connect", return_value=conn), \
                patch("ramble.transcribe.state.is_listening", return_value=False), \
                patch.object(self.transcriber, "warmup"):
            self.transcriber.run_forever(stop)
        self.memory.set_resident.assert_called_once_with(False)
        self.memory.close.assert_called_once()
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
