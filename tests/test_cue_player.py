"""Persistent audio callback/protocol regression tests, with no sound hardware."""

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

from ramble import config, cue_player
from ramble.cue_player import CueBuffer, CuePlayer, tone


class BufferTests(unittest.TestCase):
    def feed(self, audio, frames, current=100, dac=100.02, underflow=False):
        output = np.full((frames, 2), 9, dtype=np.float32)
        timing = SimpleNamespace(currentTime=current, outputBufferDacTime=dac)
        with patch.object(cue_player.time, "monotonic", return_value=current):
            audio.callback(output, frames, timing, SimpleNamespace(output_underflow=underflow))
        return output

    def test_variable_buffers_preserve_samples_volume_channels_and_dac_completion(self):
        for rate in (16000, 48000):
            for kind in ("accepted", "error", "working"):
                with self.subTest(rate=rate, kind=kind):
                    audio = CueBuffer(rate)
                    audio.submit(dict(id=1, kind=kind, volume=.5, deadline=102))
                    blocks = []
                    elapsed = 0
                    while audio.current.ends_at is None:
                        frames = (127, 512, 1000)[len(blocks) % 3]
                        blocks.append(self.feed(audio, frames, 100 + elapsed, 100.02 + elapsed))
                        elapsed += frames / rate
                        self.assertIsNone(audio.take_result())
                    output = np.concatenate(blocks)
                    expected = tone(kind, rate) * .5
                    np.testing.assert_array_equal(output[:len(expected), 0], expected)
                    np.testing.assert_array_equal(output[:, 0], output[:, 1])
                    self.assertFalse(np.any(output[len(expected):]))
                    end = audio.current.ends_at
                    self.feed(audio, 64, end - .001, end + .02)
                    self.assertIsNone(audio.take_result())
                    self.feed(audio, 64, end + .001, end + .02)
                    self.assertEqual(audio.take_result(), dict(event="result", id=1, status="played"))
                    self.assertFalse(np.any(self.feed(audio, 64)))

    def test_late_start_never_plays_and_underflow_is_not_reported_as_success(self):
        audio = CueBuffer(16000)
        audio.submit(dict(id=1, kind="accepted", volume=.5, deadline=99))
        self.assertFalse(np.any(self.feed(audio, 512)))
        self.assertEqual(audio.take_result()["status"], "expired")
        audio.submit(dict(id=2, kind="error", volume=.5, deadline=101))
        self.feed(audio, 512)
        self.assertFalse(np.any(self.feed(audio, 512, underflow=True)))
        self.assertEqual(audio.take_result()["status"], "failed")


# The actual helper and callback run in a real subprocess. Only sounddevice and
# CoreAudio discovery are replaced. Buffers go to NumPy arrays, never a speaker.
SILENT_HELPER = r'''
import os, sys, threading, time
from types import SimpleNamespace
from pathlib import Path
import numpy as np
from ramble import cue_player
mode, marker = sys.argv[1:]
class Monitor:
    def read(self):
        return int(Path(marker).exists())
class Output:
    def __init__(self, **kw):
        self.kw = kw
    def start(self):
        if mode == 'hung_open':
            time.sleep(60)
        def pump():
            start = time.monotonic()
            while True:
                if mode == 'stalled' and time.monotonic() - start > .35:
                    return
                if mode == 'hung_play' and self.kw['callback'].__self__.current is not None:
                    return
                now = time.monotonic()
                output = np.zeros((480, self.kw['channels']), dtype=np.float32)
                self.kw['callback'](output, 480,
                    SimpleNamespace(currentTime=now, outputBufferDacTime=now + .01),
                    SimpleNamespace(output_underflow=(mode == 'underflow' and
                        self.kw['callback'].__self__.current is not None and
                        self.kw['callback'].__self__.current.position > 0)))
                time.sleep(.01)
        threading.Thread(target=pump, daemon=True).start()
sys.modules['sounddevice'] = SimpleNamespace(OutputStream=Output,
    query_devices=lambda **kw: dict(default_samplerate=48000, max_output_channels=2))
cue_player.DefaultOutput = Monitor
cue_player.main()
'''


class PlayerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-player-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.marker = self.home / "device-changed"
        self.mode = "normal"
        self.children = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            self.assertEqual(args[0], [sys.executable, '-u', '-m', 'ramble.cue_player'])
            child = real_popen([sys.executable, "-u", "-c", SILENT_HELPER,
                                self.mode, str(self.marker)], **kwargs)
            self.children.append(child)
            return child
        for p in (patch.object(config, "HOME", self.home),
                  patch.object(cue_player.subprocess, "Popen", side_effect=launch),
                  patch.object(cue_player, "STARTUP_TIMEOUT", .8),
                  patch.object(cue_player, "HEALTH_TIMEOUT", .6),
                  patch.object(cue_player, "RETRY_DELAY", .03)):
            p.start()
            self.addCleanup(p.stop)
        self.player = CuePlayer()
        self.addCleanup(self.player.close)

    def wait_for(self, predicate, timeout=4):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.player.maintain()
            if predicate():
                return
            time.sleep(.02)
        self.fail("silent helper did not reach expected state")

    def ready(self):
        self.wait_for(lambda: self.player.ready)
        return self.player.process

    def test_reuses_warm_output_and_confirms_both_cues(self):
        first = self.ready()
        for kind in ("accepted", "working", "working", "error", "accepted"):
            self.assertTrue(self.player.play(kind, .5, time.monotonic() + 2, lambda: False))
            self.assertIs(self.player.process, first)
        self.assertEqual(len(self.children), 1)

    def test_hung_open_is_reaped_and_next_helper_recovers(self):
        self.mode = "hung_open"
        self.player.maintain()
        failed = self.player.process
        self.mode = "normal"
        self.wait_for(lambda: self.player.ready and self.player.process is not failed)
        self.assertIsNotNone(failed.poll())
        self.assertTrue(self.player.play("accepted", .5, time.monotonic() + 2, lambda: False))

    def test_callback_stall_is_detected_even_with_live_process(self):
        self.mode = "stalled"
        failed = self.ready()
        self.mode = "normal"
        self.wait_for(lambda: self.player.ready and self.player.process is not failed)
        self.assertIsNotNone(failed.poll())
        self.assertTrue(self.player.play("error", .5, time.monotonic() + 2, lambda: False))

    def test_stall_during_cue_retries_on_fresh_stream_within_deadline(self):
        self.mode = "hung_play"
        failed = self.ready()
        self.mode = "normal"
        # Keep production's health deadline: the per-cue bound must recover
        # before the idle health watchdog, while the cue is still fresh.
        with patch.object(cue_player, "HEALTH_TIMEOUT", 1.5):
            self.assertTrue(self.player.play("accepted", .5, time.monotonic() + 2, lambda: False))
        self.assertIsNot(self.player.process, failed)
        self.assertIsNotNone(failed.poll())

    def test_crash_and_output_device_change_reopen_output(self):
        failed = self.ready()
        failed.kill()
        failed.wait(timeout=1)
        second = self.ready()
        self.assertIsNot(failed, second)
        self.marker.touch()
        self.wait_for(lambda: self.player.ready and self.player.process is not second)
        self.assertIsNotNone(second.poll())
        self.assertTrue(self.player.play("accepted", .5, time.monotonic() + 2, lambda: False))

    def test_cancelled_or_expired_cue_never_spawns_player(self):
        self.assertFalse(self.player.play("accepted", .5, time.monotonic() - 1, lambda: False))
        self.assertFalse(self.player.play("accepted", .5, time.monotonic() + 2, lambda: True))
        self.assertFalse(self.children)

    def test_cancellation_during_playback_reaps_helper(self):
        first = self.ready()
        cancel_at = time.monotonic() + .05
        self.assertFalse(self.player.play("error", .5, time.monotonic() + 2,
                                          lambda: time.monotonic() >= cancel_at))
        self.assertIsNone(self.player.process)
        self.assertIsNotNone(first.poll())

    def test_lost_parent_pipe_exits_helper_even_during_hung_open(self):
        self.mode = "hung_open"
        self.player.maintain()
        child = self.player.process
        child.stdin.close()
        child.wait(timeout=3)
        self.assertEqual(child.returncode, 0)

    def speech_file(self, rate=24000, seconds=.1):
        path = self.home / f"speech-{rate}.wav"
        t = np.arange(round(rate * seconds)) / rate
        sf.write(path, .1 * np.sin(2 * np.pi * 1000 * t), rate)
        return path

    def test_cues_and_mixed_rate_speech_reuse_one_warm_stream(self):
        first = self.ready()
        starts = []
        for rate in (22050, 24000, 44100, 48000):
            self.assertTrue(self.player.play('accepted', .5, time.monotonic()+2, lambda: False))
            requested = time.monotonic()
            self.player.play_file(self.speech_file(rate), timeout=2, cancelled=lambda: False,
                                  on_start=starts.append)
            self.assertLess(starts[-1]['first_frame_mono_s'] - requested, .5)
            self.assertIs(self.player.process, first)
        self.assertEqual(len(starts), 4)
        self.assertEqual(len(self.children), 1)
        self.assertFalse(self.player.started)

    def test_speech_uses_its_duration_not_the_short_cue_deadline(self):
        self.ready()
        started = time.monotonic()
        self.player.play_file(self.speech_file(seconds=1.2), timeout=3, cancelled=lambda: False)
        self.assertGreaterEqual(time.monotonic() - started, 1.2)

    def test_interrupted_speech_is_failed_without_replay_and_next_reply_recovers(self):
        for mode in ('hung_play', 'underflow'):
            with self.subTest(mode=mode):
                self.player.close()
                self.mode = mode
                failed = self.ready()
                children = len(self.children)
                with self.assertRaises(cue_player.PlaybackError):
                    self.player.play_file(self.speech_file(), timeout=2, cancelled=lambda: False)
                self.assertEqual(len(self.children), children)
                self.assertIsNotNone(failed.poll())
                self.mode = 'normal'
                self.player.play_file(self.speech_file(), timeout=3, cancelled=lambda: False)
                self.assertEqual(len(self.children), children+1)

    def test_device_change_during_speech_does_not_repeat_words(self):
        failed = self.ready()
        starts = []
        def started(receipt):
            starts.append(receipt)
            self.marker.touch()
        with self.assertRaises(cue_player.PlaybackError):
            self.player.play_file(self.speech_file(seconds=.6), timeout=2,
                                  cancelled=lambda: False, on_start=started)
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(self.children), 1)
        self.assertIsNotNone(failed.poll())

    def test_cancelled_speech_never_opens_output_and_mid_reply_cancel_stops_it(self):
        with self.assertRaises(cue_player.PlaybackCancelled):
            self.player.play_file(self.speech_file(), timeout=2, cancelled=lambda: True)
        self.assertFalse(self.children)
        first = self.ready()
        started = []
        with self.assertRaises(cue_player.PlaybackCancelled):
            self.player.play_file(self.speech_file(), timeout=2, cancelled=lambda: bool(started),
                                  on_start=started.append)
        self.assertIsNotNone(first.poll())
        self.assertEqual(len(started), 1)

    def test_shutdown_interrupts_speech_and_cannot_reopen_output(self):
        import threading
        first = self.ready()
        started = threading.Event()
        errors = []
        path = self.speech_file(seconds=2)
        def deliver():
            try:
                self.player.play_file(path, timeout=3, cancelled=lambda: False,
                                      on_start=lambda _: started.set())
            except cue_player.PlaybackCancelled as exc:
                errors.append(exc)
        thread = threading.Thread(target=deliver)
        thread.start()
        self.assertTrue(started.wait(2))
        self.player.shutdown()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsNotNone(first.poll())
        self.assertFalse(self.player.maintain())


if __name__ == "__main__":
    unittest.main()
