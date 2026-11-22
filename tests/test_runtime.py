"""Duplicate-launch replay: no microphone, generated audio, or real agents."""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import config, db, state
from ramble.capture_control import CaptureControl, CaptureError
from ramble.daemon import Daemon
from ramble.runtime import AlreadyRunning, acquire_daemon_lock


class RuntimeTests(unittest.TestCase):
    def test_blocked_microphone_driver_exits_and_releases_capture_owner(self):
        for listening in (True, False):
            with self.subTest(listening=listening), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                selected = state.write(listening, home=home)
                code = textwrap.dedent("""\
                    from pathlib import Path
                    import sys, threading
                    from unittest.mock import Mock
                    from ramble import config
                    import ramble.daemon as runtime
                    from ramble.runtime import acquire_daemon_lock

                    config.HOME = Path(sys.argv[1])
                    # Initial sync succeeds. The next start/stop blocks like
                    # a native driver call, with the steady-state deadline.
                    runtime.MIC_CONTROL_TIMEOUT_S = .05
                    runtime.INITIAL_MIC_CONTROL_TIMEOUT_S = 10
                    daemon = runtime.Daemon()
                    owner = acquire_daemon_lock(config.HOME)
                    daemon.recorder = Mock(active=True, error=None)
                    blocked = threading.Event()
                    calls = 0
                    def driver_call():
                        global calls
                        calls += 1
                        if calls > 1:
                            blocked.wait()
                    operation = (daemon.recorder.start if sys.argv[2] == 'True'
                                 else daemon.recorder.stop)
                    operation.side_effect = driver_call
                    threading.Thread(target=daemon._mic_control, daemon=True).start()
                    daemon._mic_watchdog()
                """)
                result = subprocess.run([sys.executable, "-c", code, tmp, str(listening)],
                                        capture_output=True, text=True, timeout=15,
                                        env=dict(os.environ, RAMBLE_HOME=tmp))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("Microphone control stopped responding", result.stderr)
                import json
                status = json.loads((home / "capture-status.json").read_text())
                self.assertTrue(status["failed"])
                self.assertFalse(status["active"])
                self.assertEqual(state.read(home=home), selected)
                with self.assertRaises(CaptureError):
                    CaptureControl(home).snapshot()
                # A fresh app can acquire ownership after forced recovery.
                recovered = acquire_daemon_lock(home)
                recovered.close()

    def test_mic_watchdog_allows_initial_prompt_and_healthy_control(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "HOME", Path(tmp)):
            daemon = Daemon()
            entered, release = threading.Event(), threading.Event()
            def initial_sync():
                entered.set()
                release.wait(2)
            with patch.object(daemon, "sync", side_effect=initial_sync), \
                    patch("ramble.daemon.os._exit") as exit_process:
                control = threading.Thread(target=daemon._mic_control)
                control.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self.assertGreater(daemon._mic_control_deadline - time.monotonic(), 290)
                    # A watchdog tick during the permission grace must not exit.
                    with patch.object(daemon.stop, "wait", side_effect=[False, True]):
                        daemon._mic_watchdog()
                    exit_process.assert_not_called()
                    daemon.stop.set()
                finally:
                    release.set()
                    control.join(3)
                self.assertFalse(control.is_alive())
                self.assertIsNone(daemon._mic_control_deadline)

    def test_duplicate_launch_has_one_activation_and_response_but_repeats_work(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "HOME", Path(tmp)):
            first, duplicate = Daemon(), Daemon()
            accepted = []
            # Exercise the real Daemon.start gate, replacing hardware/model
            # threads only. Both launch attempts receive the same captured input.
            with patch("ramble.daemon.threading.Thread") as thread, patch.object(db, "connect") as connect:
                for candidate in (first, duplicate):
                    try:
                        candidate.start()
                    except AlreadyRunning:
                        continue
                    accepted.append(candidate)
                first.start()  # Reentrant calls cannot create more workers.
                workers = [call.kwargs['name'] for call in thread.call_args_list]
                self.assertEqual(len(workers), len(set(workers)))
                self.assertEqual(set(workers), {'transcriber', 'router', 'mic-control', 'mic-watchdog', 'fast-lane',
                                               'audio-cues', 'work-sessions', 'speech-output',
                                               'article-reading', 'reading-questions', 'model-discovery'})
                self.assertEqual(thread.return_value.start.call_count, len(workers))
                connect.assert_called_once_with()
            self.assertEqual(accepted, [first])

            # Exercise the legacy fast-reply replay independently of the durable
            # session flow covered by test_sessions.
            first.fast.sessions = None
            conn = sqlite3.connect(":memory:")
            self.addCleanup(conn.close)
            conn.row_factory = sqlite3.Row
            conn.executescript(db.SCHEMA)
            first.fast.cfg.update(fast_speak=True, dispatch_daily_cap=0)
            activation = Mock(wraps=first.fast.offer)
            first.transcriber.on_utterance = activation

            def replay(worker, seg_id, text, started_at):
                path = Path(tmp) / (seg_id + ".flac")
                conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES (?,?,?,2,?)",
                             (seg_id, started_at, started_at + 2, str(path)))
                row = conn.execute("SELECT * FROM segments WHERE id=?", (seg_id,)).fetchone()
                decoded = {"text": text, "segments": [{"no_speech_prob": 0, "avg_logprob": 0}]}
                with patch.object(worker.transcriber, "transcribe_path", return_value=decoded):
                    worker.transcriber.process(conn, row)
                worker.fast.respond(conn, [worker.fast.q.get_nowait()])

            text = "Looks like Ramble was woken up twice by the same request."
            with patch.object(first.fast, "run_codex", return_value={"reply": "On it.", "handoff": True}) as model, \
                    patch("ramble.speech.speak") as speak, patch.object(first.fast.cues, "emit"):
                for worker in accepted:
                    replay(worker, "original", text, 100)
                self.assertEqual(activation.call_count, 1)
                self.assertEqual(model.call_count, 1)
                self.assertEqual(speak.call_count, 1)
                self.assertEqual(conn.execute("SELECT text FROM thoughts").fetchone()[0].count("[handoff:"), 1)
                # The same words spoken later are legitimate, as is a different
                # request immediately afterward. There is no text/time heuristic.
                replay(first, "legitimate-repeat", text, 110)
                replay(first, "distinct", "Ramble, tell me the time.", 120)
                self.assertEqual(activation.call_count, 3)
                self.assertEqual(model.call_count, 3)
                self.assertEqual(speak.call_count, 3)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM segments WHERE status='transcribed'").fetchone()[0], 3)

    def test_process_contention_independent_homes_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "shared"
            code = (
                "from pathlib import Path; import sys; "
                "from ramble.runtime import acquire_daemon_lock; "
                "lock = acquire_daemon_lock(Path(sys.argv[1])); "
                "print('locked', flush=True); sys.stdin.readline()"
            )
            child = subprocess.Popen([sys.executable, "-u", "-c", code, str(home)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "locked")
                with self.assertRaises(AlreadyRunning):
                    acquire_daemon_lock(home)
                independent = acquire_daemon_lock(Path(tmp) / "independent")
                self.assertFalse(independent.closed)
                # No cleanup handler runs on SIGKILL; flock must still recover.
                child.kill()
                child.communicate(timeout=5)
                inode = (home / "daemon.lock").stat().st_ino
                recovered = acquire_daemon_lock(home)
                self.assertEqual((home / "daemon.lock").stat().st_ino, inode)
                self.assertEqual((home / "daemon.lock").read_text().strip(), str(os.getpid()))
                self.assertFalse(os.get_inheritable(recovered.fileno()))
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

    def test_shutdown_keeps_lock_until_process_exit(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(config, "HOME", Path(tmp)):
            daemon = Daemon()
            with patch("ramble.daemon.threading.Thread"), patch.object(db, "connect"):
                daemon.start()
            with patch.object(daemon.recorder, "stop"):
                daemon.shutdown()
            with self.assertRaises(AlreadyRunning):
                Daemon().start()


if __name__ == "__main__":
    unittest.main()
