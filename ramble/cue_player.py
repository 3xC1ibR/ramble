"""Shared warm output, isolated so a stuck CoreAudio call cannot strand the daemon.

The helper keeps an output-only stream running with silence between cues. Its
receipts and heartbeats come from callback progress, not merely a live process.
Speech files are decoded off the callback thread. No microphone, model, or
runtime database is involved here.
"""

from __future__ import annotations

import atexit
import ctypes
import functools
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from math import gcd
from pathlib import Path

import numpy as np

from . import config
from .activity import OutputMeter

log = logging.getLogger("ramble.cues")
STARTUP_TIMEOUT = 5.0
HEALTH_TIMEOUT = 1.5
PLAY_TIMEOUT = 1.0
RETRY_DELAY = 1.0


class PlaybackError(RuntimeError):
    pass


class PlaybackCancelled(PlaybackError):
    pass


def serialized(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


def tone(kind: str, samplerate: float = config.SAMPLE_RATE) -> np.ndarray:
    """Quiet sine glides with smooth ends, below minimum speech duration."""
    if kind == "working":
        # Two rounded notes a perfect fifth apart. Separate envelopes avoid a
        # sustained harmonic sound that the VAD can mistake for a quiet voice.
        duration = .08
        t = np.arange(round(samplerate * duration)) / samplerate
        envelope = .03 * np.sin(np.pi * t / duration) ** 2
        return np.concatenate([envelope * np.sin(2 * np.pi * 880 * t),
                               np.zeros(round(samplerate * .035)),
                               envelope * np.sin(2 * np.pi * 1320 * t)]).astype(np.float32)
    start, end, duration = {"accepted": (660, 880, .14), "error": (330, 220, .20)}[kind]
    t = np.arange(round(samplerate * duration)) / samplerate
    phase = 2 * np.pi * (start * t + (end - start) * t * t / (2 * duration))
    return (.12 * np.sin(phase) * np.sin(np.pi * t / duration) ** 2).astype(np.float32)


class CuePlayer:
    """One helper shared by cue, speech, and supervision threads in a process."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.process = None
        self.ready = False
        self.last_alive = self.retry_at = 0.0
        self.buffer = b""
        self.receipts = {}
        self.started = {}
        self.serial = 0

    @serialized
    def close(self) -> None:
        process, self.process = self.process, None
        self.ready = False
        self.buffer = b""
        self.receipts.clear()
        self.started.clear()
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=.2)
            process.stdin.close()
            process.stdout.close()

    def shutdown(self) -> None:
        # Signal before acquiring the lock held by an in-flight reply.
        self.stopping.set()
        self.close()

    def _reset(self, reason: str, *, delay: float | None = None) -> None:
        log.warning("audio output restarting: %s", reason)
        self.close()
        self.retry_at = time.monotonic() + (RETRY_DELAY if delay is None else delay)

    @serialized
    def maintain(self) -> bool:
        if self.stopping.is_set():
            return False
        now = time.monotonic()
        if self.process is None:
            if now < self.retry_at:
                return False
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "ramble.cue_player"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                cwd=config.REPO, env=dict(os.environ, RAMBLE_HOME=str(config.HOME)),
            )
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
            self.last_alive = now
        try:
            while True:
                try:
                    chunk = os.read(self.process.stdout.fileno(), 4096)
                except BlockingIOError:
                    break
                if not chunk:
                    self._reset("helper exited")
                    return False
                self.buffer += chunk
                while b"\n" in self.buffer:
                    line, self.buffer = self.buffer.split(b"\n", 1)
                    event = json.loads(line)
                    if event["event"] in ("ready", "alive"):
                        self.last_alive = now
                        if not self.ready:
                            self.ready = True
                            log.info("audio output ready (shared persistent stream, pid=%s)", self.process.pid)
                    elif event["event"] == "result":
                        self.receipts[event["id"]] = event["status"]
                    elif event["event"] == "started":
                        self.started[event["id"]] = event
                    elif event["event"] == "reset":
                        self._reset(event["reason"])
                        return False
            limit = HEALTH_TIMEOUT if self.ready else STARTUP_TIMEOUT
            if now - self.last_alive > limit:
                self._reset("audio callbacks stopped" if self.ready else "output did not become ready")
        except (OSError, ValueError, KeyError) as exc:
            self._reset(str(exc))
        return self.ready

    @serialized
    def play(self, kind: str, volume: float, deadline: float, cancelled) -> bool:
        # One retry can recover a lost device/callback. Every command carries a
        # start deadline, so a helper that resumes late cannot emit a stale ding.
        for _ in range(2):
            while time.monotonic() < deadline:
                if self.stopping.is_set() or cancelled():
                    self.close()
                    return False
                if self.maintain():
                    break
                time.sleep(.02)
            else:
                return False
            if self.stopping.is_set() or cancelled() or time.monotonic() >= deadline:
                return False
            process = self.process
            self.serial += 1
            token = self.serial
            command = json.dumps(dict(id=token, kind=kind, volume=volume, deadline=deadline)) + "\n"
            try:
                # Smaller than PIPE_BUF; a nonblocking pipe write is atomic.
                os.write(process.stdin.fileno(), command.encode())
            except OSError as exc:
                self._reset(str(exc))
                continue
            until = time.monotonic() + PLAY_TIMEOUT
            while time.monotonic() < until:
                if self.stopping.is_set() or cancelled():
                    self.close()
                    return False
                self.maintain()
                if self.process is not process:
                    break
                result = self.receipts.pop(token, None)
                if result == "played":
                    return True
                if result == "expired":
                    return False
                if result is not None:
                    self._reset("output underflow during cue", delay=0)
                    break
                time.sleep(.02)
            else:
                self._reset("cue did not reach its output deadline", delay=0)
        return False

    @serialized
    def play_file(self, path: str | Path, *, timeout: float, cancelled, on_start=None) -> None:
        """Confirm all frames reached their DAC deadline; never replay a reply.

        Recovery is safe before submitting the file. Once sent, a lost receipt
        is ambiguous: fail delivery rather than repeating words already heard.
        """
        until = time.monotonic() + timeout
        start_deadline = min(until, time.monotonic() + STARTUP_TIMEOUT + RETRY_DELAY)

        def check():
            if self.stopping.is_set() or cancelled():
                raise PlaybackCancelled("Speech cancelled")
            if time.monotonic() >= until:
                raise PlaybackError("Speech output timed out")

        try:
            while True:
                check()
                if self.maintain():
                    break
                if time.monotonic() >= start_deadline:
                    raise PlaybackError("Speech output did not become ready")
                time.sleep(.02)
            check()
            process = self.process
            self.serial += 1
            token = self.serial
            command = json.dumps(dict(id=token, kind="speech", path=str(path),
                                      deadline=start_deadline)) + "\n"
            data = command.encode()
            # Atomic nonblocking command; audio stays in the local file.
            if len(data) > os.fpathconf(process.stdin.fileno(), "PC_PIPE_BUF"):
                raise PlaybackError("Speech file path exceeds output command limit")
            if os.write(process.stdin.fileno(), data) != len(data):
                raise PlaybackError("Incomplete speech output command")
            notified = False
            while True:
                check()
                self.maintain()
                if self.process is not process:
                    raise PlaybackError("Speech output interrupted; not replayed")
                started = self.started.pop(token, None)
                if started is not None:
                    notified = True
                    if on_start:
                        on_start(started)
                result = self.receipts.pop(token, None)
                if result == "played" and notified:
                    return
                if result is not None:
                    raise PlaybackError(f"Speech output {result}; not replayed")
                if not notified and time.monotonic() >= start_deadline:
                    raise PlaybackError("Speech output did not start before its deadline")
                time.sleep(.01)
        except BaseException:
            # Also stops queued frames on mute, pause, shutdown or callback error.
            self.close()
            raise


_player = None
_player_lock = threading.Lock()


def shared_player() -> CuePlayer:
    global _player
    with _player_lock:
        if _player is None:
            _player = CuePlayer()
            atexit.register(_player.shutdown)
        return _player


def load_audio(path: str, samplerate: int, channels: int) -> np.ndarray:
    """Decode/resample before publishing a buffer to the real-time callback."""
    import soundfile as sf
    from scipy.signal import resample_poly

    samples, source_rate = sf.read(path, dtype="float32", always_2d=True)
    if not len(samples) or not np.isfinite(samples).all():
        raise ValueError("Speech file is empty or contains invalid samples")
    if samples.shape[1] != channels:
        samples = samples.mean(axis=1, keepdims=True)
        samples = np.repeat(samples, channels, axis=1)
    if source_rate != samplerate:
        factor = gcd(source_rate, samplerate)
        samples = resample_poly(samples, samplerate // factor, source_rate // factor, axis=0)
    return np.ascontiguousarray(samples, dtype=np.float32)


@dataclass
class Playback:
    token: int
    audio: np.ndarray
    deadline: float
    position: int = 0
    ends_at: float | None = None
    result: str | None = None
    report_start: bool = False
    started_at: float | None = None
    start_reported: bool = False


class CueBuffer:
    """Callback-owned playback cursor; the main thread only submits/takes cues."""

    def __init__(self, samplerate: float, channels: int = 2) -> None:
        self.samplerate = samplerate
        self.channels = channels
        self.tones = {kind: tone(kind, samplerate) for kind in ("accepted", "error", "working")}
        self.current: Playback | None = None
        self.last_callback = 0.0
        self.meter_sample = None

    def submit(self, command: dict) -> None:
        if self.current is not None:
            raise RuntimeError("cue already playing")
        speech = command["kind"] == "speech"
        if speech:
            samples = load_audio(command["path"], round(self.samplerate), self.channels)
        else:
            volume = min(1.0, max(0.0, float(command["volume"])))
            samples = (self.tones[command["kind"]] * volume)[:, None]
        self.current = Playback(command["id"], samples, command["deadline"], report_start=speech)

    def callback(self, outdata, frames, timing, status) -> None:
        # Never block, log, read files, or send pipe messages on the audio thread.
        outdata.fill(0)
        now = time.monotonic()
        self.last_callback = now
        cue = self.current
        self.meter_sample = None
        if cue is None or cue.result is not None:
            return
        if cue.position == 0 and now >= cue.deadline:
            cue.result = "expired"
            return
        if status.output_underflow and cue.position:
            cue.result = "failed"
            return
        if cue.ends_at is not None:
            if timing.currentTime >= cue.ends_at:
                cue.result = "played"
            elif cue.report_start:
                self.meter_sample = (now, 0.0)
            return
        count = min(frames, len(cue.audio) - cue.position)
        outdata[:count] = cue.audio[cue.position:cue.position + count]
        if cue.report_start and count:
            self.meter_sample = (now + timing.outputBufferDacTime - timing.currentTime,
                                 float(np.sqrt(np.mean(outdata[:count] ** 2))))
        if cue.position == 0:
            # PortAudio's DAC timestamp is an estimate, not an acoustic sensor.
            cue.started_at = now + timing.outputBufferDacTime - timing.currentTime
        cue.position += count
        if cue.position == len(cue.audio):
            # Wait until the final frame's DAC time, not just its submission.
            cue.ends_at = timing.outputBufferDacTime + count / self.samplerate

    def take_started(self) -> dict | None:
        cue = self.current
        if cue is None or not cue.report_start or cue.started_at is None or cue.start_reported:
            return None
        cue.start_reported = True
        return dict(event="started", id=cue.token, first_frame_mono_s=cue.started_at)

    def take_result(self) -> dict | None:
        cue = self.current
        if cue is None or cue.result is None:
            return None
        self.current = None
        return dict(event="result", id=cue.token, status=cue.result)


class DefaultOutput:
    """Read CoreAudio's live default ID; PortAudio's device list can be cached."""

    def __init__(self) -> None:
        self.get_data = None
        if sys.platform == "darwin":
            lib = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
            self.get_data = lib.AudioObjectGetPropertyData
            self.get_data.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
            self.get_data.restype = ctypes.c_int32

    def read(self) -> int | None:
        if self.get_data is None:
            return None
        address = (ctypes.c_uint32 * 3)(int.from_bytes(b"dOut", "big"), int.from_bytes(b"glob", "big"), 0)
        device, size = ctypes.c_uint32(), ctypes.c_uint32(4)
        code = self.get_data(1, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device))
        if code:
            raise OSError(f"default audio output lookup failed ({code})")
        return device.value


def _read_commands(commands: queue.Queue) -> None:
    try:
        for line in sys.stdin:
            commands.put(json.loads(line))
    finally:
        # Also handles a killed parent while CoreAudio is stuck in open/close.
        # OS process teardown releases its output stream; never linger orphaned.
        os._exit(0)


def serve() -> None:
    commands: queue.Queue = queue.Queue()
    threading.Thread(target=_read_commands, args=(commands,), daemon=True).start()
    # Load the speech decoder/resampler while warming, not on the first reply.
    import soundfile  # noqa: F401
    from scipy.signal import resample_poly  # noqa: F401
    monitor = DefaultOutput()
    device = monitor.read()
    import sounddevice as sd

    device_info = sd.query_devices(kind="output")
    sample_rate = device_info["default_samplerate"]
    channels = min(2, device_info["max_output_channels"])
    audio = CueBuffer(sample_rate, channels)
    meter = OutputMeter()
    stream = sd.OutputStream(samplerate=sample_rate, channels=channels, dtype="float32",
                             latency="low", callback=audio.callback)
    stream.start()
    ready = False
    last_health = 0.0
    while True:
        now = time.monotonic()
        if now - last_health >= .25:
            if monitor.read() != device:
                print(json.dumps(dict(event="reset", reason="default output changed")), flush=True)
                return
            # A live process with stalled callbacks must not look healthy.
            if audio.last_callback and now - audio.last_callback < .5:
                print(json.dumps(dict(event="alive" if ready else "ready")), flush=True)
                ready = True
            last_health = now
        started = audio.take_started()
        meter.publish(audio.meter_sample)
        if started:
            print(json.dumps(started), flush=True)
        result = audio.take_result()
        if result:
            print(json.dumps(result), flush=True)
        try:
            command = commands.get(timeout=.01)
        except queue.Empty:
            continue
        audio.submit(command)


def main() -> None:
    try:
        serve()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(dict(event="reset", reason=f"{type(exc).__name__}: {exc}")), flush=True)
    finally:
        # Avoid a second unbounded CoreAudio call during teardown.
        os._exit(0)


if __name__ == "__main__":
    main()
