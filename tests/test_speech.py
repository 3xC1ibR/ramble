"""No microphone, agents, or real-home data: python -m unittest discover -s tests."""

import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from ramble import config, db, speech
from ramble.recorder import Recorder, save_segment


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ramble-test-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        paths = {"HOME": home, "DB_PATH": home / "ramble.db"}
        paths.update({name: home / name.lower() for name in
                      ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        self.paths = patch.multiple(config, **paths)
        self.paths.start()
        self.addCleanup(self.paths.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0)
        self.wav = home / "reply.wav"
        sf.write(self.wav, np.zeros(1600), 16000)

    def test_kokoro_contract_and_valid_wav(self):
        response = io.BytesIO(self.wav.read_bytes())
        with patch("urllib.request.urlopen", return_value=response) as post:
            result = speech.synthesize("Hello the user", self.cfg)
        payload = json.loads(post.call_args.args[0].data)
        self.assertEqual(payload["voice"], "af_heart")
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(result.engine, "kokoro")
        self.assertGreater(result.duration, 0)

    def test_unreachable_server_falls_back_and_both_failures_surface(self):
        def generate(args, **kwargs):
            self.assertEqual(args[0], "/usr/bin/say")
            sf.write(args[2], np.zeros(1600), 16000)
        with patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                patch("subprocess.run", side_effect=generate):
            self.assertEqual(speech.synthesize("hello", self.cfg).engine, "macos-say")
        with patch("urllib.request.urlopen", side_effect=OSError("offline")), \
                patch("subprocess.run", side_effect=OSError("say failed")):
            with self.assertRaisesRegex(OSError, "say failed"):
                speech.synthesize("hello", self.cfg)

    def test_guard_committed_before_player_and_closed_after_failure(self):
        def player(args, **kwargs):
            now = time.time()
            self.assertTrue(speech.overlaps_playback(self.conn, now, now + .01))
            row = self.conn.execute("SELECT * FROM speech_playbacks").fetchone()
            self.assertLess(row["ended_at"] - now, 17)
            raise subprocess.CalledProcessError(1, args)
        with patch("subprocess.run", side_effect=player):
            with self.assertRaises(subprocess.CalledProcessError):
                speech.play_audio(self.wav, self.cfg)
        end = self.conn.execute("SELECT ended_at FROM speech_playbacks").fetchone()[0]
        self.assertAlmostEqual(end - time.time(), 1, delta=.2)
        self.assertFalse(speech.overlaps_playback(self.conn, end, end + .1))

    def test_mic_drops_overlap_but_explicit_import_is_allowed(self):
        self.conn.execute("INSERT INTO speech_playbacks VALUES ('test',100,103,?)", (str(self.wav),))
        samples = np.zeros(16000, dtype=np.float32)
        self.assertIsNone(save_segment(self.conn, 99.5, samples))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0], 0)
        self.assertIsNotNone(save_segment(self.conn, 103, samples))
        self.assertIsNotNone(save_segment(self.conn, 101, samples, source="file:fixture"))

    def test_recorder_discards_delayed_chunks_and_partial_utterance(self):
        self.conn.execute("INSERT INTO speech_playbacks VALUES ('test',100,103,?)", (str(self.wav),))
        recorder = Recorder(self.cfg, threading.Event())
        # Exercise the real worker queue but stub VAD so no microphone/model is needed.
        with patch("ramble.recorder.Segmenter") as segmenter:
            segmenter.return_value.feed.return_value = 0
            recorder.q.put((99, np.ones(512, dtype=np.float32)))
            recorder.q.put((101, np.ones(512, dtype=np.float32)))
            recorder.q.put((104, np.ones(512, dtype=np.float32)))
            recorder.q.put(None)
            with self.assertLogs("ramble.recorder", level="INFO") as logs:
                recorder._process()
            self.assertEqual(segmenter.return_value.feed.call_count, 2)
            segmenter.return_value.reset.assert_called_once()
            self.assertIn("dropping mic audio", " ".join(logs.output))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM thoughts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
