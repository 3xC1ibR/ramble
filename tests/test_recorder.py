"""Short wake-word capture without a microphone or speaker."""

import unittest

import numpy as np

from ramble import config
from ramble.recorder import CHUNK_S, Segmenter
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


if __name__ == "__main__":
    unittest.main()
