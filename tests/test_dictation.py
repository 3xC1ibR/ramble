"""Dictation boundaries and routing without devices, agents or real clipboard writes."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from ramble import config, db, state
from ramble.dictation import Dictation
from ramble.recorder import Recorder, CHUNK_S, save_segment
from ramble.router import Router
from ramble.transcribe import Transcriber
from ramble.vad import CHUNK


class DictationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ramble-test-dictation-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        paths = dict(HOME=home, DB_PATH=home / "ramble.db", STATE_PATH=home / "state.json")
        paths.update({key: home / key.lower() for key in ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        self.enterContext(patch.multiple(config, **paths))
        state.write(True)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False, speak_questions=False)
        self.recorder = SimpleNamespace(active=True, error=None, set_dictation=Mock())
        self.desktop = Mock()
        self.desktop.target.return_value = (123, "focused input")
        self.desktop.paste.return_value = (True, "Pasted")
        self.controller = Dictation(self.recorder)
        self.transcriber = Transcriber(self.cfg, threading.Event())
        self.transcriber.on_utterance = Mock()

    def segment(self, source, timestamp=100):
        identifier = save_segment(self.conn, timestamp, np.ones(config.SAMPLE_RATE, dtype=np.float32), source)
        return self.conn.execute("SELECT * FROM segments WHERE id=?", (identifier,)).fetchone()

    def transcribe(self, row, text):
        decoded = dict(text=text, segments=[dict(no_speech_prob=0, avg_logprob=0)])
        with patch.object(self.transcriber, "transcribe_path", return_value=decoded):
            self.transcriber.process(self.conn, row)

    def test_dictation_bypasses_fast_lane_jev_triage_and_ambient_grouping(self):
        ambient = self.segment("mic")
        self.transcribe(ambient, "A background thought.")
        self.transcriber.on_utterance.reset_mock()
        for timestamp, text in ((102, "Ramble, delete the file."), (104, "Thank you."), (106, "Okay.")):
            self.transcribe(self.segment("dictation:explicit", timestamp), text)
        self.transcriber.on_utterance.assert_not_called()
        thought = self.conn.execute("SELECT * FROM thoughts WHERE id='explicit'").fetchone()
        self.assertEqual((thought['status'], thought['classification']), ('done', 'dictation'))
        self.assertIn("Thank you.", thought['text'])
        self.assertIn("Okay.", thought['text'])
        ambient_thought = self.conn.execute("SELECT * FROM thoughts WHERE status='open'").fetchone()
        self.assertEqual(ambient_thought['ended_at'], 101)
        self.assertNotIn("delete", ambient_thought['text'])
        router = Router(self.cfg, threading.Event())
        # Even an explicit CLI retriage cannot turn a dictation into an action.
        self.conn.execute("UPDATE thoughts SET status='closed' WHERE id='explicit'")
        with patch.object(router, "run_triage", create=True) as model:
            router.triage(self.conn, thought)
        model.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT status FROM thoughts WHERE id='explicit'").fetchone()[0], 'done')
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM dispatches").fetchone()[0], 0)

    def test_recorder_serializes_boundaries_and_disables_wake_previews(self):
        recorder = Recorder(self.cfg, threading.Event())
        recorder.early = Mock()
        def audio(start):
            for index in range(50):
                recorder.q.put((start + index * CHUNK_S, np.ones(CHUNK, dtype=np.float32)))
        audio(100)
        recorder.set_dictation("explicit")
        audio(102)
        sealed = threading.Event()
        recorder.set_dictation(None, sealed.set)
        audio(104)
        recorder.q.put(None)
        with patch("ramble.recorder.SileroVAD") as vad:
            vad.return_value.return_value = .99
            recorder._process()
        rows = self.conn.execute("SELECT * FROM segments ORDER BY started_at").fetchall()
        self.assertEqual([row['source'] for row in rows], ['mic', 'dictation:explicit', 'mic'])
        self.assertEqual([row['duration'] for row in rows], [50 * CHUNK_S] * 3)
        self.assertTrue(sealed.is_set())
        preview_starts = [call.args[0] for call in recorder.early.offer.call_args_list]
        self.assertTrue(preview_starts)
        self.assertNotIn(102, preview_starts)
        self.assertNotIn(102, [call.args[0] for call in recorder.early.finish.call_args_list])

    def test_explicit_dictation_finishes_before_an_ambient_backlog(self):
        self.segment("mic", 100)
        self.segment("dictation:explicit", 102)
        processed = []
        stop = threading.Event()
        def process(conn, row):
            processed.append(row['source'])
            conn.execute("UPDATE segments SET status='empty' WHERE id=?", (row['id'],))
            if len(processed) == 2:
                stop.set()
        with patch.object(self.transcriber, "warmup"), patch.object(self.transcriber, "memory"), \
                patch.object(self.transcriber, "process", side_effect=process):
            self.transcriber.run_forever(stop)
        self.assertEqual(processed, ["dictation:explicit", "mic"])

    def test_waits_for_boundary_and_all_transcripts_then_pastes_once(self):
        self.controller.toggle(self.desktop)
        recording = self.controller.recording
        first = self.segment(f"dictation:{recording.id}", 100)
        second = self.segment(f"dictation:{recording.id}", 102)
        self.transcribe(first, "First sentence.")
        self.controller.toggle(self.desktop)
        self.assertEqual(self.controller.activity.label, "Transcribing")
        self.controller.toggle(self.desktop)  # A repeat while finishing is ignored.
        self.controller.poll(self.desktop)
        self.desktop.copy.assert_not_called()
        self.recorder.set_dictation.call_args.args[1]()  # recorder acknowledges flush
        self.controller.poll(self.desktop)
        self.desktop.paste.assert_not_called()
        self.transcribe(second, "Second sentence.")
        deadline = time.monotonic() + 3
        while self.controller.recording and time.monotonic() < deadline:
            self.controller.poll(self.desktop)
            time.sleep(.01)
        self.assertIsNone(self.controller.recording)
        self.desktop.copy.assert_called_once_with("First sentence. Second sentence.")
        self.desktop.paste.assert_called_once_with((123, "focused input"))
        self.controller.poll(self.desktop)
        self.desktop.paste.assert_called_once()

    def test_pause_and_late_results_never_paste(self):
        self.controller.toggle(self.desktop)
        identifier = self.controller.recording.id
        state.write(True, time.time() + 3600)
        self.controller.results.put((identifier, "Do not paste.", None))
        self.controller.poll(self.desktop)
        self.desktop.copy.assert_not_called()
        self.desktop.paste.assert_not_called()
        self.assertIsNone(self.controller.recording)
        self.assertEqual(self.controller.activity.label, "Cancelled")

    def deliver(self):
        self.controller.toggle(self.desktop)
        self.controller.results.put((self.controller.recording.id, "Dictated text.", None))
        self.controller.poll(self.desktop)

    def test_paste_offers_one_undo_and_keeps_clipboard_contents(self):
        self.deliver()
        self.assertTrue(self.controller.can_undo)
        self.assertEqual(self.controller.activity.mode, "pasted")
        self.desktop.prepare_undo.assert_called_once_with((123, "focused input"), "Dictated text.")
        self.desktop.undo.return_value = (True, "Undo sent.")
        self.controller.undo(self.desktop)
        self.controller.undo(self.desktop)
        self.desktop.undo.assert_called_once_with(self.desktop.prepare_undo.return_value)
        self.desktop.copy.assert_called_once_with("Dictated text.")
        self.assertFalse(self.controller.can_undo)
        self.assertEqual(self.controller.activity.label, "Undone")

    def test_undo_expires_and_late_click_does_nothing(self):
        self.deliver()
        with patch('ramble.dictation.time.monotonic', return_value=self.controller.notice_until):
            self.controller.poll(self.desktop)
            self.assertFalse(self.controller.can_undo)
            self.assertIsNone(self.controller.activity)
            self.controller.undo(self.desktop)
        self.desktop.undo.assert_not_called()

    def test_new_typing_and_new_dictation_dismiss_old_undo(self):
        self.deliver()
        self.desktop.undo_unchanged.return_value = False
        self.controller.poll(self.desktop)
        self.assertFalse(self.controller.can_undo)
        self.assertIsNone(self.controller.activity)
        self.deliver()
        self.controller.toggle(self.desktop)
        self.assertFalse(self.controller.can_undo)
        self.controller.undo(self.desktop)
        self.desktop.undo.assert_not_called()
        self.assertIsNotNone(self.controller.recording)

    def test_clipboard_only_delivery_does_not_offer_undo(self):
        self.desktop.paste.return_value = (False, "Copied.")
        self.deliver()
        self.assertFalse(self.controller.can_undo)
        self.assertEqual(self.controller.activity.label, "Copied")

    def test_rejected_undo_reports_failure_and_cannot_be_repeated(self):
        self.deliver()
        self.desktop.undo.return_value = (False, "The selected input changed.")
        self.controller.undo(self.desktop)
        self.assertFalse(self.controller.can_undo)
        self.assertEqual(self.controller.activity.label, "Undo unavailable")
        self.assertEqual(self.controller.activity.detail, "The selected input changed.")

    def test_empty_and_partial_failure_do_not_paste(self):
        for text, error, label in (("", None, "No speech"), ("Partial.", "Decode failed", "Check dictation")):
            self.desktop.reset_mock()
            self.controller.toggle(self.desktop)
            self.controller.results.put((self.controller.recording.id, text, error))
            self.controller.poll(self.desktop)
            self.assertEqual(self.controller.activity.label, label)
            self.desktop.paste.assert_not_called()
            if text:
                self.desktop.copy.assert_called_once_with(text)
            else:
                self.desktop.copy.assert_not_called()

    def test_microphone_off_is_respected_and_notice_expires(self):
        state.write(False)
        before = config.STATE_PATH.read_bytes()
        self.controller.toggle(self.desktop)
        self.assertIsNone(self.controller.recording)
        self.assertEqual(self.controller.activity.label, "Mic off")
        self.assertEqual(config.STATE_PATH.read_bytes(), before)
        self.recorder.set_dictation.assert_not_called()
        self.controller.notice_until = 0
        self.controller.poll(self.desktop)
        self.assertIsNone(self.controller.activity)


if __name__ == "__main__":
    unittest.main()
