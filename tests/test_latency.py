"""Timing regressions: synthetic files, mocked models/player, no mic or dispatch."""

import io
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from ramble import config, db, latency
from ramble.fastlane import FastLane
from ramble.recorder import save_segment
from ramble.transcribe import Transcriber


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ramble-latency-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        paths = {"HOME": home, "DB_PATH": home / "ramble.db", "STATE_PATH": home / "state.json"}
        paths.update({k: home / k.lower() for k in ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        p = patch.multiple(config, **paths)
        p.start()
        self.addCleanup(p.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, audio_cues_enabled=False)

    def test_file_to_response_trace_and_failed_player(self):
        wav = config.HOME / "fixture.wav"
        sf.write(wav, np.zeros(1600), 16000)
        fast = FastLane(self.cfg)
        transcriber = Transcriber(self.cfg, threading.Event())
        transcriber.on_utterance = fast.offer
        for fail in (False, True):
            with self.subTest(fail=fail), self.assertLogs("ramble.latency", level="INFO") as logs:
                seg_id = save_segment(self.conn, 100, np.zeros(16000), source="file:fixture")
                row = self.conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
                with patch.object(transcriber, "transcribe_path", return_value={
                    "text": "Ramble, hello", "segments": [{"no_speech_prob": 0, "avg_logprob": 0}]
                }):
                    transcriber.process(self.conn, row)
                with patch.object(fast, "run_codex", return_value={"reply": "Hello", "handoff": False}), \
                        patch("urllib.request.urlopen", return_value=io.BytesIO(wav.read_bytes())), \
                        patch("ramble.cue_player.CuePlayer.play_file", side_effect=OSError("output failed") if fail else None):
                    fast.respond(self.conn, [fast.q.get_nowait()])
            events = [json.loads(line.split("latency ", 1)[1]) for line in logs.output]
            stages = [e["stage"] for e in events]
            expected = ["segment_saved", "transcription_start", "transcription_end", "request_queued",
                        "answer_start", "answer_end", "reply_ready", "synthesis_start", "synthesis_end",
                        "speech_file_ready", "speech_lock_start", "speech_lock_end", "playback_command_start", "playback_command_end"]
            self.assertEqual(stages, expected)
            self.assertEqual([e["mono_s"] for e in events], sorted(e["mono_s"] for e in events))
            self.assertTrue(all(e["request_id"] == seg_id for e in events[4:]))
            self.assertEqual(events[-1]["ok"], not fail)
            self.assertTrue(all("text" not in e and "reply" not in e for e in events))
            self.assertIsNone(latency._request.get())

    def test_logging_failure_does_not_break_response(self):
        with patch.object(latency.log, "info", side_effect=OSError("log unavailable")):
            with latency.request("id"), latency.span("example"):
                pass

    def test_nested_context_restores_and_propagates_failure(self):
        with self.assertLogs("ramble.latency", level="INFO") as logs:
            with latency.request("outer"):
                with self.assertRaises(ValueError), latency.request("inner"), latency.span("answer"):
                    raise ValueError("not logged")
                latency.event("next")
        events = [json.loads(line.split("latency ", 1)[1]) for line in logs.output]
        self.assertEqual([e["request_id"] for e in events], ["inner", "inner", "outer"])
        self.assertFalse(events[1]["ok"])


if __name__ == "__main__":
    unittest.main()
