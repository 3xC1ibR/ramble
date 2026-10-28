"""No microphone, agents, or real-home data: python -m unittest discover -s tests."""

import io
import fcntl
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

from ramble import config, db, speech, state, voice_control
from ramble.cue_player import PlaybackCancelled
from ramble.recorder import Recorder, save_segment


class SpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ramble-test-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name)
        paths = {"HOME": home, "DB_PATH": home / "ramble.db", "STATE_PATH": home / "state.json"}
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

    def test_elevenlabs_file_key_and_pcm_become_playable_wav(self):
        key_file = config.HOME / "api-key"
        key_file.write_text("test-file-key\n")
        cfg = dict(self.cfg, tts_provider="elevenlabs",
                   elevenlabs_api_key_file=str(key_file), tts_speed=0.95)
        pcm = np.arange(2400, dtype="<i2").tobytes()
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "unused-env-key"}), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(pcm)) as post, \
                patch("subprocess.run") as player:
            result = speech.synthesize("Hello the user", cfg, voice="override-id")
        request = post.call_args.args[0]
        self.assertEqual(request.full_url,
                         "https://api.elevenlabs.io/v1/text-to-speech/override-id?output_format=pcm_24000")
        self.assertEqual(request.get_header("Xi-api-key"), "test-file-key")
        payload = json.loads(request.data)
        self.assertEqual(payload["text"], "Hello the user")
        self.assertEqual(payload["model_id"], "eleven_flash_v2_5")
        self.assertEqual(payload["voice_settings"]["speed"], 0.95)
        self.assertNotIn("test-file-key", request.data.decode())
        self.assertEqual(result.engine, "elevenlabs")
        self.assertAlmostEqual(result.duration, 0.1)
        audio, rate = sf.read(result.path, dtype="int16")
        self.assertEqual(rate, 24000)
        np.testing.assert_array_equal(audio, np.arange(2400))
        player.assert_not_called()

    def test_elevenlabs_env_key_and_default_voice(self):
        cfg = dict(self.cfg, tts_provider="elevenlabs")
        with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-env-key"}), \
                patch("urllib.request.urlopen", return_value=io.BytesIO(b"\0\0" * 2400)) as post:
            speech.synthesize("hello", cfg)
        request = post.call_args.args[0]
        self.assertEqual(request.get_header("Xi-api-key"), "test-env-key")
        self.assertIn(cfg["elevenlabs_voice_id"], request.full_url)

    def test_elevenlabs_missing_key_does_not_send_request(self):
        cfg = dict(self.cfg, tts_provider="elevenlabs")
        with patch.dict("os.environ", {}, clear=True), \
                patch("urllib.request.urlopen") as post, \
                patch("subprocess.run", side_effect=OSError("say failed")):
            with self.assertRaisesRegex(OSError, "say failed"):
                speech.synthesize("hello", cfg)
        post.assert_not_called()

    def test_elevenlabs_bad_audio_or_http_error_falls_back(self):
        cfg = dict(self.cfg, tts_provider="elevenlabs")
        def generate(args, **kwargs):
            self.assertEqual(args[0], "/usr/bin/say")
            sf.write(args[2], np.zeros(1600), 16000)
        failures = [b"", b"\0", OSError("unauthorized")]
        for failure in failures:
            with self.subTest(failure=failure), \
                    patch.dict("os.environ", {"ELEVENLABS_API_KEY": "test-key"}), \
                    patch("urllib.request.urlopen") as post, \
                    patch("subprocess.run", side_effect=generate):
                if isinstance(failure, Exception):
                    post.side_effect = failure
                else:
                    post.return_value = io.BytesIO(failure)
                self.assertEqual(speech.synthesize("hello", cfg).engine, "macos-say")

    def test_invalid_provider_is_visible(self):
        with self.assertRaisesRegex(ValueError, "Unknown TTS provider"):
            speech.synthesize("hello", dict(self.cfg, tts_provider="typo"))

    def test_guard_committed_before_player_and_closed_after_failure(self):
        def player(path, **kwargs):
            now = time.time()
            self.assertTrue(speech.overlaps_playback(self.conn, now, now + .01))
            row = self.conn.execute("SELECT * FROM speech_playbacks").fetchone()
            self.assertLess(row["ended_at"] - now, 17)
            raise OSError("output failed")
        with patch("ramble.cue_player.CuePlayer.play_file", side_effect=player):
            with self.assertRaisesRegex(OSError, "output failed"):
                speech.play_audio(self.wav, self.cfg)
        end = self.conn.execute("SELECT ended_at FROM speech_playbacks").fetchone()[0]
        self.assertAlmostEqual(end - time.time(), 1, delta=.2)
        self.assertFalse(speech.overlaps_playback(self.conn, end, end + .1))

    def test_synthesis_leaves_cue_output_available_and_notice_matches_playback(self):
        sequence = []
        def synthesize(*args, **kwargs):
            with (config.HOME / 'speech.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sequence.append('synthesis')
            return speech.SpeechResult(self.wav, 'test', .1)
        def start():
            self.assertTrue(speech.overlaps_playback(self.conn, time.time(), time.time()+.01))
            sequence.append('start')
        def player(*args, **kwargs):
            sequence.append('player')
            kwargs['on_start']({'first_frame_mono_s': time.monotonic()})
        with patch.object(speech, 'synthesize', side_effect=synthesize), \
                patch('ramble.cue_player.CuePlayer.play_file', side_effect=player):
            speech.speak('Hello', self.cfg, on_playback_start=start)
        self.assertEqual(sequence, ['synthesis', 'player', 'start'])

    def test_pause_or_mute_during_persistent_output_cancels_even_after_resume(self):
        for action in ('pause', 'mute'):
            with self.subTest(action=action):
                def player(_path, *, timeout, cancelled, on_start):
                    self.assertFalse(cancelled())
                    on_start({'first_frame_mono_s': time.monotonic()})
                    if action == 'pause':
                        state.write(True, time.time()+3600)
                        state.write(True)
                    else:
                        voice_control.write(False)
                        voice_control.write(True)
                    self.assertTrue(cancelled())
                    raise PlaybackCancelled('cancelled')
                with patch('ramble.cue_player.CuePlayer.play_file', side_effect=player):
                    with self.assertRaises(state.PausedError):
                        speech.play_audio(self.wav, self.cfg)
                end = self.conn.execute('SELECT MAX(ended_at) FROM speech_playbacks').fetchone()[0]
                self.assertAlmostEqual(end-time.time(), self.cfg['tts_guard_tail_s'], delta=.2)

    def test_first_frame_diagnostic_uses_callback_receipt_and_keeps_request_id(self):
        from ramble import latency
        def player(_path, **kwargs):
            kwargs['on_start']({'first_frame_mono_s': time.monotonic()+.02})
        with patch('ramble.cue_player.CuePlayer.play_file', side_effect=player), \
                self.assertLogs('ramble.latency', level='INFO') as logs, latency.request('request-7'):
            speech.play_audio(self.wav, self.cfg)
        events = [json.loads(line.split('latency ', 1)[1]) for line in logs.output]
        first = next(e for e in events if e['stage'] == 'playback_first_frame')
        self.assertEqual(first['request_id'], 'request-7')
        self.assertEqual(first['timing_source'], 'portaudio_dac_estimate')
        self.assertGreaterEqual(first['output_start_delay_s'], .02)
        self.assertLess(first['output_start_delay_s'], .1)

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
