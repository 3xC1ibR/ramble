import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from ramble import speaker_embedding as se


def tone():
    t = np.arange(8000) / 16000
    return (0.1 * np.sin(2 * np.pi * 440 * t) + 0.02 * np.sin(2 * np.pi * 3000 * t)).astype(np.float32)


class FakeSession:
    def __init__(self):
        self.inputs = []

    def get_inputs(self):
        return [type("Input", (), {"name": "feats"})()]

    def run(self, _outputs, feeds):
        self.inputs.append(feeds["feats"])
        return [np.ones((1, 256), dtype=np.float32)]


class FilterbankTests(unittest.TestCase):
    def test_matches_torchaudio_kaldi_fbank_reference(self):
        # Reference: torchaudio.compliance.kaldi.fbank(x * 32768, num_mel_bins=80, frame_length=25,
        # frame_shift=10, dither=0, window_type='hamming') on the same signal (torchaudio 2.8).
        f = se.fbank(tone())
        self.assertEqual(f.shape, (48, 80))
        np.testing.assert_allclose(f[10, [0, 5, 10, 20, 40, 60, 79]],
                                   [9.3778, 9.3016, 9.2307, 11.822, 9.7757, 11.4927, 10.5074], atol=2e-3)
        self.assertAlmostEqual(float(f.mean()), 11.5293, places=2)

    def test_mel_banks_have_kaldi_shape_and_zero_nyquist_column(self):
        banks = se.mel_banks()
        self.assertEqual(banks.shape, (80, 257))
        self.assertTrue((banks[:, -1] == 0).all())
        self.assertTrue((banks.sum(axis=1) > 0).all())

    def test_short_audio_is_padded_before_embedding(self):
        session = FakeSession()
        vec = se.SpeakerEmbedder("unused.onnx", session=session).embed(np.zeros(1600, dtype=np.float32))
        self.assertEqual(session.inputs[0].shape, (1, 48, 80))  # padded to 0.5 s
        self.assertAlmostEqual(float(np.linalg.norm(vec)), 1.0, places=5)


class OwnerProfileTests(unittest.TestCase):
    def test_enrollment_requires_twenty_clips(self):
        with self.assertRaises(ValueError):
            se.OwnerProfile.enroll([np.ones(4)] * 19, 0.2)

    def test_profile_round_trip_and_score(self):
        rng = np.random.default_rng(0)
        base = rng.normal(size=256)
        prof = se.OwnerProfile.enroll([base + rng.normal(scale=0.1, size=256) for _ in range(25)], 0.2,
                                      {"source": "test"})
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            path = Path(home) / "owner-voice.json"
            prof.save(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            loaded = se.OwnerProfile.load(path)
            self.assertEqual(json.loads(path.read_text())["schema"], 1)
        self.assertEqual(loaded.meta["enrolled_clips"], 25)
        self.assertGreater(loaded.score(base), 0.95)
        self.assertLess(abs(loaded.score(rng.normal(size=256))), 0.3)


if __name__ == "__main__":
    unittest.main()
