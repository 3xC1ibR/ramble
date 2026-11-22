"""Owner-voice embeddings with WeSpeaker ResNet34 (ONNX, CPU) and no torch dependency.

The feature pipeline reproduces Kaldi filterbanks as computed by
``torchaudio.compliance.kaldi.fbank`` (80 bins, 25/10 ms, Hamming window,
dither 0) followed by per-utterance mean normalisation, which is the input
WeSpeaker models were trained on. Scores are cosine similarities between a
clip embedding and the enrolled owner centroid stored in RAMBLE_HOME.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
FRAME_LENGTH = 400  # 25 ms
FRAME_SHIFT = 160  # 10 ms
FFT_SIZE = 512
MEL_BINS = 80
LOW_FREQ = 20.0
PREEMPHASIS = 0.97
MIN_SAMPLES = 8000  # 0.5 s; shorter clips are zero-padded before embedding
MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
MODEL_FILE = "voxceleb_resnet34_LM.onnx"
PROFILE_SCHEMA = 1


def _mel(freq):
    return 1127.0 * np.log(1.0 + np.asarray(freq, dtype=np.float64) / 700.0)


def mel_banks(num_bins: int = MEL_BINS, fft_size: int = FFT_SIZE, sample_rate: int = SAMPLE_RATE,
              low_freq: float = LOW_FREQ) -> np.ndarray:
    """Kaldi triangular mel filters, shape (num_bins, fft_size // 2 + 1); the Nyquist column is zero."""
    half = fft_size // 2
    high_freq = sample_rate / 2.0
    mel_low, mel_high = _mel(low_freq), _mel(high_freq)
    delta = (mel_high - mel_low) / (num_bins + 1)
    fft_mel = _mel(np.arange(half) * sample_rate / fft_size)
    left = mel_low + np.arange(num_bins)[:, None] * delta
    center, right = left + delta, left + 2 * delta
    up = (fft_mel[None, :] - left) / (center - left)
    down = (right - fft_mel[None, :]) / (right - center)
    banks = np.maximum(0.0, np.minimum(up, down))
    return np.pad(banks, ((0, 0), (0, 1))).astype(np.float32)


_BANKS = mel_banks()
_WINDOW = (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(FRAME_LENGTH) / (FRAME_LENGTH - 1))).astype(np.float32)


def fbank(audio: np.ndarray) -> np.ndarray:
    """Log mel filterbank features (frames, 80) for 16 kHz mono float audio in [-1, 1]."""
    x = np.asarray(audio, dtype=np.float32) * 32768.0
    if len(x) < FRAME_LENGTH:
        x = np.pad(x, (0, FRAME_LENGTH - len(x)))
    frames = 1 + (len(x) - FRAME_LENGTH) // FRAME_SHIFT
    idx = np.arange(FRAME_LENGTH)[None, :] + FRAME_SHIFT * np.arange(frames)[:, None]
    f = x[idx].astype(np.float64)
    f -= f.mean(axis=1, keepdims=True)
    shifted = np.concatenate([f[:, :1], f[:, :-1]], axis=1)
    f = f - PREEMPHASIS * shifted
    f *= _WINDOW
    power = np.abs(np.fft.rfft(f, n=FFT_SIZE, axis=1)) ** 2
    energies = power.astype(np.float32) @ _BANKS.T
    return np.log(np.maximum(energies, np.finfo(np.float32).eps)).astype(np.float32)


def default_model_path() -> Path | None:
    """The locally cached WeSpeaker model, if present. Nothing is downloaded here."""
    env = os.environ.get("RAMBLE_OWNER_VOICE_MODEL")
    if env:
        return Path(env).expanduser()
    hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    snapshots = hub / ("models--" + MODEL_REPO.replace("/", "--")) / "snapshots"
    for candidate in sorted(snapshots.glob(f"*/{MODEL_FILE}")):
        return candidate
    return None


def _unit(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    if not np.isfinite(v).all() or norm < 1e-8:
        raise ValueError("invalid speaker embedding")
    return v / norm


class SpeakerEmbedder:
    """Embeds clips with an ONNX speaker model; two CPU threads keep it beside Whisper."""

    def __init__(self, model_path: str | Path, threads: int = 2, session=None):
        self.model_path = Path(model_path)
        if session is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            session = ort.InferenceSession(str(self.model_path), opts, providers=["CPUExecutionProvider"])
        self.session = session
        self.input_name = session.get_inputs()[0].name

    def embed(self, audio: np.ndarray) -> np.ndarray:
        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(audio) < MIN_SAMPLES:
            audio = np.pad(audio, (0, MIN_SAMPLES - len(audio)))
        feats = fbank(audio)
        feats = feats - feats.mean(axis=0, keepdims=True)
        return _unit(self.session.run(None, {self.input_name: feats[None, :, :]})[0])


class OwnerProfile:
    """The enrolled owner voice: a unit centroid plus the calibrated decision threshold."""

    def __init__(self, centroid: np.ndarray, threshold: float, meta: dict | None = None):
        self.centroid = _unit(centroid)
        self.threshold = float(threshold)
        self.meta = dict(meta or {})

    def score(self, embedding: np.ndarray) -> float:
        return float(np.dot(_unit(embedding), self.centroid))

    @classmethod
    def enroll(cls, embeddings, threshold: float, meta: dict | None = None) -> "OwnerProfile":
        rows = [_unit(e) for e in embeddings]
        if len(rows) < 20:
            raise ValueError(f"enrollment needs at least 20 owner clips, got {len(rows)}")
        return cls(np.mean(rows, axis=0), threshold, {**(meta or {}), "enrolled_clips": len(rows),
                                                      "enrolled_at": time.time()})

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema": PROFILE_SCHEMA, "threshold": self.threshold,
                                   "centroid": [round(float(x), 7) for x in self.centroid],
                                   "meta": self.meta}))
        os.replace(tmp, path)
        os.chmod(path, 0o600)

    @classmethod
    def load(cls, path: str | Path) -> "OwnerProfile":
        data = json.loads(Path(path).read_text())
        if data.get("schema") != PROFILE_SCHEMA:
            raise ValueError(f"unsupported owner profile schema {data.get('schema')!r}")
        return cls(np.asarray(data["centroid"], dtype=np.float32), data["threshold"], data.get("meta"))
