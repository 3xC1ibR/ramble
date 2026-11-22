"""Which processes on this Mac are playing audio, read from Core Audio without capturing it.

A laptop microphone cannot tell a YouTube voice from the owner's, but macOS can
say whether some other process is currently sending audio to an output device
(``kAudioProcessPropertyIsRunningOutput``, macOS 14.4+). The monitor samples
that once a second into ``output_activity`` so each speech segment can be
checked against it later. It reads properties only: no audio is captured,
played or tapped, and no permission prompt is involved.

It cannot see audio from another device (a TV, an iPad, a phone) or a person
in the room; those still need the owner-voice verifier.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import struct
import threading
import time

log = logging.getLogger("ramble.output")

SCHEMA = """CREATE TABLE IF NOT EXISTS output_activity (
    sampled_at REAL PRIMARY KEY,
    active INTEGER NOT NULL,          -- 1 = another process was sending audio to an output
    pids TEXT NOT NULL DEFAULT ''      -- comma-separated output PIDs, excluding Ramble's own
);
"""


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


_SYSTEM_OBJECT = 1
_SCOPE_GLOBAL = _fourcc("glob")
_ELEMENT_MAIN = 0
_PROCESS_LIST = _fourcc("prs#")  # kAudioHardwarePropertyProcessObjectList
_PROCESS_PID = _fourcc("ppid")  # kAudioProcessPropertyPID
_PROCESS_RUNNING_OUTPUT = _fourcc("piro")  # kAudioProcessPropertyIsRunningOutput


class CoreAudioReader:
    """Minimal ctypes binding for the three read-only properties used here."""

    def __init__(self):
        path = ctypes.util.find_library("CoreAudio")
        if not path:
            raise OSError("CoreAudio is unavailable")
        self.lib = ctypes.CDLL(path)

    def _size(self, obj: int, selector: int) -> int:
        addr = _Address(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)
        size = ctypes.c_uint32(0)
        status = self.lib.AudioObjectGetPropertyDataSize(ctypes.c_uint32(obj), ctypes.byref(addr), 0, None,
                                                         ctypes.byref(size))
        if status:
            raise OSError(f"AudioObjectGetPropertyDataSize failed: {status}")
        return size.value

    def _read(self, obj: int, selector: int, ctype, count: int = 1) -> list:
        addr = _Address(selector, _SCOPE_GLOBAL, _ELEMENT_MAIN)
        size = ctypes.c_uint32(ctypes.sizeof(ctype) * count)
        buf = (ctype * count)()
        status = self.lib.AudioObjectGetPropertyData(ctypes.c_uint32(obj), ctypes.byref(addr), 0, None,
                                                     ctypes.byref(size), buf)
        if status:
            raise OSError(f"AudioObjectGetPropertyData failed: {status}")
        return list(buf)[: size.value // ctypes.sizeof(ctype)]

    def output_pids(self) -> set[int]:
        count = self._size(_SYSTEM_OBJECT, _PROCESS_LIST) // ctypes.sizeof(ctypes.c_uint32)
        pids = set()
        for obj in self._read(_SYSTEM_OBJECT, _PROCESS_LIST, ctypes.c_uint32, count) if count else []:
            try:
                if self._read(obj, _PROCESS_RUNNING_OUTPUT, ctypes.c_uint32)[0]:
                    pids.add(int(self._read(obj, _PROCESS_PID, ctypes.c_int32)[0]))
            except OSError:
                continue  # the process exited between the list and the read
        return pids


# System alert and notification sounds are short and are not media.
IGNORED_COMMANDS = {"systemsoundserverd", "NotificationCenter", "UserNotificationCenter"}


def ramble_pids() -> set[int]:
    """Ramble and all its descendants (cue player, speech helpers) are not external media."""
    own, frontier = {os.getpid()}, [os.getpid()]
    try:
        import subprocess
        while frontier:
            out = subprocess.run(["pgrep", "-P", str(frontier.pop())], capture_output=True, text=True, timeout=2)
            children = {int(p) for p in out.stdout.split()} - own
            own.update(children)
            frontier.extend(children)
    except Exception:  # noqa: BLE001
        pass
    return own


def command_name(pid: int) -> str:
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        return os.path.basename(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return ""


class OutputActivityMonitor:
    """Samples external output activity into SQLite; failures disable sampling, never capture."""

    def __init__(self, connect, reader=None, interval_s: float = 1.0, exclude=ramble_pids,
                 clock=time.time, retention_s: float = 14 * 86400, name_of=command_name):
        self.connect = connect
        self.reader = reader
        self.interval_s = interval_s
        self.exclude = exclude
        self.clock = clock
        self.retention_s = retention_s
        self.name_of = name_of

    def sample(self, conn) -> tuple[bool, set[int]]:
        pids = self.reader.output_pids()
        if pids:
            pids -= set(self.exclude())
            pids = {p for p in pids if self.name_of(p) not in IGNORED_COMMANDS}
        now = self.clock()
        conn.execute("INSERT OR REPLACE INTO output_activity(sampled_at, active, pids) VALUES (?,?,?)",
                     (now, int(bool(pids)), ",".join(str(p) for p in sorted(pids))))
        return bool(pids), pids

    def run_forever(self, stop: threading.Event) -> None:
        try:
            self.reader = self.reader or CoreAudioReader()
        except OSError:
            log.warning("output activity unavailable; segments will be judged by voice only")
            return
        conn = self.connect()
        conn.execute(SCHEMA)
        last_prune = 0.0
        try:
            while not stop.is_set():
                try:
                    self.sample(conn)
                    if self.clock() - last_prune > 3600:
                        conn.execute("DELETE FROM output_activity WHERE sampled_at < ?",
                                     (self.clock() - self.retention_s,))
                        last_prune = self.clock()
                    conn.commit()
                except Exception:  # noqa: BLE001 - a sampling error must not affect capture
                    log.exception("output activity sample failed")
                stop.wait(self.interval_s)
        finally:
            conn.close()


def active_fraction(conn, start: float, end: float, max_gap_s: float = 3.0) -> float | None:
    """Fraction of samples in [start, end] with external output; None if unmeasured (fail open)."""
    try:
        rows = conn.execute("SELECT sampled_at, active FROM output_activity WHERE sampled_at BETWEEN ? AND ? "
                            "ORDER BY sampled_at", (start - 1.0, end + 1.0)).fetchall()
    except Exception:  # noqa: BLE001 - table absent: the monitor never ran
        return None
    if not rows:
        return None
    times = [start - 1.0] + [r[0] for r in rows] + [end + 1.0]
    if max(b - a for a, b in zip(times, times[1:])) > max_gap_s + 2.0:
        return None  # coverage gap: do not infer silence from missing samples
    return sum(int(r[1]) for r in rows) / len(rows)
