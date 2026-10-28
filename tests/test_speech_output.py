"""Verify output samples and DAC timing without opening any audio device."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

from ramble import cue_player
from ramble.cue_player import CueBuffer, load_audio


class SpeechBufferTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-output-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'speech.wav'

    def test_resampling_preserves_duration_pitch_and_stereo(self):
        for rate in (22050, 24000, 44100, 48000):
            for channels in (1, 2):
                with self.subTest(rate=rate, channels=channels):
                    t = np.arange(rate) / rate
                    signal = (.2*np.sin(2*np.pi*1000*t)).astype(np.float32)
                    source = signal if channels == 1 else np.column_stack([signal, -signal])
                    sf.write(self.path, source, rate, subtype='FLOAT')
                    samples = load_audio(str(self.path), 48000, 2)
                    self.assertEqual(samples.shape, (48000, 2))
                    self.assertEqual(samples.dtype, np.float32)
                    self.assertEqual(np.argmax(np.abs(np.fft.rfft(samples[:, 0]))), 1000)
                    self.assertAlmostEqual(float(np.sqrt(np.mean(samples[100:-100, 0]**2))),
                                           .2/np.sqrt(2), delta=.001)
                    np.testing.assert_array_equal(samples[:, 0], samples[:, 1] * (1 if channels == 1 else -1))
                    if rate == 48000:
                        np.testing.assert_array_equal(samples[:, 0], signal)

    def test_callback_delivers_each_frame_once_and_waits_for_last_dac_frame(self):
        samples = np.column_stack([np.linspace(-.2, .2, 1000), np.linspace(.1, -.1, 1000)]).astype(np.float32)
        sf.write(self.path, samples, 48000, subtype='FLOAT')
        buffer = CueBuffer(48000)
        buffer.submit(dict(id=7, kind='speech', path=str(self.path), deadline=101))
        blocks = []
        offset = 0
        for frames in (127, 512, 700):
            now = 100 + offset / 48000
            output = np.zeros((frames, 2), dtype=np.float32)
            with patch.object(cue_player.time, 'monotonic', return_value=now):
                buffer.callback(output, frames,
                                SimpleNamespace(currentTime=now, outputBufferDacTime=now+.02),
                                SimpleNamespace(output_underflow=False))
            blocks.append(output)
            offset += frames
            self.assertIsNone(buffer.take_result())
        output = np.concatenate(blocks)
        np.testing.assert_array_equal(output[:1000], samples)
        self.assertFalse(np.any(output[1000:]))
        first = buffer.take_started()
        self.assertAlmostEqual(first['first_frame_mono_s'], 100.02)
        self.assertIsNone(buffer.take_started())
        end = buffer.current.ends_at
        self.assertAlmostEqual(end, 100.02+1000/48000)
        for now, expected in ((end-.001, None), (end+.001, 'played')):
            output.fill(9)
            buffer.callback(output, len(output),
                            SimpleNamespace(currentTime=now, outputBufferDacTime=now+.02),
                            SimpleNamespace(output_underflow=False))
            result = buffer.take_result()
            self.assertEqual(result['status'] if result else None, expected)
            self.assertFalse(np.any(output))

    def test_invalid_and_expired_files_cannot_report_played(self):
        for samples in (np.array([], dtype=np.float32), np.array([np.nan], dtype=np.float32)):
            sf.write(self.path, samples, 24000, subtype='FLOAT')
            with self.assertRaises(ValueError):
                load_audio(str(self.path), 48000, 2)
        sf.write(self.path, np.ones(100), 24000)
        buffer = CueBuffer(48000)
        buffer.submit(dict(id=8, kind='speech', path=str(self.path), deadline=0))
        output = np.ones((512, 2), dtype=np.float32)
        buffer.callback(output, 512, SimpleNamespace(currentTime=100, outputBufferDacTime=100.02),
                        SimpleNamespace(output_underflow=False))
        self.assertFalse(np.any(output))
        self.assertIsNone(buffer.take_started())
        self.assertEqual(buffer.take_result()['status'], 'expired')


if __name__ == '__main__':
    unittest.main()
