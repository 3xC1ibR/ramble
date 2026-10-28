"""Timed pause regression checks. No microphone, speakers, or dispatched agents."""

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

from ramble import cli, config, db, speech, state
from ramble.cues import AudioCues
from ramble.daemon import Daemon
from ramble.fastlane import FastLane
from ramble.recorder import save_segment
from ramble.router import Router, notify


class SnoozeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-snooze-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        paths = {"HOME": home, "DB_PATH": home / "ramble.db",
                 "STATE_PATH": home / "state.json", "CONFIG_PATH": home / "config.json"}
        paths.update({name: home / name.lower() for name in
                      ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        patched = patch.multiple(config, **paths)
        patched.start()
        self.addCleanup(patched.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False,
                        speak_questions=False)
        now = time.time()
        self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,status,text) "
                          "VALUES ('thought',?,?,'closed','Ramble, build a thing')", (now, now))
        self.thought = self.conn.execute("SELECT * FROM thoughts").fetchone()
        self.item = {"action": "dispatch", "kind": "task", "text": "Build a thing",
                     "dispatch": {"name": "Test task", "cwd": "/tmp", "prompt": "Test only"}}
        self.decision = {"classification": "actionable", "summary": "Test", "notify": "Ready",
                         "items": [self.item, {"action": "ask", "question": "Which thing?"}]}
        self.router = Router(self.cfg, threading.Event())

    def pause(self):
        return state.write(True, time.time() + 3600)

    def test_cli_duration_and_early_resume(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with patch("sys.argv", ["ramble", "off", "90m"]):
                cli.main()
            saved = state.read()
            self.assertAlmostEqual(saved["paused_until"] - saved["changed_at"], 5400, delta=.1)
            self.assertTrue(state.is_paused())
            with patch("sys.argv", ["ramble", "on"]):
                cli.main()
        self.assertIn("paused until", output.getvalue())
        self.assertTrue(state.is_listening())
        self.assertFalse(state.is_paused())
        self.assertEqual(state.read()["last_paused_at"], saved["last_paused_at"])

    def test_expiry_and_mic_sync_without_opening_mic(self):
        with patch("ramble.state.time.time", return_value=100):
            state.write(True, 200)
        daemon = Daemon.__new__(Daemon)
        daemon.recorder = MagicMock(active=True)
        daemon.wake = threading.Event()
        with patch("ramble.state.time.time", return_value=150):
            daemon.sync()
            daemon.recorder.stop.assert_called_once()
            self.assertFalse(state.is_listening())
        daemon.recorder.active = False
        with patch("ramble.state.time.time", return_value=201):
            self.assertTrue(state.is_listening())
            self.assertTrue(state.activity_allowed())
            daemon.sync()
            daemon.recorder.start.assert_called_once()

    def test_pause_blocks_fast_name_ambient_segment_and_chime(self):
        self.pause()
        fast = FastLane(self.cfg)
        now = time.time()
        self.assertFalse(fast.offer(self.conn, {"started_at": now, "ended_at": now},
                                    "Ramble, please start an agent"))
        self.assertTrue(fast.q.empty())
        self.assertIsNone(save_segment(self.conn, now, np.zeros(16000)))
        cues = AudioCues({})
        cues.emit("accepted")
        self.assertTrue(cues.q.empty())
        with patch("ramble.state.run_audio") as player:
            cues.play("error", time.monotonic())
        player.assert_not_called()

    def test_queued_fast_reply_stays_cancelled_after_resume(self):
        fast = FastLane(self.cfg)
        now = time.time()
        item = {"seg": {"id": "segment", "started_at": now - 1, "ended_at": now},
                "text": "Ramble, do some work"}
        self.pause()
        state.write(True)
        with patch.object(fast, "run_codex") as model, patch.object(speech, "speak") as speaker:
            fast.respond(self.conn, [item])
        model.assert_not_called()
        speaker.assert_not_called()
        self.assertFalse(fast.history)
        fresh = {"id": "new", "started_at": time.time(), "ended_at": time.time()}
        self.assertTrue(fast.offer(self.conn, fresh, "Ramble, tell me the time"))

    def test_pause_during_fast_generation_cancels_output(self):
        fast = FastLane(dict(self.cfg, fast_speak=True))
        now = time.time()
        item = {"seg": {"id": "segment", "started_at": now, "ended_at": now}, "text": "Ramble, hello"}
        def generate(_, **kwargs):
            self.pause()
            return {"reply": "Hello", "handoff": True}
        with patch.object(fast, "run_codex", side_effect=generate), patch.object(speech, "speak") as speaker:
            fast.respond(self.conn, [item])
        speaker.assert_not_called()
        self.assertFalse(fast.history)

    def test_router_defers_triage_then_resumes_with_no_agents(self):
        self.pause()
        with patch.object(self.router, "run_triage", return_value=self.decision) as model, \
                patch("ramble.router.subprocess.run") as child, patch.object(speech, "speak") as speaker, \
                patch("ramble.router.notify") as notification:
            self.router.triage(self.conn, self.thought)
            model.assert_not_called()
            state.write(True)
            self.router.triage(self.conn, self.thought)
        model.assert_called_once()
        child.assert_not_called()  # cap=0; do not register or spawn an agent
        speaker.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT status FROM thoughts").fetchone()[0], "done")
        self.assertEqual(self.conn.execute("SELECT status FROM dispatches").fetchone()[0], "skipped_cap")
        self.assertTrue(notification.called)

    def test_pause_during_triage_holds_result(self):
        def generate(*_):
            self.pause()
            return self.decision
        with patch.object(self.router, "run_triage", side_effect=generate), \
                patch.object(self.router, "dispatch") as dispatch, patch.object(self.router, "ask") as ask, \
                patch("ramble.router.notify") as notification:
            self.router.triage(self.conn, self.thought)
        dispatch.assert_not_called()
        ask.assert_not_called()
        notification.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT status FROM thoughts").fetchone()[0], "closed")

    def test_side_effect_guards_block_dispatch_questions_and_both_notifications(self):
        self.pause()
        cfg = dict(self.cfg, notify=True, ntfy_topic="test-unused", dispatch_daily_cap=100,
                   speak_questions=True)
        router = Router(cfg, threading.Event())
        with patch("ramble.router.subprocess.run") as child, \
                patch("ramble.router.urllib.request.urlopen") as push, patch.object(speech, "speak") as speaker:
            self.assertEqual(router.dispatch(self.conn, self.thought, self.item)["status"], "skipped_paused")
            router.ask("Are you awake?")
            notify(cfg, "Ready")
        child.assert_not_called()
        push.assert_not_called()
        speaker.assert_not_called()

    def test_speech_and_direct_playback_refuse_during_pause(self):
        self.pause()
        with patch.object(speech, "synthesize") as generate, patch("ramble.state.subprocess.Popen") as player:
            with self.assertRaises(state.PausedError):
                speech.speak("Hello", self.cfg)
            with self.assertRaises(state.PausedError):
                speech.play_audio("unused.wav", self.cfg)
        generate.assert_not_called()
        player.assert_not_called()

    def test_pause_and_resume_during_synthesis_cancels_stale_speech(self):
        def synthesize(*_, **kwargs):
            self.pause()
            state.write(True)
            return speech.SpeechResult(Path("unused.wav"), "test", 1)
        with patch.object(speech, "synthesize", side_effect=synthesize), patch.object(speech, "play_audio") as player:
            with self.assertRaises(state.PausedError):
                speech.speak("Hello", self.cfg)
        player.assert_not_called()

    def test_active_player_is_terminated_when_paused(self):
        player = MagicMock()
        player.__enter__.return_value = player
        player.poll.return_value = None
        def playing(*_, **kwargs):
            self.pause()
            raise subprocess.TimeoutExpired("mock-player", .1)
        waits = 0
        def wait(*args, **kwargs):
            nonlocal waits
            waits += 1
            if waits == 1:
                return playing()
            return 0
        player.wait.side_effect = wait
        with patch("ramble.state.subprocess.Popen", return_value=player):
            with self.assertRaises(state.PausedError):
                state.run_audio(["unused-test-player"], timeout=2)
        player.terminate.assert_called_once()

    def test_player_success_failure_and_timeout_are_visible(self):
        player = MagicMock()
        player.__enter__.return_value = player
        player.poll.return_value = 0
        with patch("ramble.state.subprocess.Popen", return_value=player):
            player.wait.return_value = 0
            state.run_audio(["unused-test-player"], timeout=2)
            player.wait.return_value = 1
            with self.assertRaises(subprocess.CalledProcessError):
                state.run_audio(["unused-test-player"], timeout=2)
            with self.assertRaises(subprocess.TimeoutExpired):
                state.run_audio(["unused-test-player"], timeout=0)


if __name__ == "__main__":
    unittest.main()
