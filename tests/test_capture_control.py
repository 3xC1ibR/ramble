"""Exercise the real daemon sync using a fake recorder; never open a microphone."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from ramble import config, state
from ramble.daemon import Daemon


class FakeRecorder:
    active = False
    error = None
    fail = False

    def start(self):
        if self.fail:
            self.error = 'No microphone permission'
        else:
            self.active = True

    def stop(self):
        self.active = False


class CaptureSyncTests(unittest.TestCase):
    def test_actual_sync_acknowledges_on_off_pause_and_failure(self):
        with tempfile.TemporaryDirectory(prefix='ramble-test-control-', dir='/tmp') as tmp:
            home = Path(tmp)
            (home / 'config.json').write_text('{"dispatch_daily_cap":0,"fast_speak":false,"speak_questions":false}')
            with patch.multiple(config, HOME=home, STATE_PATH=home / 'state.json'):
                daemon = Daemon.__new__(Daemon)
                daemon.recorder = FakeRecorder()
                daemon.wake = threading.Event()
                def check(enabled, paused_until=None):
                    selected = state.write(enabled, paused_until, home=home)
                    daemon.sync()
                    saved = json.loads((home / 'capture-status.json').read_text())
                    self.assertEqual(saved['selected'], selected)
                    return saved
                self.assertTrue(check(True)['active'])
                self.assertFalse(check(False)['active'])
                self.assertFalse(check(True, time.time() + 3600)['active'])
                self.assertTrue(check(True)['active'])
                check(False)
                daemon.recorder.fail = True
                failed = check(True)
                self.assertTrue(failed['failed']); self.assertFalse(failed['active'])
                self.assertFalse(check(False)['failed'])


if __name__ == '__main__':
    unittest.main()
