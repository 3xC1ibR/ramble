"""One capture/transcription/router pipeline per runtime home."""

from __future__ import annotations

import atexit
import fcntl
import os
from pathlib import Path
from typing import TextIO


class AlreadyRunning(RuntimeError):
    """Another daemon owns this home's pipeline."""


def acquire_daemon_lock(home: Path) -> TextIO:
    """Hold an OS lock until process exit, including while workers shut down.

    Never unlink the file: replacing its inode would let a second process lock
    a different file. The PID is diagnostic; only flock determines ownership.
    Separate RAMBLE_HOME directories can run independently.
    """
    home.mkdir(parents=True, exist_ok=True)
    handle = (home / "daemon.lock").open("a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise AlreadyRunning(f"Ramble already running for {home} (pid {owner})") from None
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()) + "\n")
        handle.flush()
    except BaseException:
        handle.close()
        raise
    # Keep a strong reference even if the caller drops its Daemon while a
    # model/playback worker is still exiting. SIGKILL also releases flock.
    atexit.register(handle.close)
    return handle
