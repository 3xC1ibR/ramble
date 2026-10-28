"""Speech cancellation uses a fake player; no sound or microphone."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
import json
import subprocess
from unittest.mock import Mock, patch
from ramble import config, state
from ramble.fastlane import FastLane
from ramble.sessions import Sessions
from ramble.session_store import SessionStore
from ramble.speech_queue import SpeechQueue
from ramble.cues import AudioCues


class QueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="ramble-test-speech-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.enterContext(patch.object(config, "STATE_PATH", self.home / "state.json"))
        self.store = SessionStore(self.home)
        with self.store.connect() as conn:
            conn.execute(
                "INSERT INTO work_sessions(id,name,goal,cwd,kind,status,created_at,updated_at) VALUES ('s','task','task',?,'interactive','running',?,?)",
                (str(self.home), time.time(), time.time()),
            )
        self.queue = SpeechQueue(dict(config.DEFAULTS, fast_speak=True), self.store)

    def test_stop_talking_cancels_owned_player_without_stopping_worker(self):
        started = threading.Event()
        finished = threading.Event()

        def player(text, cfg, *, cancelled, origin):
            started.set()
            while not cancelled():
                if finished.wait(0.01):
                    raise AssertionError("Cancellation never reached player")
            raise state.PausedError("Cancelled")

        self.queue.enqueue("Progress", session_id="s", kind="progress")
        with patch("ramble.speech.speak", side_effect=player):
            worker = threading.Thread(target=self.queue.tick, args=(threading.Event(),))
            worker.start()
            try:
                self.assertTrue(started.wait(2))
                self.store.control("s", "stop_talking", "stop")
                worker.join(2)
                self.assertFalse(worker.is_alive())
            finally:
                finished.set()
                worker.join(2)
        self.assertEqual(self.store.get("s")["status"], "running")
        with self.store.connect() as c:
            self.assertEqual(
                c.execute("SELECT status FROM session_speech").fetchone()[0],
                "cancelled",
            )

    def test_replies_precede_progress_and_stale_progress_is_coalesced(self):
        self.queue.enqueue("Old", session_id="s", kind="progress", job_id="old")
        self.queue.enqueue("New", session_id="s", kind="progress", job_id="new")
        self.queue.enqueue("Answer", job_id="answer")
        with patch("ramble.speech.speak") as speak:
            self.queue.tick(threading.Event())
            self.assertEqual(speak.call_args.args[0], "Answer")
        with self.store.connect() as c:
            self.assertIsNotNone(c.execute("SELECT finished_at FROM session_speech WHERE id='answer'").fetchone()[0])
            self.assertEqual(
                c.execute(
                    "SELECT status FROM session_speech WHERE id='old'"
                ).fetchone()[0],
                "superseded",
            )

    def test_restart_does_not_replay_possible_spoken_output(self):
        self.queue.enqueue("Possibly heard", job_id="old")
        with self.store.connect() as c:
            c.execute("UPDATE session_speech SET status='playing'")
        stop = threading.Event()
        stop.set()
        self.queue.run_forever(stop)
        with self.store.connect() as c:
            self.assertEqual(
                c.execute("SELECT status FROM session_speech").fetchone()[0],
                "uncertain",
            )

    def test_working_feedback_survives_queue_and_synthesis_until_playback(self):
        self.queue.cues = cues = AudioCues(self.queue.cfg)
        token = cues.start_work(time.time())
        cues.wait_for_speech(token, 'answer')
        self.queue.enqueue('Answer', job_id='answer')
        cues.end_work(token, keep_pending=True)
        self.assertTrue(cues.work_active())
        def speak(*args, **kwargs):
            self.assertTrue(cues.work_active())
            kwargs['on_playback_start']()
            self.assertFalse(cues.work_active())
        with patch('ramble.speech.speak', side_effect=speak) as speaker:
            self.queue.tick(threading.Event())
        speaker.assert_called_once()
        self.assertFalse(cues.work_active())

    def test_cancelled_expired_suppressed_and_failed_jobs_end_feedback(self):
        for status in ('cancelled', 'expired', 'suppressed', 'failed'):
            with self.subTest(status=status):
                self.queue.cues = cues = AudioCues(self.queue.cfg)
                token = cues.start_work(time.time())
                cues.wait_for_speech(token, status)
                self.queue.enqueue('Answer', job_id=status)
                if status in ('cancelled', 'expired'):
                    with self.store.connect() as conn:
                        conn.execute('UPDATE session_speech SET status=? WHERE id=?', (status, status))
                    with patch('ramble.speech.speak') as speak:
                        self.queue.tick(threading.Event())
                    speak.assert_not_called()
                elif status == 'suppressed':
                    self.queue.cfg['fast_speak'] = False
                    with patch('ramble.speech.speak') as speak:
                        self.queue.tick(threading.Event())
                    speak.assert_not_called()
                    self.queue.cfg['fast_speak'] = True
                else:
                    with patch('ramble.speech.speak', side_effect=RuntimeError('failed')), \
                            self.assertLogs('ramble.speech_queue', level='ERROR'):
                        self.queue.tick(threading.Event())
                self.assertIsNone(cues.pending_speech())

    def test_an_old_speech_receipt_cannot_stop_new_request_feedback(self):
        self.queue.cues = cues = AudioCues(self.queue.cfg)
        old = cues.start_work(100)
        cues.wait_for_speech(old, 'old')
        new = cues.start_work(101)
        cues.wait_for_speech(new, 'new')
        cues.finish_speech_wait('old')
        self.assertEqual(cues.pending_speech(), 'new')

    def test_coalesced_continuation_hands_its_working_cue_to_final_queued_reply(self):
        fast = FastLane(dict(config.DEFAULTS, fast_speak=True, dispatch_daily_cap=0,
                             fast_coalesce_s=0))
        fast.sessions = Sessions(fast.cfg, backend=Mock(), store=self.store)
        fast.sessions.speech = self.queue
        self.queue.cues = fast.cues
        now = time.time()
        first = {'seg': {'id': 'first', 'started_at': now-10, 'ended_at': now-8},
                 'text': 'Ramble, show me'}
        second = {'seg': {'id': 'second', 'started_at': now-7, 'ended_at': now-5},
                  'text': 'the latest results'}
        with self.store.connect() as conn:
            for item in (first, second):
                seg = item['seg']
                conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text) VALUES (?,?,?,2,'unused','transcribed',?)",
                             (seg['id'], seg['started_at'], seg['ended_at'], item['text']))
            fast.offer(conn, first['seg'], first['text'])
            initial = fast.q.get_nowait()
            calls = []
            def answer(*args, **kwargs):
                calls.append(args)
                if len(calls) == 1:
                    fast.offer(conn, second['seg'], second['text'])
                    return {'reply': 'Show what?', 'handoff': False}
                return {'reply': 'Here are the results.', 'handoff': False}
            with patch.object(fast, 'run_codex', side_effect=answer):
                fast.respond(conn, [initial])
        self.assertEqual(len(calls), 2)
        self.assertTrue(fast.cues.work_active())
        self.assertIsNotNone(fast.cues.pending_speech())
        def speak(text, *args, **kwargs):
            self.assertEqual(text, 'Here are the results.')
            self.assertTrue(fast.cues.work_active())
            kwargs['on_playback_start']()
            self.assertFalse(fast.cues.work_active())
        with patch('ramble.speech.speak', side_effect=speak):
            self.queue.tick(threading.Event())
        self.assertFalse(fast.cues.work_active())

    def respond_slowly(self, fail=False):
        fast = FastLane(dict(config.DEFAULTS, fast_speak=False, dispatch_daily_cap=0))
        fast.sessions = Sessions(fast.cfg, backend=Mock(), store=self.store)
        fast.sessions.speech = self.queue
        now = time.time()
        item = {'seg': {'id':'request', 'started_at':now-2, 'ended_at':now}, 'text':'Ramble, hello'}
        with self.store.connect() as conn:
            conn.execute("INSERT INTO thoughts(id,started_at,ended_at,text,status) VALUES ('thought',?,?,?,'closed')", (now-2,now,item['text']))
            conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) VALUES ('request',?,?,2,'unused','transcribed',?,'thought')", (now-2,now,item['text']))
            fast.sessions.accept_input(conn, item)

            def answer(prompt, **options):
                options['on_wait'](13)
                options['on_wait'](20)
                with self.store.connect() as c:
                    self.assertEqual(c.execute("SELECT COUNT(*) FROM session_speech WHERE kind='progress'").fetchone()[0], 1)
                if fail:
                    raise subprocess.TimeoutExpired('codex', 30)
                return {'reply':'Hello.', 'handoff':False}

            with patch.object(fast, 'run_codex', side_effect=answer), patch.object(fast.cues, 'emit'):
                if fail:
                    with self.assertLogs('ramble.fast', level='ERROR'):
                        fast.respond(conn, [item])
                else:
                    fast.respond(conn, [item])

    def test_pending_wait_notice_is_sent_once_and_cancelled_before_answer(self):
        self.respond_slowly()
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM session_speech WHERE kind='progress'").fetchone()[0], 'cancelled')
        with patch('ramble.speech.speak') as speak, self.assertLogs('ramble.latency', level='INFO') as logs:
            self.queue.tick(threading.Event())
        self.assertEqual(speak.call_args.args[0], 'Hello.')
        events = [json.loads(r.message.split('latency ',1)[1]) for r in logs.records]
        self.assertEqual({e['request_id'] for e in events}, {'request'})
        self.assertEqual(events[-1]['stage'], 'speech_job_end')
        self.assertEqual(events[-1]['status'], 'played')

    def test_failed_answer_cancels_wait_notice_and_preserves_failed_input(self):
        self.respond_slowly(fail=True)
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT status FROM session_speech WHERE kind='progress'").fetchone()[0], 'cancelled')
            self.assertEqual(conn.execute("SELECT status FROM session_inputs WHERE segment_id='request'").fetchone()[0], 'failed')
        with patch('ramble.speech.speak') as speak:
            self.queue.tick(threading.Event())
        self.assertIn('Codex timed out', speak.call_args.args[0])
