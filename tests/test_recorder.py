"""Short wake-word capture without a microphone or speaker."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from ramble import config, state
from ramble.daemon import Daemon
from ramble.dictation import Dictation
from ramble.recorder import CHUNK_S, Recorder, Segmenter
from ramble.vad import CHUNK


class ShortSpeechTests(unittest.TestCase):
    def capture(self, voiced_chunks: int, probability: float) -> list[np.ndarray]:
        saved = []
        seg = Segmenter(dict(config.DEFAULTS), lambda _ts, audio: saved.append(audio))
        chunk = np.zeros(CHUNK, dtype=np.float32)
        for i in range(voiced_chunks):
            seg._feed(chunk, 100 + i * CHUNK_S, probability)
        seg.flush()
        return saved

    def test_short_confident_wake_word_is_kept(self):
        self.assertEqual(len(self.capture(11, .95)), 1)

    def test_short_uncertain_noise_and_tone_are_dropped(self):
        self.assertEqual(self.capture(11, .6), [])
        self.assertEqual(self.capture(6, .99), [])


class MicrophoneRecoveryTests(unittest.TestCase):
    """Inject device/worker failures; no microphone, playback, models or agents."""

    def setUp(self):
        self.now = 100.0
        self.enterContext(patch("ramble.recorder.time.monotonic", side_effect=lambda: self.now))
        self.threads = []
        self.streams = []
        self.enterContext(patch("ramble.recorder.threading.Thread", side_effect=self.new_thread))
        self.open_stream = self.enterContext(patch("sounddevice.InputStream", side_effect=self.new_stream))
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False, speak_questions=False)
        self.recorder = Recorder(self.cfg, threading.Event())
        self.recorder.on_failure = Mock()
        self.addCleanup(self.recorder.stop)

    def new_thread(self, **kwargs):
        worker = Mock()
        worker.is_alive.return_value = False
        worker.start.side_effect = lambda: setattr(worker.is_alive, "return_value", True)
        worker.join.side_effect = lambda **_: setattr(worker.is_alive, "return_value", False)
        self.threads.append(worker)
        return worker

    def new_stream(self, **kwargs):
        stream = Mock(active=False, device=1)
        stream.callback = kwargs["callback"]
        stream.start.side_effect = lambda: setattr(stream, "active", True)
        stream.abort.side_effect = lambda: setattr(stream, "active", False)
        self.streams.append(stream)
        return stream

    def deliver(self, value, *, processed=True):
        audio = np.full((CHUNK, 1), value, dtype=np.float32)
        self.streams[-1].callback(audio, CHUNK, None, None)
        if processed:
            self.recorder.q.get_nowait()
            self.recorder._last_processed_at = self.now

    def test_stalled_callbacks_report_inactive_and_reopen(self):
        self.recorder.start()
        self.assertTrue(self.recorder.active)
        self.now += 6
        self.assertFalse(self.recorder.active)
        self.recorder.start()
        self.assertEqual(len(self.streams), 2)
        self.streams[0].abort.assert_called_once()
        self.streams[0].close.assert_called_once()
        self.assertTrue(self.recorder.active)
        self.recorder.on_failure.assert_called_once()

    def test_inactive_stream_and_dead_worker_are_recovered(self):
        self.recorder.start()
        for failure in ("device", "worker"):
            with self.subTest(failure=failure):
                self.now += 2
                if failure == "device":
                    self.streams[-1].active = False
                else:
                    self.threads[-1].is_alive.return_value = False
                self.assertFalse(self.recorder.active)
                count = len(self.streams)
                self.recorder.start()
                self.assertEqual(len(self.streams), count + 1)
                self.assertTrue(self.recorder.active)

    def test_live_callbacks_cannot_hide_stalled_processing(self):
        self.recorder.start()
        for _ in range(11):
            self.now += 1
            self.deliver(.01, processed=False)
        self.assertFalse(self.recorder.active)
        self.recorder.start()
        self.assertEqual(len(self.streams), 2)

    def test_exact_zero_input_recovers_but_quiet_noise_does_not(self):
        self.recorder.start()
        for _ in range(30):
            self.now += 1
            self.deliver(0)
            self.recorder.start()
        self.assertEqual(len(self.streams), 2)
        # Noise too quiet to round above 0.0000 in the log is still valid input.
        for _ in range(65):
            self.now += 1
            self.deliver(1e-8)
            self.recorder.start()
        self.assertEqual(len(self.streams), 2)
        self.assertTrue(self.recorder.active)
        self.assertEqual(self.recorder._retry_delay, 1)

    def test_failed_opens_back_off_and_cap_at_thirty_seconds(self):
        self.open_stream.side_effect = RuntimeError("device unavailable")
        with self.assertLogs("ramble.recorder", level="ERROR"):
            for delay in (1, 2, 4, 8, 16, 30, 30):
                count = self.open_stream.call_count
                self.recorder.start()
                self.assertEqual(self.open_stream.call_count, count + 1)
                self.assertFalse(self.recorder.active)
                self.assertIn("device unavailable", self.recorder.error)
                self.now += delay - .1
                self.recorder.start()
                self.assertEqual(self.open_stream.call_count, count + 1)
                self.now += .1
        self.assertEqual(len(self.threads), 7)
        self.assertTrue(all(not worker.is_alive() for worker in self.threads))

    def test_start_failure_closes_partial_stream_and_worker(self):
        stream = self.new_stream(callback=Mock())
        stream.start.side_effect = RuntimeError("start failed")
        self.open_stream.side_effect = None
        self.open_stream.return_value = stream
        with self.assertLogs("ramble.recorder", level="ERROR"):
            self.recorder.start()
        stream.close.assert_called_once()
        self.assertIsNone(self.recorder.stream)
        self.assertIsNone(self.recorder.worker)
        self.assertFalse(self.recorder.active)
        self.assertIn("start failed", self.recorder.error)

    def test_failed_abort_still_closes_and_recovers(self):
        self.recorder.start()
        self.now += 6
        self.streams[0].abort.side_effect = RuntimeError("device disappeared")
        with self.assertLogs("ramble.recorder", level="WARNING"):
            self.recorder.start()
        self.streams[0].close.assert_called_once()
        self.assertTrue(self.recorder.active)
        self.assertEqual(len(self.streams), 2)

    def test_recovery_waits_for_previous_worker_before_replacing_queue(self):
        self.recorder.start()
        previous_queue = self.recorder.q
        self.threads[0].join.side_effect = None  # Simulate a worker blocked in VAD/SQLite.
        self.now += 6
        self.recorder.start()
        self.assertEqual(len(self.streams), 1)
        self.assertIs(self.recorder.q, previous_queue)
        self.assertFalse(self.recorder.active)
        self.assertIn("has not stopped", self.recorder.error)
        self.threads[0].is_alive.return_value = False
        self.now += 2
        self.recorder.start()
        self.assertEqual(len(self.streams), 2)
        self.assertIsNot(self.recorder.q, previous_queue)

    def test_late_retired_stream_callbacks_cannot_enter_new_queue(self):
        self.recorder.start()
        retired_callback = self.streams[0].callback
        self.now += 6
        self.recorder.start()
        last_callback = self.recorder._last_callback_at
        self.now += 1
        retired_callback(np.ones((CHUNK, 1), dtype=np.float32), CHUNK, None, None)
        self.assertTrue(self.recorder.q.empty())
        self.assertEqual(self.recorder._last_callback_at, last_callback)
        self.deliver(.01)
        self.assertTrue(self.recorder.active)

    def test_pause_and_off_stop_unhealthy_stream_and_prevent_recovery(self):
        with tempfile.TemporaryDirectory(prefix="ramble-test-recovery-", dir="/tmp") as tmp:
            home = Path(tmp)
            self.enterContext(patch.multiple(config, HOME=home, STATE_PATH=home / "state.json"))
            daemon = Daemon.__new__(Daemon)
            daemon.recorder = self.recorder
            daemon.wake = threading.Event()
            for paused_until in (None, state.time.time() + 3600):
                state.write(True)
                daemon.sync()
                count = len(self.streams)
                self.now += 6
                self.assertFalse(self.recorder.active)
                selected = state.write(paused_until is not None, paused_until)
                daemon.sync()
                self.now += 60
                daemon.sync()
                self.assertEqual(len(self.streams), count)
                self.assertFalse(self.recorder.running)
                self.assertIsNone(self.recorder.stream)
                self.assertEqual(state.read(), selected)
                status = json.loads((home / "capture-status.json").read_text())
                self.assertFalse(status["active"])
                self.assertFalse(status["failed"])
            # Resuming explicitly resets a previous recovery delay.
            self.recorder._retry_at = self.now + 30
            daemon.sync()
            state.write(True)
            daemon.sync()
            self.assertTrue(self.recorder.active)

    def test_failed_recovery_acknowledges_failure_and_cancels_dictation(self):
        with tempfile.TemporaryDirectory(prefix="ramble-test-recovery-", dir="/tmp") as tmp:
            home = Path(tmp)
            self.enterContext(patch.multiple(config, HOME=home, STATE_PATH=home / "state.json"))
            state.write(True)
            daemon = Daemon.__new__(Daemon)
            daemon.recorder = self.recorder
            daemon.wake = threading.Event()
            daemon.dictation = Dictation(self.recorder)
            self.recorder.on_failure = daemon.dictation.cancel
            daemon.sync()
            desktop = Mock()
            daemon.dictation.toggle(desktop)
            recording = daemon.dictation.recording
            self.assertIsNotNone(recording)
            self.now += 6
            self.open_stream.side_effect = RuntimeError("device unavailable")
            with self.assertLogs("ramble.recorder", level="WARNING"):
                daemon.sync()
            self.assertTrue(recording.cancelled.is_set())
            self.assertIsNone(daemon.dictation.recording)
            status = json.loads((home / "capture-status.json").read_text())
            self.assertFalse(status["active"])
            self.assertTrue(status["failed"])
            daemon.dictation.poll(desktop)
            desktop.copy.assert_not_called()
            desktop.paste.assert_not_called()


if __name__ == "__main__":
    unittest.main()
