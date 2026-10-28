"""Earcon verification without playing speakers or creating agents."""

import tempfile
import threading
import time
import unittest
import fcntl
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from ramble import config
from ramble.cues import AudioCues, tone
from ramble.recorder import Segmenter
from ramble.vad import CHUNK


class CueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-cues-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        paths = patch.multiple(config, HOME=self.home, STATE_PATH=self.home / "state.json")
        paths.start()
        self.addCleanup(paths.stop)

    def test_smooth_quiet_short_and_distinct(self):
        for kind in ("accepted", "error", "working"):
            audio = tone(kind)
            self.assertLess(len(audio) / config.SAMPLE_RATE, .25)
            self.assertLessEqual(float(np.max(np.abs(audio))), .12)
            self.assertAlmostEqual(float(audio[0]), 0)
            self.assertLess(abs(float(audio[-1])), .001)
        self.assertFalse(np.array_equal(tone("accepted"), tone("error")))
        self.assertLess(np.max(np.abs(tone("working"))), np.max(np.abs(tone("accepted"))) * .3)

    def test_real_vad_does_not_record_cues_even_when_repeated_or_louder(self):
        for kind in ("accepted", "error", "working"):
            gains = (.05, .1, .5, 1, 3) if kind == "working" else (.1, 1, 3)
            gaps = (.2, 2.5) if kind == "working" else (.2,)
            for gain in gains:
                for gap in gaps:
                    with self.subTest(kind=kind, gain=gain, gap=gap):
                        recorded = []
                        seg = Segmenter(dict(config.DEFAULTS), lambda *args: recorded.append(args))
                        pulse = np.concatenate([tone(kind) * gain, np.zeros(round(config.SAMPLE_RATE * gap))])
                        audio = np.concatenate([np.zeros(16000), np.tile(pulse, 5), np.zeros(16000)]).astype(np.float32)
                        for i in range(0, len(audio) - CHUNK + 1, CHUNK):
                            seg.feed(audio[i:i + CHUNK], 100 + i / config.SAMPLE_RATE)
                        seg.flush()
                        self.assertEqual(recorded, [])

    def test_acceptance_is_rate_limited_and_queue_is_bounded(self):
        cues = AudioCues({})
        with patch("ramble.cues.time.monotonic", return_value=100):
            cues.emit("accepted")
            cues.emit("accepted")
            self.assertEqual(cues.q.qsize(), 1)
            for _ in range(10):
                cues.emit("error")
            self.assertEqual(cues.q.qsize(), 4)

    def test_disabled_or_silent_mode_never_queues(self):
        for cfg in ({"audio_cues_enabled": False}, {"fast_speak": False}):
            cues = AudioCues(cfg)
            cues.emit("accepted")
            self.assertTrue(cues.q.empty())

    def test_playback_uses_shared_lock_and_no_microphone_guard(self):
        cues = AudioCues({})
        queued = time.monotonic()
        def play(kind, volume, deadline, cancelled):
            self.assertEqual((kind, volume), ("accepted", .5))
            self.assertEqual(deadline, queued + 2)
            self.assertFalse(cancelled())
            with (self.home / "speech.lock").open("a") as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        with patch.object(cues.player, "play", side_effect=play):
            cues.play("accepted", queued)
        self.assertFalse((self.home / "ramble.db").exists())
        self.assertFalse((self.home / "cues").exists())  # No WAV cache or afplay.

    def test_busy_speaker_and_stale_cues_are_skipped(self):
        cues = AudioCues({})
        with patch.object(cues.player, "play") as player:
            cues.play("accepted", time.monotonic() - 3)
            player.assert_not_called()
            with patch("ramble.cues.fcntl.flock", side_effect=BlockingIOError):
                cues.play("accepted", time.monotonic())
            player.assert_not_called()

    def test_pause_resume_and_shutdown_cancel_inflight_cue(self):
        from ramble import state
        cues = AudioCues({})
        stop = threading.Event()
        def play(_kind, _volume, _deadline, cancelled):
            self.assertFalse(cancelled())
            state.write(True, time.time() + 3600)
            self.assertTrue(cancelled())
            state.write(True)
            self.assertTrue(cancelled())  # Old cues stay cancelled after resume.
            return False
        with patch.object(cues.player, "play", side_effect=play):
            cues.play("accepted", time.monotonic(), stop)
        def stopping(_kind, _volume, _deadline, cancelled):
            stop.set()
            self.assertTrue(cancelled())
            return False
        with patch.object(cues.player, "play", side_effect=stopping):
            cues.play("accepted", time.monotonic(), stop)

    def test_worker_prewarms_and_closes_output_even_without_cues(self):
        cues = AudioCues({})
        stop = threading.Event()
        cues.player = Mock()
        cues.player.maintain.side_effect = stop.set
        cues.run_forever(stop)
        cues.player.maintain.assert_called_once()
        cues.player.close.assert_called_once()

    def test_disabling_chimes_still_warms_spoken_replies_and_shares_the_player(self):
        from ramble.speech import shared_player
        cues = AudioCues({'audio_cues_enabled': False, 'fast_speak': True})
        self.assertIs(cues.player, shared_player())
        stop = threading.Event()
        with patch.object(cues.player, 'maintain', side_effect=stop.set) as warm, \
                patch.object(cues.player, 'close'):
            cues.run_forever(stop)
        warm.assert_called_once()

    def test_silent_and_paused_workers_never_open_output(self):
        from ramble import state
        for cfg in ({"fast_speak": False, "speak_questions": False}, {}):
            if not cfg:
                state.write(True, time.time() + 3600)
            cues = AudioCues(cfg)
            stop = threading.Event()
            cues.player = Mock()
            cues.player.close.side_effect = stop.set
            cues.run_forever(stop)
            cues.player.maintain.assert_not_called()

    def test_work_pulses_settle_repeat_and_never_accumulate(self):
        cues = AudioCues({})
        with patch("ramble.cues.time.monotonic", return_value=100) as clock:
            cues.emit("accepted")
            token = cues.start_work(time.time())
            clock.return_value = 100.9
            self.assertIsNone(cues.work_due())
            clock.return_value = 101
            self.assertEqual(cues.work_due(), token)
            clock.return_value = 103.4
            self.assertIsNone(cues.work_due())
            clock.return_value = 103.5
            self.assertEqual(cues.work_due(), token)
            clock.return_value = 200  # Missed pulses never catch up in a burst.
            self.assertEqual(cues.work_due(), token)
            self.assertIsNone(cues.work_due())
            self.assertEqual(cues.q.qsize(), 1)  # Only the original ding is queued.
            cues.end_work(token)
            clock.return_value = 210
            self.assertIsNone(cues.work_due())

    def test_bare_name_error_and_work_disabled_do_not_pulse(self):
        cues = AudioCues({})
        with patch("ramble.cues.time.monotonic", return_value=100) as clock:
            cues.emit("accepted")
            clock.return_value = 105
            self.assertIsNone(cues.work_due())
            cues.start_work(time.time())
            cues.emit("error")
            clock.return_value = 110
            self.assertIsNone(cues.work_due())
            self.assertFalse(cues.work_active())
        disabled = AudioCues({"audio_working_enabled": False})
        disabled.start_work(time.time())
        self.assertFalse(disabled.work_active())
        disabled.emit("accepted")
        self.assertEqual(disabled.q.qsize(), 1)

    def test_work_yields_to_user_speech_and_new_dings(self):
        cues = AudioCues({})
        with patch("ramble.cues.time.monotonic", return_value=100) as clock:
            token = cues.start_work(time.time())
            cues.speech_active = lambda: True
            clock.return_value = 102
            self.assertIsNone(cues.work_due())
            cues.speech_active = lambda: False
            cues.emit("accepted")
            clock.return_value = 102.9
            self.assertIsNone(cues.work_due())
            clock.return_value = 103
            self.assertEqual(cues.work_due(), token)
        def speaking(_kind, _volume, _deadline, cancelled):
            self.assertFalse(cancelled())
            cues.speech_active = lambda: True
            self.assertTrue(cancelled())
            return False
        with patch.object(cues.player, "play", side_effect=speaking):
            cues.play("working", time.monotonic(), work_token=token)

    def test_completed_request_cancels_pulse_without_stopping_new_request(self):
        cues = AudioCues({})
        old = cues.start_work(time.time())
        current = cues.start_work(time.time())
        cues.end_work(old)
        self.assertTrue(cues.work_active(current))
        with patch.object(cues.player, "play") as player:
            cues.play("working", time.monotonic(), work_token=old)
            player.assert_not_called()
        def finish(_kind, _volume, _deadline, cancelled):
            self.assertFalse(cancelled())
            cues.end_work(current)
            self.assertTrue(cancelled())
            return False
        with patch.object(cues.player, "play", side_effect=finish):
            cues.play("working", time.monotonic(), work_token=current)

    def test_pause_cancels_work_and_resume_does_not_restore_old_pulses(self):
        from ramble import state
        cues = AudioCues({})
        cues.start_work(time.time())
        self.assertTrue(cues.work_active())
        state.write(True, time.time() + 3600)
        self.assertFalse(cues.work_active())
        state.write(True)
        self.assertFalse(cues.work_active())
        cues.start_work(time.time())
        self.assertTrue(cues.work_active())

    def test_worker_delivers_ding_before_live_work_pulse(self):
        cues = AudioCues({})
        stop = threading.Event()
        cues.player = Mock()
        played = []
        with patch("ramble.cues.time.monotonic", return_value=100) as clock:
            cues.emit("accepted")
            cues.start_work(time.time())
            def play(kind, _volume, _deadline, cancelled):
                self.assertFalse(cancelled())
                played.append(kind)
                clock.return_value = 102
                if kind == "working":
                    stop.set()
                return True
            cues.player.play.side_effect = play
            cues.run_forever(stop)
        self.assertEqual(played, ["accepted", "working"])
        self.assertFalse(cues.work_active())

    def test_session_pulse_survives_fast_cleanup_and_rechecks_owner_during_playback(self):
        cues = AudioCues({})
        work = (("session", "command", 0, 0), time.time())
        cues.session_work = Mock(return_value=work)
        with patch("ramble.cues.time.monotonic", return_value=100) as clock:
            fast = cues.start_work(time.time())
            cues.end_work(fast)  # Direct routing hands the response to its owner.
            self.assertIsNone(cues.work_due())
            clock.return_value = 101
            self.assertEqual(cues.work_due(), work)
            clock.return_value = 103.4
            self.assertIsNone(cues.work_due())
            clock.return_value = 103.5
            self.assertEqual(cues.work_due(), work)
            cues.speech_active = lambda: True
            clock.return_value = 110
            self.assertIsNone(cues.work_due())
            cues.speech_active = lambda: False
            self.assertEqual(cues.work_due(), work)
        def quiet(_kind, _volume, _deadline, cancelled):
            self.assertFalse(cancelled())
            cues.session_work.return_value = None
            self.assertTrue(cancelled())
            return False
        with patch.object(cues.player, "play", side_effect=quiet):
            cues.play("working", time.monotonic(), work_token=work)

    def test_session_pulse_does_not_duplicate_fast_pulse_or_replay_after_pause(self):
        from ramble import state
        cues = AudioCues({})
        work = (("session", "command", 0, 0), time.time())
        cues.session_work = lambda: work
        with patch("ramble.cues.time.monotonic", return_value=100) as clock:
            fast = cues.start_work(time.time())
            clock.return_value = 101
            self.assertEqual(cues.work_due(), fast)
            self.assertIsNone(cues.work_due())
            cues.end_work(fast)
            self.assertIsNone(cues.work_due())
            clock.return_value = 102
            self.assertEqual(cues.work_due(), work)
            state.write(True, time.time() + 3600)
            self.assertFalse(cues.work_active(work))
            state.write(True)
            clock.return_value = 200
            self.assertIsNone(cues.work_due())

    def test_session_pulse_respects_voice_switch_and_disabled_cues(self):
        from ramble import voice_control
        work = (("session", "command", 0, 0), time.time())
        for cfg in ({}, {"audio_working_enabled": False},
                    {"audio_cues_enabled": False}, {"fast_speak": False}):
            cues = AudioCues(cfg)
            cues.session_work = lambda: work
            if cfg:
                self.assertFalse(cues.work_active(work))
        cues = AudioCues({})
        cues.session_work = lambda: work
        self.assertTrue(cues.work_active(work))
        voice_control.write(False)
        self.assertFalse(cues.work_active(work))
        voice_control.write(True)
        self.assertFalse(cues.work_active(work))
