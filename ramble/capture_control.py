"""Capture acknowledgement and control shared with the paired Companion."""
from __future__ import annotations

import fcntl
import json
import os
import threading
import time

from . import state


class CaptureError(RuntimeError):
    pass


def acknowledge(home, selected, active, failed=False):
    """Only the capture owner calls this, after applying the selected state."""
    record = {'selected': selected, 'active': bool(active), 'failed': failed,
              'checked_at': time.time(), 'pid': os.getpid()}
    path = home / 'capture-status.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(record))
    temp.replace(path)


class CaptureControl:
    def __init__(self, home):
        self.home = home
        self.lock = threading.Lock()

    def snapshot(self):
        try:
            # Strict reads: a corrupt/missing switch must never appear as muted.
            selected = json.loads((self.home / 'state.json').read_text())
            if not isinstance(selected, dict) or type(selected.get('enabled')) is not bool:
                raise ValueError()
            status = json.loads((self.home / 'capture-status.json').read_text())
            # A stopped app must not leave a seemingly live acknowledgement.
            with (self.home / 'daemon.lock').open('r') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    raise CaptureError('Ramble is not running. Open Ramble on the Mac, then refresh.')
            if not 0 <= time.time() - status['checked_at'] < 6:
                raise CaptureError('Ramble capture status is stale. Check the app on the Mac.')
            if status.get('failed'):
                raise CaptureError('Ramble could not change microphone capture. Check the app on the Mac.')
            listening = state.is_listening(selected)
            confirmed = status['selected'] == selected and status['active'] == listening
            return {'muted': not listening, 'confirmed': confirmed,
                    'paused_until': selected.get('paused_until') if state.is_paused(selected) else None}
        except (OSError, ValueError, KeyError, TypeError):
            raise CaptureError('Ramble capture status is unavailable. Check the app on the Mac.') from None

    def set_muted(self, muted):
        if type(muted) is not bool:
            raise ValueError('Supply muted as true or false.')
        with self.lock:
            self.snapshot()  # Never silently edit the switch of a stopped app.
            state.write(not muted, home=self.home)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                current = self.snapshot()
                if current['confirmed'] and current['muted'] == muted:
                    return current
                time.sleep(.1)
            raise CaptureError('Capture change was not confirmed. Refresh to check the current state before trying again.')
