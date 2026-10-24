"""Silero VAD v5 via onnxruntime (no torch). Feed 512-sample float32 chunks at 16 kHz."""

from __future__ import annotations

import numpy as np
import onnxruntime as ort

from . import config

CHUNK = 512
CONTEXT = 64


class SileroVAD:
    def __init__(self) -> None:
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(config.VAD_MODEL), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self.sr = np.array(config.SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, CONTEXT), dtype=np.float32)

    def __call__(self, chunk: np.ndarray) -> float:
        x = np.concatenate([self.context, chunk.reshape(1, CHUNK).astype(np.float32)], axis=1)
        out, self.state = self.session.run(None, {"input": x, "state": self.state, "sr": self.sr})
        self.context = x[:, -CONTEXT:]
        return float(out[0][0])
