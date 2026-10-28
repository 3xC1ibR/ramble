"""Audio-only mute checks: throwaway homes, fake models/players, no microphone."""
import contextlib
import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from ramble import cli, config, db, speech, state, voice_control
from ramble.cues import AudioCues
from ramble.daemon import Daemon
from ramble.fastlane import FastLane
from ramble.recorder import save_segment
from ramble.router import Router
from ramble.session_store import SessionStore
from ramble.speech_queue import SpeechQueue
from ramble.transcribe import Transcriber


class VoiceControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-voice-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        paths = {"HOME": self.home, "STATE_PATH": self.home / "state.json",
                 "DB_PATH": self.home / "ramble.db", "CONFIG_PATH": self.home / "config.json"}
        paths.update({name: self.home / name.lower() for name in
                      ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        self.enterContext(patch.multiple(config, **paths))
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False,
                        speak_questions=False)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.fast = FastLane(self.cfg)

    def test_explicit_commands_with_and_without_name(self):
        for text in ("Disable voice responses.", "Ramble, mute spoken replies!",
                     "Hey Rabble, pause voice responses", "Please turn off your voice responses"):
            with self.subTest(text=text):
                self.assertIs(voice_control.command(text), False)
        for text in ("Enable voice responses", "Ramble, resume spoken responses.",
                     "Unmute voice replies", "Turn on voice responses, please."):
            with self.subTest(text=text):
                self.assertIs(voice_control.command(text), True)
        for text in ("Don't disable voice responses", 'Say "disable voice responses"',
                     "I want to disable voice responses later", "Disable voice responses and delete this",
                     "Stop talking", "Pause transcription", "Voice responses"):
            with self.subTest(text=text):
                self.assertIsNone(voice_control.command(text))

    def test_separate_durable_switch_preserves_listening_and_timed_pause(self):
        self.assertTrue(voice_control.read()["enabled"])
        state.write(True)
        previous = config.STATE_PATH.read_bytes()
        voice_control.write(False)
        self.assertFalse(voice_control.read()["enabled"])
        self.assertEqual(config.STATE_PATH.read_bytes(), previous)
        self.assertTrue(state.is_listening())
        self.assertTrue(state.activity_allowed())
        self.assertFalse(state.audio_allowed())
        state.write(False)
        state.write(True, time.time() + 3600)
        self.assertFalse(voice_control.read()["enabled"])
        voice_control.write(True)
        self.assertTrue(state.is_paused())
        self.assertFalse(state.audio_allowed())

    def test_corrupt_voice_switch_fails_silent(self):
        for content in ("broken", "[]", '{"enabled":"yes"}', '{"enabled":true,"changed_at":"bad"}'):
            (self.home / "voice.json").write_text(content)
            self.assertFalse(state.audio_allowed())
        voice_control.write(True)
        self.assertTrue(state.audio_allowed())

    def test_voice_commands_are_local_silent_and_claimed_even_with_fast_disabled(self):
        self.fast.cfg["fast_enabled"] = False
        state.write(True)
        for text, expected in (("Disable voice responses", False), ("Ramble, enable voice responses", True)):
            seg = {"id": db.new_id(), "started_at": time.time(), "ended_at": time.time()}
            with patch.object(self.fast.cues, "emit") as cue, patch.object(self.fast, "run_codex") as model:
                self.assertTrue(self.fast.offer(self.conn, seg, text))
            self.assertEqual(voice_control.read()["enabled"], expected)
            self.assertTrue(state.is_listening())
            self.assertEqual(self.conn.execute("SELECT status FROM session_claims WHERE segment_id=?",
                                               (seg["id"],)).fetchone()[0], "handled")
            cue.assert_not_called()
            model.assert_not_called()
            self.assertTrue(self.fast.q.empty())

    def test_muted_capture_transcribes_commands_and_other_speech_without_model_dispatch(self):
        state.write(True)
        transcriber = Transcriber(self.cfg, threading.Event())
        transcriber.on_utterance = self.fast.offer
        voice_control.write(False)
        for text in ("A thought that should still be transcribed", "Enable voice responses"):
            sid = save_segment(self.conn, time.time(), np.zeros(16000))
            self.assertIsNotNone(sid)
            row = self.conn.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
            with patch.object(transcriber, "transcribe_path", return_value={
                "text": text, "language": "en",
                "segments": [{"no_speech_prob": 0, "avg_logprob": 0}],
            }):
                transcriber.process(self.conn, row)
            saved = self.conn.execute("SELECT * FROM segments WHERE id=?", (sid,)).fetchone()
            self.assertEqual(saved["status"], "transcribed")
            self.assertEqual(saved["text"], text)
        self.assertTrue(voice_control.read()["enabled"])
        router = Router(self.cfg, threading.Event())
        thought = self.conn.execute("SELECT t.* FROM thoughts t JOIN segments s ON s.thought_id=t.id WHERE s.id=?", (sid,)).fetchone()
        with patch.object(router, "run_triage") as triage, patch.object(router, "dispatch") as dispatch:
            router.triage(self.conn, thought)
        triage.assert_not_called()
        dispatch.assert_not_called()

    def test_muting_does_not_stop_mic_or_block_addressed_work(self):
        state.write(True)
        voice_control.write(False)
        daemon = Daemon.__new__(Daemon)
        daemon.recorder = MagicMock(active=True, error=None)
        daemon.wake = threading.Event()
        daemon.sync()
        daemon.recorder.stop.assert_not_called()
        seg = {"id": "new", "started_at": time.time(), "ended_at": time.time()}
        self.assertTrue(self.fast.offer(self.conn, seg, "Ramble, tell me the time"))
        self.assertEqual(self.fast.q.get_nowait()["text"], "Ramble, tell me the time")

    def test_all_audio_entry_points_and_cues_are_silent(self):
        voice_control.write(False)
        with patch.object(speech, "synthesize") as generate, patch("ramble.state.subprocess.Popen") as player:
            with self.assertRaises(state.PausedError):
                speech.speak("Hello", self.cfg)
            with self.assertRaises(state.PausedError):
                speech.play_audio("unused.wav", self.cfg)
            with self.assertRaises(state.PausedError):
                state.run_audio(["unused-player"], timeout=1)
        generate.assert_not_called()
        player.assert_not_called()
        cues = AudioCues(dict(self.cfg, fast_speak=True))
        cues.start_work(time.time())
        self.assertFalse(cues.work_active())
        with patch.object(cues.player, "play") as player:
            for kind in ("accepted", "error", "working"):
                cues.emit(kind)
                cues.play(kind, time.monotonic())
        self.assertTrue(cues.q.empty())
        player.assert_not_called()

    def test_active_player_stops_and_old_audio_stays_cancelled_after_unmute(self):
        before = time.time()
        player = MagicMock()
        player.__enter__.return_value = player
        player.poll.return_value = None
        def mute(*args, **kwargs):
            voice_control.write(False)
            voice_control.write(True)
            player.wait.side_effect = None
            player.wait.return_value = 0
            raise subprocess.TimeoutExpired("unused-player", .1)
        player.wait.side_effect = mute
        with patch("ramble.state.subprocess.Popen", return_value=player):
            with self.assertRaises(state.PausedError):
                state.run_audio(["unused-player"], timeout=2)
        player.terminate.assert_called_once()
        self.assertTrue(state.audio_allowed())
        self.assertFalse(state.audio_allowed(before))

    def test_mute_and_resume_during_synthesis_cancels_reply(self):
        def generate(*args, **kwargs):
            voice_control.write(False)
            voice_control.write(True)
            return speech.SpeechResult(self.home / "unused.wav", "fake", 1)
        with patch.object(speech, "synthesize", side_effect=generate), patch.object(speech, "play_audio") as player:
            with self.assertRaises(state.PausedError):
                speech.speak("Hello", self.cfg)
        player.assert_not_called()

    def test_global_mute_cancels_active_queued_speech_without_stopping_work(self):
        store = SessionStore(self.home)
        queue = SpeechQueue(dict(self.cfg, fast_speak=True), store)
        queue.enqueue("Playing", job_id="playing")
        def player(text, cfg, *, cancelled, origin):
            self.assertFalse(cancelled())
            voice_control.write(False)
            self.assertTrue(cancelled())
            raise state.PausedError("Muted")
        with patch.object(speech, "speak", side_effect=player):
            queue.tick(threading.Event())
        with store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM session_speech").fetchone()[0], "cancelled")
        self.assertTrue(state.activity_allowed())

    def test_global_mute_cancels_inflight_cue_and_keeps_it_cancelled_after_resume(self):
        cues = AudioCues(dict(self.cfg, fast_speak=True))
        def player(kind, volume, deadline, cancelled):
            self.assertFalse(cancelled())
            voice_control.write(False)
            self.assertTrue(cancelled())
            voice_control.write(True)
            self.assertTrue(cancelled())
            return False
        with patch.object(cues.player, "play", side_effect=player) as played:
            cues.play("accepted", time.monotonic())
        played.assert_called_once()

    def test_queue_drops_old_and_muted_replies_then_plays_fresh_reply(self):
        store = SessionStore(self.home)
        queue = SpeechQueue(dict(self.cfg, fast_speak=True), store)
        before = time.time()
        queue.enqueue("Old", job_id="old")
        voice_control.write(False)
        queue.enqueue("Muted", job_id="muted")
        voice_control.write(True)
        queue.enqueue("Old generation finished late", job_id="late", since=before)
        with patch.object(speech, "speak") as speaker:
            queue.tick(threading.Event())
            speaker.assert_not_called()
            queue.enqueue("Fresh", job_id="fresh")
            queue.tick(threading.Event())
            self.assertEqual(speaker.call_args.args[0], "Fresh")
        with store.connect() as conn:
            statuses = dict(conn.execute("SELECT id,status FROM session_speech"))
        self.assertEqual(statuses, {"old": "cancelled", "muted": "cancelled", "late": "cancelled", "fresh": "played"})

    def test_cli_controls_and_status_without_touching_capture(self):
        state.write(True)
        previous = config.STATE_PATH.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            for args in (["voice", "off"], ["voice"], ["status"], ["voice", "on"]):
                with patch("sys.argv", ["ramble", *args]), patch("ramble.cli.subprocess.run", return_value=MagicMock(stdout="")):
                    cli.main()
        self.assertIn("voice responses off; mic on", output.getvalue())
        self.assertIn("voice responses: off", output.getvalue())
        self.assertTrue(voice_control.read()["enabled"])
        self.assertEqual(config.STATE_PATH.read_bytes(), previous)


if __name__ == "__main__":
    unittest.main()
