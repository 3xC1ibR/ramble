"""Opening audio previews never admit work or touch a microphone/speaker."""

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np

from ramble import config, db, state, voice_control
from ramble.fastlane import FastLane
from ramble.recorder import CHUNK_S, Segmenter
from ramble.transcribe import Transcriber
from ramble.vad import CHUNK


def result(text, **confidence):
    return {'text': text, 'segments': [{'no_speech_prob': .01, 'avg_logprob': -.1,
                                       'compression_ratio': 1, **confidence}]}


class EarlyWakeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-wake-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        self.enterContext(patch.multiple(config, HOME=home, STATE_PATH=home/'state.json'))
        state.write(True)
        self.cfg = dict(config.DEFAULTS, fast_speak=True, dispatch_daily_cap=0,
                        speak_questions=False)
        self.fast = FastLane(self.cfg)
        self.early = self.fast.early
        self.start = time.time()
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)

    def preview(self, text='Hey Ramble, can you help me?', **confidence):
        self.early.offer(self.start, np.zeros(19200, dtype=np.float32))
        preview = self.early.take()
        self.assertIsNotNone(preview)
        r = result(text)
        r['segments'][0].update(confidence)
        self.early.recognize(preview, r)
        return preview

    def full(self, text):
        return self.fast.offer(self.conn, {'id': 'source', 'started_at': self.start,
                                         'ended_at': self.start + 19}, text)

    def test_preview_only_acknowledges_and_full_request_does_not_ding_again(self):
        self.preview()
        self.assertEqual(self.fast.cues.q.qsize(), 1)
        self.assertTrue(self.fast.cues.work_active())
        self.assertTrue(self.fast.q.empty())
        self.assertEqual(list(self.fast.history), [])
        self.assertEqual(self.fast.name_only_until, 0)
        self.assertEqual(self.fast.request_until, 0)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_claims').fetchone()[0], 0)
        self.assertTrue(self.full('Hey Ramble, can you help me with this?'))
        self.assertEqual(self.fast.cues.q.qsize(), 1)
        self.assertEqual(self.fast.q.qsize(), 1)

    def test_bare_name_does_not_pulse_or_admit_a_followup_until_full_recognition(self):
        self.preview('Hey Ramble?')
        self.assertFalse(self.fast.cues.work_active())
        self.assertEqual(self.fast.name_only_until, 0)
        self.assertFalse(self.full('Hey Ramble?'))
        self.assertEqual(self.fast.cues.q.qsize(), 1)
        self.assertGreater(self.fast.name_only_until, self.start)

    def test_false_preview_is_rejected_by_full_transcription(self):
        self.preview()
        self.assertFalse(self.full("Right, but what's the easiest way?"))
        self.assertTrue(self.fast.q.empty())
        self.assertFalse(self.fast.cues.work_active())

    def test_false_preview_does_not_leave_working_feedback_on_a_passive_candidate(self):
        self.preview()
        self.fast.sessions = Mock()
        self.fast.sessions.followup_owner.return_value = {'session_id': 'owner'}
        self.fast.jev = Mock(available=False)
        self.assertTrue(self.full('A background comment'))
        self.assertTrue(self.fast.q.empty())
        self.assertEqual(self.fast.candidates.qsize(), 1)
        self.assertFalse(self.fast.cues.work_active())

    def test_discarded_capture_cancels_its_provisional_feedback(self):
        complete = Mock()
        segmenter = Segmenter(dict(self.cfg, min_speech_s=10), complete,
                              on_preview=self.early.offer, on_discard=self.early.discard)
        for i in range(40):
            segmenter._feed(np.zeros(CHUNK, dtype=np.float32), self.start + i * CHUNK_S, .7)
        preview = self.early.take()
        self.early.recognize(preview, result('Hey Ramble, help me'))
        self.assertTrue(self.fast.cues.work_active())
        segmenter.flush()
        complete.assert_not_called()
        self.assertFalse(self.fast.cues.work_active())
        self.assertFalse(self.early.valid(preview))

    def test_bounded_prefixes_arrive_before_complete_segment(self):
        prefixes, complete = [], []
        segmenter = Segmenter(self.cfg, lambda ts, a: complete.append(a),
                              on_preview=lambda ts, a: prefixes.append((ts, len(a))))
        chunk = np.zeros(CHUNK, dtype=np.float32)
        for i in range(200):
            segmenter._feed(chunk, self.start+i*CHUNK_S, .99)
        self.assertEqual(complete, [])
        self.assertEqual(len(prefixes), 3)
        self.assertEqual([round(n/16000, 1) for _, n in prefixes], [1.2, 2.4, 4.0])
        segmenter.flush()
        self.assertEqual(len(complete), 1)
        self.assertEqual(len(complete[0]), 200*CHUNK)

    def test_mailbox_replaces_older_audio_and_stops_after_request_recognition(self):
        self.early.offer(self.start, np.zeros(19200))
        self.early.offer(self.start, np.zeros(38400))
        preview = self.early.take()
        self.assertEqual(len(preview.audio), 38400)
        self.assertIsNone(self.early.take())
        self.early.recognize(preview, result('Hey Ramble, hello.'))
        self.early.offer(self.start, np.zeros(64000))
        self.assertIsNone(self.early.take())

    def test_finished_cancelled_and_stale_previews_never_chime(self):
        for invalidate in [lambda p: self.early.finish(self.start),
                           lambda p: self.early.reset()]:
            self.early.offer(self.start, np.zeros(19200))
            preview = self.early.take()
            invalidate(preview)
            self.early.recognize(preview, result('Hey Ramble, hello.'))
            self.assertTrue(self.fast.cues.q.empty())
        self.early.offer(self.start, np.zeros(19200))
        preview = self.early.take()
        with patch('ramble.early_wake.time.monotonic', return_value=preview.captured_at+3):
            self.early.recognize(preview, result('Hey Ramble, hello.'))
        self.assertTrue(self.fast.cues.q.empty())

    def test_low_confidence_and_nonopening_mentions_do_not_acknowledge(self):
        for text, score in [('We should ask Ramble about it.', {}),
                            ('"Hey Ramble," she said.', {}),
                            ('Right, but what now?', {}),
                            ('Hey Ramble', {'avg_logprob': -1.2}),
                            ('Hey Ramble', {'no_speech_prob': .9}),
                            ('Ramble, disable voice responses.', {})]:
            self.preview(text, **score)
            self.assertTrue(self.fast.cues.q.empty())
            self.assertTrue(self.fast.q.empty())

    def test_disabled_muted_off_and_paused_previews_are_not_queued(self):
        for field in ['early_wake_enabled', 'fast_enabled', 'fast_speak', 'audio_cues_enabled']:
            self.cfg[field] = False
            self.early.offer(self.start, np.zeros(19200))
            self.assertIsNone(self.early.take())
            self.cfg[field] = True
        voice_control.write(False)
        self.early.offer(self.start, np.zeros(19200))
        self.assertIsNone(self.early.take())
        voice_control.write(True)
        self.start = time.time()
        state.write(False)
        self.early.offer(self.start, np.zeros(19200))
        self.assertIsNone(self.early.take())
        state.write(True, time.time()+60)
        self.early.offer(self.start, np.zeros(19200))
        self.assertIsNone(self.early.take())

    def test_reset_cancels_preview_work_but_not_a_later_request(self):
        self.preview()
        later = self.fast.cues.start_work(self.start+30)
        self.early.reset()
        self.assertTrue(self.fast.cues.work_active(later))

    def test_transcriber_previews_are_unhinted_bounded_and_never_publish_text(self):
        transcriber = Transcriber(self.cfg, threading.Event())
        transcriber.early = self.early
        transcriber.on_utterance = Mock()
        self.early.offer(self.start, np.zeros(19200))
        with patch.object(transcriber, '_decode', return_value=result('Hey Ramble, hello.')) as decode:
            transcriber.process_preview(self.early.take())
        self.assertIsNone(decode.call_args.kwargs['initial_prompt'])
        self.assertEqual(decode.call_args.kwargs['temperature'], 0)
        self.assertEqual(decode.call_args.kwargs['sample_len'], 48)
        transcriber.on_utterance.assert_not_called()

    def test_completed_files_take_priority_over_previews(self):
        transcriber = Transcriber(self.cfg, threading.Event())
        transcriber.early = self.early
        self.early.offer(self.start, np.zeros(19200))
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES ('full',1,2,1,'unused')")
        stop = threading.Event()
        with patch('ramble.transcribe.db.connect', return_value=self.conn), \
                patch.object(transcriber, 'warmup'), patch.object(transcriber, 'memory'), \
                patch.object(transcriber, 'process', side_effect=lambda *_: stop.set()) as full, \
                patch.object(transcriber, 'process_preview') as partial:
            transcriber.run_forever(stop)
        # Worker closes its connection; prevent the shared cleanup closing twice.
        full.assert_called_once()
        partial.assert_not_called()


if __name__ == '__main__':
    unittest.main()
