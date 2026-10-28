"""Visual feedback with real callback buffers; no devices, agents or playback."""
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from ramble import config, state, voice_control
from ramble.activity import ActivityMonitor, OutputMeter, display_level, resolve_activity
from ramble.cue_player import CueBuffer, Playback
from ramble.cues import AudioCues
from ramble.sessions import Sessions
from ramble.session_store import SessionStore


class ActivityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-mini-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        paths = patch.multiple(config, HOME=self.home, STATE_PATH=self.home / "state.json")
        paths.start()
        self.addCleanup(paths.stop)

    def test_audio_levels_handle_silence_quiet_speech_and_invalid_values(self):
        for value in (0, -1, float("nan"), float("inf")):
            self.assertEqual(display_level(value), 0)
        self.assertGreater(display_level(.005), .2)
        self.assertGreater(display_level(.1), display_level(.005))
        self.assertEqual(display_level(10), 1)

    def test_speech_capture_and_pause_take_precedence_over_work(self):
        flags = dict(enabled=True, capture=True, processing=True, thinking=True, working=True)
        self.assertEqual(resolve_activity(**flags).mode, "thinking")
        self.assertEqual(resolve_activity(**flags, awake=True).mode, "awake")
        self.assertEqual(resolve_activity(**flags, hearing=True, awake=True).mode, "listening")
        self.assertEqual(resolve_activity(**flags, hearing=True, speaking=True).mode, "speaking")
        self.assertEqual(resolve_activity(**flags, speaking=True, paused=True).mode, "paused")
        self.assertEqual(resolve_activity(working=True).mode, "working")
        self.assertEqual(resolve_activity(speaking=True).mode, "speaking")
        self.assertEqual(resolve_activity(enabled=True, error="device gone").mode, "error")

    def test_meter_follows_output_samples_and_clears_on_completion(self):
        buffer = CueBuffer(1000, 2)
        samples = np.concatenate((np.full((100, 2), .2), np.zeros((100, 2)))).astype(np.float32)
        buffer.current = Playback(1, samples, deadline=110, report_start=True)
        writer, reader = OutputMeter(self.home), OutputMeter(self.home)
        for now, expected in ((100, .2), (100.1, 0.0)):
            output = np.zeros((100, 2), dtype=np.float32)
            with patch("ramble.cue_player.time.monotonic", return_value=now):
                buffer.callback(output, 100,
                                SimpleNamespace(currentTime=now, outputBufferDacTime=now+.02),
                                SimpleNamespace(output_underflow=False))
            writer.publish(buffer.meter_sample, now=now)
            active, level = reader.read(now=now+.03)
            self.assertTrue(active)
            self.assertAlmostEqual(level, expected, places=5)
        buffer.callback(output, 100, SimpleNamespace(currentTime=100.3, outputBufferDacTime=100.32),
                        SimpleNamespace(output_underflow=False))
        writer.publish(buffer.meter_sample, now=100.3)
        self.assertEqual(reader.read(now=100.3), (False, 0))

    def test_chimes_are_not_speech_and_meter_expires_after_crash(self):
        buffer = CueBuffer(16000)
        buffer.submit(dict(id=1, kind="accepted", volume=.5, deadline=110))
        with patch("ramble.cue_player.time.monotonic", return_value=100):
            buffer.callback(np.zeros((512, 2)), 512,
                            SimpleNamespace(currentTime=100, outputBufferDacTime=100.02),
                            SimpleNamespace(output_underflow=False))
        self.assertIsNone(buffer.meter_sample)
        writer, reader = OutputMeter(self.home), OutputMeter(self.home)
        writer.publish((100, .1), now=100)
        self.assertTrue(reader.read(now=100.2)[0])
        self.assertFalse(reader.read(now=100.4)[0])
        self.assertFalse(reader.read(now=20)[0])  # a prior boot's monotonic timestamp
        writer.path.write_text("broken")
        self.assertFalse(reader.read(now=100)[0])
        writer.path.write_text(json.dumps({"at": 100, "level": "nan", "active": True}))
        self.assertFalse(reader.read(now=100)[0])

    def test_mute_preserves_wake_and_work_but_pause_cancels_visual_work(self):
        state.write(True)
        voice_control.write(False)
        cues = AudioCues({"audio_cues_enabled": False, "fast_speak": False})
        now = time.monotonic()
        self.assertFalse(cues.emit("accepted"))
        self.assertGreaterEqual(cues.last_wake, now)
        cues.start_work(time.time())
        self.assertTrue(cues.visual_work_active())
        self.assertFalse(cues.work_active())
        state.write(True, time.time() + 3600)
        self.assertFalse(cues.visual_work_active())

    def test_foreground_quiet_work_is_visible_and_idle_is_not(self):
        sessions = Sessions({}, store=SessionStore(self.home))
        with sessions.store.connect(True) as conn:
            conn.execute("INSERT INTO work_sessions(id,name,goal,cwd,kind,status,foreground,epoch,"
                         "connected,created_at,updated_at,quiet) VALUES ('s','Test','','/tmp','work',"
                         "'running',1,0,1,100,100,1)")
            conn.execute("INSERT INTO session_commands(id,session_id,action,payload,status,epoch,created_at) "
                         "VALUES ('c','s','message','{}','completed',0,100)")
        self.assertIsNone(sessions.thinking_work())
        self.assertIsNotNone(sessions.thinking_work(include_quiet=True))
        with sessions.store.connect(True) as conn:
            conn.execute("UPDATE work_sessions SET status='idle'")
        self.assertIsNone(sessions.thinking_work(include_quiet=True))

    def test_monitor_recovers_from_speaking_to_work_and_pause(self):
        state.write(True)
        cues = AudioCues({})
        daemon = SimpleNamespace(
            stop=threading.Event(), sessions=None,
            recorder=SimpleNamespace(active=True, speech_active=False, error=None),
            fast=SimpleNamespace(cues=cues, name_only_until=0, busy=False, q=queue.Queue()),
            transcriber=SimpleNamespace(backlog=0), router=SimpleNamespace(current=None))
        monitor = ActivityMonitor(daemon)
        monitor.update()
        self.assertEqual(monitor.current.mode, "idle")
        daemon.transcriber.backlog = 1
        monitor.update()
        self.assertEqual(monitor.current.mode, "processing")
        cues.start_work(time.time())
        monitor.update()
        self.assertEqual(monitor.current.mode, "thinking")
        meter = OutputMeter(self.home)
        meter.publish((time.monotonic(), .12))
        monitor.update()
        self.assertEqual(monitor.current.mode, "speaking")
        meter.publish(None)
        monitor.update()
        self.assertEqual(monitor.current.mode, "thinking")
        state.write(True, time.time() + 3600)
        monitor.update()
        self.assertEqual(monitor.current.mode, "paused")


if __name__ == "__main__":
    unittest.main()
