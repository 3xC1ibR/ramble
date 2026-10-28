"""Daily dispatch policy regressions; isolated storage and mocked agent creation."""

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ramble import config, db, state
from ramble.router import Router


class DispatchCapTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-cap-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        paths = {"HOME": home, "DB_PATH": home / "ramble.db",
                 "STATE_PATH": home / "state.json", "CONFIG_PATH": home / "config.json"}
        paths.update({name: home / name.lower() for name in
                      ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        patched = patch.multiple(config, **paths)
        patched.start()
        self.addCleanup(patched.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = config.load()
        self.router = Router(self.cfg, threading.Event())
        now = time.time()
        self.thought = {"id": "dashboard", "started_at": now, "ended_at": now,
                        "text": "Find my dashboard in Playground and get an ngrok link."}
        self.item = {"dispatch": {"name": "Find personal dashboard", "cwd": "/tmp",
                                  "prompt": self.thought["text"]}}
        registration = patch("ramble.router.ensure_coord_session", return_value="test-caller")
        self.registration = registration.start()
        self.addCleanup(registration.stop)
        child = patch("ramble.router.subprocess.run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"thread_id": "test-thread", "result": {"url": "test-url"}}), ""))
        self.child = child.start()
        self.addCleanup(child.stop)

    def seed_dispatches(self, count, status="created"):
        self.conn.executemany(
            "INSERT INTO dispatches(id,thought_id,created_at,name,cwd,prompt,status) "
            "VALUES (?, 'older', ?, 'Earlier task', '/tmp', 'Test', ?)",
            [(f"{status}-{i}", time.time(), status) for i in range(count)],
        )

    def test_default_dispatches_past_old_daily_limit(self):
        self.seed_dispatches(30)
        self.assertIn("unlimited (30 dispatched)", self.router.build_input(self.conn, self.thought))
        rec = self.router.dispatch(self.conn, self.thought, self.item)
        self.assertEqual(rec["status"], "created")
        self.assertEqual(rec["thread_id"], "test-thread")
        self.assertEqual(self.router.dispatches_today(self.conn), 31)
        self.registration.assert_called_once()
        self.child.assert_called_once()

    def test_null_override_is_unlimited(self):
        config.CONFIG_PATH.write_text('{"dispatch_daily_cap": null}')
        self.router.cfg = config.load()
        self.seed_dispatches(100)
        self.assertIn("unlimited (100 dispatched)", self.router.build_input(self.conn, self.thought))
        self.assertEqual(self.router.dispatch(self.conn, self.thought, self.item)["status"], "created")

    def test_positive_override_allows_until_limit_then_stops(self):
        config.CONFIG_PATH.write_text('{"dispatch_daily_cap": 2}')
        self.router.cfg = config.load()
        self.seed_dispatches(1)
        self.assertIn("1 of 2 remaining", self.router.build_input(self.conn, self.thought))
        self.assertEqual(self.router.dispatch(self.conn, self.thought, self.item)["status"], "created")
        self.assertIn("0 of 2 remaining", self.router.build_input(self.conn, self.thought))
        self.assertEqual(self.router.dispatch(self.conn, self.thought, self.item)["status"], "skipped_cap")
        self.registration.assert_called_once()
        self.child.assert_called_once()

    def test_zero_override_disables_registration_and_dispatch(self):
        config.CONFIG_PATH.write_text('{"dispatch_daily_cap": 0}')
        self.router.cfg = config.load()
        self.assertIn("0 of 0 remaining", self.router.build_input(self.conn, self.thought))
        self.assertEqual(self.router.dispatch(self.conn, self.thought, self.item)["status"], "skipped_cap")
        self.registration.assert_not_called()
        self.child.assert_not_called()

    def test_failed_or_skipped_attempts_do_not_consume_explicit_cap(self):
        self.cfg["dispatch_daily_cap"] = 1
        for status in ("error", "skipped_cap", "skipped_paused"):
            self.seed_dispatches(2, status)
        self.assertIn("1 of 1 remaining", self.router.build_input(self.conn, self.thought))
        self.assertEqual(self.router.dispatch(self.conn, self.thought, self.item)["status"], "created")

    def test_pause_still_blocks_unlimited_dispatch(self):
        state.write(True, time.time() + 3600)
        self.assertEqual(self.router.dispatch(self.conn, self.thought, self.item)["status"], "skipped_paused")
        self.registration.assert_not_called()
        self.child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
