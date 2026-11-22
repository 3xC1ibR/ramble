"""Background status and exact-task Stop with fake owners; no real agents or audio."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ramble import config, db
from ramble.activity import ActivityMonitor
from ramble.dispatch_activity import DispatchActivityMonitor


class DispatchBackend:
    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.live = {}
        self.queued = False
        self.fail = False
        self.lose_response = False

    def command(self, request_id, thread, action, **kwargs):
        if request_id not in self.receipts:
            self.calls.append((request_id, thread, action, kwargs))
            status = "failed" if self.fail and action == "interrupt" else "completed"
            if action == "interrupt" and kwargs.get("expected") != self.live[thread].get("active_turn_id"):
                status = "failed"
            if action == "interrupt" and self.queued:
                status = "queued"
            self.receipts[request_id] = {"status": status}
            if action == "interrupt" and status == "completed":
                self.live[thread].update(active_turn_id=None, status="interrupted")
        if action == "interrupt" and self.lose_response:
            self.lose_response = False
            raise OSError("response lost")
        return self.receipts[request_id]

    def snapshot(self, thread, after):
        return self.live[thread]

    def status(self, request_id):
        if request_id not in self.receipts:
            raise RuntimeError("App request not found")
        return self.receipts[request_id]


class DispatchActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ramble-test-dispatch-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.enterContext(patch.multiple(config, HOME=self.home, DB_PATH=self.home / "ramble.db",
                                        STATE_PATH=self.home / "state.json"))
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.backend = DispatchBackend()
        self.monitor = DispatchActivityMonitor({}, backend=self.backend)

    def dispatch(self, identity="d1", *, age=0, status="created"):
        thread = "thread-" + identity
        self.conn.execute("INSERT INTO dispatches(id,thought_id,created_at,name,cwd,prompt,thread_id,status) "
                          "VALUES (?, 'thought', ?, ?, '/tmp', 'Research', ?, ?)",
                          (identity, time.time() - age, "Research " + identity, thread, status))
        self.backend.live[thread] = dict(connected=True, status="running", active_turn_id="turn-" + identity,
                                         last_turn_id="turn-" + identity)
        return thread

    def test_only_new_successful_dispatches_show_and_watching_never_submits(self):
        self.dispatch("old", age=60)
        self.dispatch("failed", status="error")
        self.monitor.update()
        self.assertIsNone(self.monitor.current)
        self.dispatch()
        self.monitor.update()
        self.assertEqual(self.monitor.current.name, "Research d1")
        self.assertEqual(self.monitor.current.target.turn_id, "turn-d1")
        self.assertEqual([c[2] for c in self.backend.calls], ["watch"])
        self.assertFalse(config.STATE_PATH.exists())

    def test_click_stops_displayed_task_when_new_dispatch_arrives(self):
        original = self.dispatch()
        self.monitor.update()
        target = self.monitor.current.target
        self.monitor.request_stop(target)
        newer = self.dispatch("d2")
        self.monitor.update()
        calls = [c for c in self.backend.calls if c[2] == "interrupt"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], original)
        self.assertEqual(calls[0][3]["expected"], "turn-d1")
        self.assertEqual(self.backend.live[newer]["active_turn_id"], "turn-d2")
        self.assertEqual(self.monitor.current.name, "Research d2")
        self.assertEqual(self.monitor.current.mode, "background")

    def test_queued_stop_is_not_reported_stopped_until_owner_confirms(self):
        thread = self.dispatch()
        self.monitor.update()
        self.backend.queued = True
        target = self.monitor.current.target
        self.monitor.request_stop(target)
        self.monitor.request_stop(target)
        self.monitor.update()
        self.assertEqual(self.monitor.current.mode, "stopping")
        self.assertIsNone(self.monitor.current.target)
        self.assertEqual(len([c for c in self.backend.calls if c[2] == "interrupt"]), 1)
        request = next(iter(self.monitor.pending))
        self.backend.receipts[request] = {"status": "completed"}
        self.monitor.update()
        self.assertEqual(self.monitor.current.mode, "stopping")
        self.backend.live[thread]["active_turn_id"] = None
        self.monitor.update()
        self.assertEqual(self.monitor.current.mode, "stopped")
        self.assertTrue(self.monitor.finished)

    def test_old_displayed_turn_cannot_stop_a_newer_turn_in_the_same_task(self):
        thread = self.dispatch()
        self.monitor.update()
        target = self.monitor.current.target
        self.backend.live[thread]["active_turn_id"] = "newer-turn"
        self.monitor.request_stop(target)
        self.monitor.update()
        self.assertEqual(self.backend.live[thread]["active_turn_id"], "newer-turn")
        self.assertEqual(self.monitor.current.mode, "stop_error")
        self.assertEqual(self.monitor.current.target.turn_id, "newer-turn")

    def test_lost_stop_response_reconciles_without_another_interrupt(self):
        self.dispatch()
        self.monitor.update()
        self.backend.lose_response = True
        self.monitor.request_stop(self.monitor.current.target)
        with self.assertLogs("ramble.dispatch_activity", level="WARNING"):
            self.monitor.update()
        self.monitor.update()
        self.assertEqual(self.monitor.current.mode, "stopped")
        self.assertEqual(len([c for c in self.backend.calls if c[2] == "interrupt"]), 1)

    def test_unsubmitted_stop_retries_with_the_same_request_and_target(self):
        thread = self.dispatch()
        self.monitor.update()
        target = self.monitor.current.target
        self.monitor.pending["lost-request"] = target
        self.monitor.update()
        request, actual_thread, action, kwargs = self.backend.calls[-1]
        self.assertEqual((request, actual_thread, action), ("lost-request", thread, "interrupt"))
        self.assertEqual(kwargs["expected"], target.turn_id)
        self.assertEqual(self.monitor.current.mode, "stopped")

    def test_failed_stop_remains_retryable_and_disconnect_removes_target(self):
        thread = self.dispatch()
        self.monitor.update()
        self.backend.fail = True
        self.monitor.request_stop(self.monitor.current.target)
        self.monitor.update()
        self.assertEqual(self.monitor.current.mode, "stop_error")
        self.assertIsNotNone(self.monitor.current.target)
        self.backend.fail = False
        self.monitor.request_stop(self.monitor.current.target)
        self.monitor.update()
        self.assertEqual(self.monitor.current.mode, "stopped")
        self.dispatch("d2")
        self.backend.live["thread-d2"]["connected"] = False
        self.monitor.update()
        self.assertIsNone(self.monitor.current.target)

    def test_expiry_and_hover_use_cached_state_without_owner_or_database_calls(self):
        self.dispatch()
        self.monitor.update()
        activity = ActivityMonitor(SimpleNamespace(cfg={}))
        activity.dispatches = self.monitor
        with patch("ramble.activity.time.monotonic", return_value=self.monitor.current.expires_at + 1), \
                patch.object(self.backend, "snapshot", side_effect=AssertionError("UI performed I/O")):
            self.assertIsNone(activity.background_activity())
            recalled = activity.background_activity(hover=True)
            self.assertEqual(recalled.label, "Research d1")
            self.assertEqual(recalled.target.turn_id, "turn-d1")

    def test_completion_clears_stop_and_cannot_be_recalled_as_running(self):
        thread = self.dispatch()
        self.monitor.update()
        self.backend.live[thread].update(status="idle", active_turn_id=None)
        self.monitor.update()
        self.assertEqual(self.monitor.current.detail, "Finished Research d1")
        self.assertIsNone(self.monitor.current.target)
        activity = ActivityMonitor(SimpleNamespace(cfg={}))
        activity.dispatches = self.monitor
        with patch("ramble.activity.time.monotonic", return_value=self.monitor.current.expires_at + 1):
            self.assertIsNone(activity.background_activity(hover=True))


if __name__ == "__main__":
    unittest.main()
