"""Bound MLX's resident model memory and reusable buffers in the transcriber.

MLX reads the residency refresh environment variable when it creates the Metal
device. Configure it before importing mlx_whisper (which imports mlx.core).
These process-wide controls belong to Ramble's single transcription worker.
"""

from __future__ import annotations

import importlib
import logging
import math
import os

from . import latency

log = logging.getLogger("ramble.transcribe")
MIB = 1024 * 1024
REFRESH_ENV = "MLX_METAL_RESIDENCY_REFRESH_INTERVAL_MS"


def _number(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return int(value)


class RecognitionMemory:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.mx = None
        self.prepared = False
        self.wired_limit = 0
        self.cache_limit = None
        self._previous_wired = self._previous_cache = None
        self._resident = False
        self._wiring_failed = False

    def prepare(self) -> None:
        if self.prepared:
            return
        self.prepared = True
        try:
            wired = _number(self.cfg.get("whisper_wired_mb", 2048), "whisper_wired_mb") * MIB
            cache = self.cfg.get("whisper_cache_mb", 128)
            cache = None if cache is None else _number(cache, "whisper_cache_mb") * MIB
            refresh = _number(self.cfg.get("whisper_residency_refresh_ms", 1000), "whisper_residency_refresh_ms")
            # Explicitly zero this when disabled, including in baseline benchmarks.
            os.environ[REFRESH_ENV] = str(min(60000, max(100, refresh)) if wired and refresh else 0)
            self.mx = importlib.import_module("mlx.core")
            info = self.mx.device_info()
            # Never reserve more than a quarter of the machine, even with an
            # accidentally oversized override. Respect Metal's own bound too.
            self.wired_limit = min(wired, int(info["memory_size"]) // 4,
                                   int(info["max_recommended_working_set_size"]))
            if cache is not None:
                cache_limit = min(cache, int(info["memory_size"]) // 16)
                self._previous_cache = self.mx.set_cache_limit(cache_limit)
                self.cache_limit = cache_limit
            self.set_resident(True)
            latency.event("transcription_memory_configured", refresh_ms=int(os.environ[REFRESH_ENV]),
                          **self.snapshot())
            log.info("whisper memory: resident budget %.0f MiB; cache %s; refresh %s ms",
                     self.wired_limit / MIB,
                     "MLX default" if self.cache_limit is None else f"{self.cache_limit / MIB:.0f} MiB",
                     os.environ[REFRESH_ENV])
        except Exception as exc:  # Memory tuning must not disable transcription.
            log.warning("whisper memory configuration unavailable (%s)", exc)
            latency.event("transcription_memory_unavailable", error_type=type(exc).__name__)

    def set_resident(self, enabled: bool) -> None:
        if self.mx is None or self._wiring_failed or enabled == self._resident:
            return
        try:
            previous = self.mx.set_wired_limit(self.wired_limit if enabled else 0)
            if self._previous_wired is None:
                self._previous_wired = previous
            self._resident = enabled
            latency.event("transcription_residency", enabled=enabled, wired_limit_bytes=self.wired_limit if enabled else 0)
        except Exception as exc:
            self._wiring_failed = True
            log.warning("whisper residency unavailable (%s); continuing with pageable memory", exc)
            latency.event("transcription_residency_unavailable", error_type=type(exc).__name__)

    def snapshot(self) -> dict:
        result = dict(wired_limit_bytes=self.wired_limit if self._resident else 0,
                      cache_limit_bytes=self.cache_limit)
        if self.mx is not None:
            for name in ("active", "cache", "peak"):
                try:
                    result[f"mlx_{name}_bytes"] = getattr(self.mx, f"get_{name}_memory")()
                except Exception:
                    pass  # Diagnostics must not interrupt recognition.
        return result

    def close(self) -> None:
        if self.mx is None:
            return
        for kind, previous in (("wired", self._previous_wired), ("cache", self._previous_cache)):
            if previous is not None:
                try:
                    getattr(self.mx, f"set_{kind}_limit")(previous)
                except Exception as exc:
                    log.warning("could not restore MLX %s limit (%s)", kind, exc)
        self._previous_wired = self._previous_cache = None
        self._resident = False
