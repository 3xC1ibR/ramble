"""Reader state, cancellation and receipts; no microphone, speakers, or model calls."""
import fcntl
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

import numpy as np
import soundfile as sf

from ramble import config, reading, speech, state, voice_control
from ramble.reading import ReadingController, ReadingStore


class ReadingImportTests(unittest.TestCase):
    def test_store_and_questions_work_without_audio_or_site_packages(self):
        program = textwrap.dedent('''
            import os
            from pathlib import Path
            import sys

            sys.path.insert(0, sys.argv[1])
            home = Path(sys.argv[2])
            os.environ['RAMBLE_HOME'] = str(home)
            forbidden = {'soundfile', 'sounddevice', 'numpy', 'AppKit', 'mlx'}
            class RejectAudioImports:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == 'ramble.speech' or fullname.split('.')[0] in forbidden:
                        raise AssertionError('Store requested an audio dependency: ' + fullname)
            sys.meta_path.insert(0, RejectAudioImports())

            from ramble.reading import ReadingStore
            from ramble import reading_interaction

            store = ReadingStore(home)
            assert store.snapshot()['reading'] is None
            current = store.start('First sentence. Second sentence.', request_id='article')['reading']
            assert store.snapshot()['reading'] == current
            paused = store.control('pause', reading_id=current['id'])['reading']
            assert paused['status'] == 'paused'
            assert paused['active_index'] is None
            store.control('resume', reading_id=current['id'])
            question = reading_interaction.submit_question(
                'What does the first sentence mean?', home=home,
                reading_id=current['id'], request_id='question')
            assert question['status'] == 'pending'
            assert question['reading_id'] == current['id']
            assert reading_interaction.snapshot(home) == question
            assert store.snapshot()['reading']['status'] == 'paused'
            assert store.snapshot()['reading']['revision'] == question['revision']
            assert 'ramble.speech' not in sys.modules
            assert not forbidden.intersection(sys.modules)
            print('Store controls and question submission passed with Python ' + sys.version.split()[0])
        ''')
        interpreters = [sys.executable]
        system_python = Path('/usr/bin/python3')
        if system_python.is_file() and str(system_python) not in interpreters:
            interpreters.append(str(system_python))
        for interpreter in interpreters:
            with self.subTest(interpreter=interpreter), \
                    tempfile.TemporaryDirectory(prefix='ramble-reading-import-', dir='/tmp') as home:
                result = subprocess.run(
                    [interpreter, '-I', '-S', '-c', program,
                     str(Path(__file__).resolve().parents[1]), home],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Store controls and question submission passed', result.stdout)


class ReadingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ramble-reading-', dir='/tmp')
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.store = ReadingStore(self.home)

    def start(self, text='First sentence. Second sentence. Third sentence.', **kwargs):
        return self.store.start(text, **kwargs)['reading']

    def test_exact_source_offsets_and_bounded_first_passage(self):
        text = '  Dr. Smith wrote: “A precise claim.”\n\n' + ('Long passage ' * 100) + 'ends here.\n'
        current = self.start(text, title='Source', source_url='https://example.com/article')
        self.assertEqual(current['text'], text)
        self.assertEqual(''.join(p['text'] for p in current['passages']), text)
        for index, passage in enumerate(current['passages']):
            self.assertEqual(passage['index'], index)
            self.assertEqual(passage['text'], text[passage['start']:passage['end']])
            self.assertLessEqual(len(passage['text']), reading.MAX_PASSAGE_CHARS)
        self.assertIn('Dr. Smith', current['passages'][0]['text'])
        self.assertLessEqual(len(current['passages'][0]['text']), reading.FIRST_PASSAGE_CHARS)
        self.assertEqual(current['status'], 'preparing')
        self.assertIsNone(current['active_index'])

    def test_validation_and_request_conflict_are_atomic(self):
        for text in ['', '   ', None, 'x' * (reading.MAX_ARTICLE_CHARS + 1)]:
            with self.assertRaises(ValueError):
                self.store.start(text)
        first = self.start(request_id='first')
        with self.assertRaises(ValueError):
            self.store.start('Different source.', request_id='first')
        for index in [-1, 3, True, None]:
            with self.assertRaises(ValueError):
                self.store.control('seek', passage_index=index, request_id='invalid')
        self.assertEqual(self.store.snapshot()['reading'], first)
        self.store.control('seek', passage_index=1, request_id='invalid')

    def test_large_whitespace_spans_never_create_empty_speech_chunks(self):
        text = ' ' * 1000 + 'A source sentence.' + '\n' * 1000 + 'Another sentence.' + ' ' * 1000
        current = self.start(text)
        self.assertEqual(''.join(p['text'] for p in current['passages']), text)
        for passage in current['passages']:
            self.assertTrue(passage['text'].strip())
            self.assertLessEqual(len(passage['text'].strip()), reading.MAX_PASSAGE_CHARS)

    def test_large_article_segmentation_does_not_delay_first_passage(self):
        text = 'A source sentence. ' * 10_000
        started = time.monotonic()
        passages = reading.passages(text)
        elapsed = time.monotonic() - started
        self.assertEqual(len(text), 190_000)
        self.assertEqual(len(passages), 10_000)
        self.assertEqual(''.join(p['text'] for p in passages), text)
        # The previous full-prefix abbreviation scan took 17 seconds here.
        # Leave ample scheduling margin over the bounded scan's milliseconds.
        self.assertLess(elapsed, 2, 'Article segmentation delayed the first passage')

    def test_idempotent_operations_do_not_replay_after_later_controls(self):
        first = self.start(request_id='start')
        self.store.control('pause', request_id='pause')
        self.store.control('resume')
        resumed = self.store.snapshot()['reading']
        self.store.control('pause', request_id='pause')
        self.assertEqual(self.store.snapshot()['reading'], resumed)
        self.start('Replacement.', request_id='replacement')
        replacement = self.store.snapshot()['reading']
        self.start(request_id='start')
        self.assertEqual(self.store.snapshot()['reading'], replacement)
        self.assertNotEqual(first['id'], replacement['id'])

    def test_heartbeat_is_not_a_revision_and_explicit_pauses_are(self):
        self.start()
        self.store.attach_controller('controller')
        current = self.store.snapshot()['reading']
        self.store.heartbeat('controller')
        self.assertEqual(current, self.store.snapshot()['reading'])
        self.assertTrue(self.store.snapshot()['controller_online'])
        paused = self.store.control('pause')['reading']
        self.assertEqual(paused['revision'], current['revision'] + 1)
        self.store.detach_controller('controller')
        self.assertFalse(self.store.snapshot()['controller_online'])

    def test_paused_revision_guard_serializes_concurrent_seek(self):
        self.start()
        paused = self.store.control('pause')['reading']
        attempted = threading.Event()
        completed = threading.Event()
        def seek():
            attempted.set()
            self.store.control('seek', passage_index=1)
            completed.set()
        with self.store.paused_revision(paused['id'], paused['revision']) as valid:
            self.assertTrue(valid)
            thread = threading.Thread(target=seek)
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(completed.wait(.03))
        thread.join(1)
        self.assertTrue(completed.is_set())
        with self.store.paused_revision(paused['id'], paused['revision']) as valid:
            self.assertFalse(valid)
        current = self.store.snapshot()['reading']
        self.store.control('resume')
        with self.store.paused_revision(current['id'], current['revision']) as valid:
            self.assertFalse(valid)

    def test_only_confirmed_completion_marks_heard_and_advances(self):
        current = self.start()
        key = (current['id'], current['generation'], 0)
        self.assertFalse(self.store.progress(*key, 'completed'))
        self.assertTrue(self.store.progress(*key, 'started'))
        started = self.store.snapshot()['reading']
        self.assertEqual(started['position'], 0)
        self.assertEqual(started['active_index'], 0)
        self.assertEqual(started['heard_indices'], [])
        self.assertTrue(self.store.progress(*key, 'completed'))
        done = self.store.snapshot()['reading']
        self.assertEqual(done['position'], 1)
        self.assertEqual(done['heard_indices'], [0])
        self.assertIsNone(done['active_index'])
        self.assertFalse(self.store.progress(*key, 'completed'))

    def test_last_started_passage_survives_partial_playback_and_pause(self):
        current = self.start()
        self.assertIsNone(current['last_started_index'])
        for event in ['started', 'completed']:
            self.store.progress(current['id'], current['generation'], 0, event)
        self.assertEqual(self.store.snapshot()['reading']['last_started_index'], 0)
        self.store.progress(current['id'], current['generation'], 1, 'started')
        self.store.control('pause')
        paused = ReadingStore(self.home).snapshot()['reading']
        self.assertEqual(paused['last_started_index'], 1)
        self.assertEqual(paused['heard_indices'], [0])
        self.assertIsNone(paused['active_index'])
        self.assertEqual(self.store.control('restart')['reading']['last_started_index'], 1)
        self.assertIsNone(self.start('Replacement source.')['last_started_index'])

    def test_existing_database_migrates_without_inventing_start_history(self):
        current = self.start()
        for event in ['started', 'completed']:
            self.store.progress(current['id'], current['generation'], 0, event)
        original = self.store.control('pause')['reading']
        with self.store.connect() as conn:
            conn.execute('ALTER TABLE readings DROP COLUMN last_started_index')
        migrated = ReadingStore(self.home)
        expected = dict(original, last_started_index=None)
        self.assertEqual(migrated.snapshot()['reading'], expected)
        self.assertEqual(ReadingStore(self.home).snapshot()['reading'], expected)
        resumed = migrated.control('resume')['reading']
        migrated.progress(resumed['id'], resumed['generation'], 1, 'started')
        self.assertEqual(migrated.snapshot()['reading']['last_started_index'], 1)

    def test_seek_does_not_imply_skipped_text_was_heard(self):
        self.start()
        current = self.store.control('seek', passage_index=2)['reading']
        self.assertEqual(current['heard_indices'], [])
        current = self.store.control('resume')['reading']
        self.store.progress(current['id'], current['generation'], 2, 'started')
        self.store.progress(current['id'], current['generation'], 2, 'completed')
        result = self.store.snapshot()['reading']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['heard_indices'], [2])
        restarted = self.store.control('restart')['reading']
        self.assertEqual(restarted['position'], 0)
        self.assertEqual(restarted['status'], 'paused')
        self.assertEqual(restarted['heard_indices'], [2])

    def test_stale_receipts_cannot_override_pause_seek_or_replacement(self):
        for action in ['pause', 'seek', 'replacement']:
            with self.subTest(action=action):
                old = self.start()
                key = (old['id'], old['generation'], 0)
                self.store.progress(*key, 'started')
                if action == 'replacement':
                    self.start('A different source.')
                elif action == 'seek':
                    self.store.control('seek', passage_index=1)
                else:
                    self.store.control('pause')
                expected = self.store.snapshot()['reading']
                for event in ['started', 'completed', 'failed', 'paused']:
                    self.assertFalse(self.store.progress(*key, event))
                self.assertEqual(self.store.snapshot()['reading'], expected)

    def test_completed_article_can_seek_and_repeat_updates_last_heard(self):
        current = self.start()
        for index in range(3):
            for event in ['started', 'completed']:
                self.store.progress(current['id'], current['generation'], index, event)
        self.assertEqual(self.store.snapshot()['reading']['heard_indices'], [0, 1, 2])
        self.store.control('seek', passage_index=0)
        current = self.store.control('resume')['reading']
        for event in ['started', 'completed']:
            self.store.progress(current['id'], current['generation'], 0, event)
        self.assertEqual(self.store.snapshot()['reading']['heard_indices'], [1, 2, 0])
        self.assertEqual(self.store.snapshot()['reading']['position'], 1)

    def test_recovery_preserves_current_sentence_and_requires_resume(self):
        current = self.start()
        for event in ['started', 'completed']:
            self.store.progress(current['id'], current['generation'], 0, event)
        self.store.progress(current['id'], current['generation'], 1, 'started')
        reopened = ReadingStore(self.home)
        reopened.attach_controller('restarted-controller')
        recovered = reopened.snapshot()['reading']
        self.assertEqual(recovered['position'], 1)
        self.assertEqual(recovered['heard_indices'], [0])
        self.assertEqual(recovered['last_started_index'], 1)
        self.assertEqual(recovered['status'], 'paused')
        self.assertIsNone(recovered['active_index'])
        self.assertFalse(reopened.progress(current['id'], current['generation'], 1, 'completed'))
        resumed = reopened.control('resume')['reading']
        self.assertEqual(resumed['position'], 1)
        self.assertEqual(resumed['status'], 'preparing')


class ReadingControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ramble-reading-', dir='/tmp')
        self.home = Path(self.temporary.name)
        self.store = ReadingStore(self.home)
        self.stop = threading.Event()
        self.cfg = dict(config.DEFAULTS, fast_speak=True, speak_questions=False, dispatch_daily_cap=0)
        self.controller = ReadingController(self.cfg, self.store)
        self.allow_synthesis = threading.Event()
        self.allow_synthesis.set()
        self.synthesis_entered = threading.Event()
        self.allow_start = threading.Event()
        self.allow_start.set()
        self.allow_completion = threading.Event()
        self.playback_entered = threading.Event()
        self.output_started = threading.Event()
        self.synthesized = []
        self.played = []
        self.synthesizer = patch.object(speech, 'synthesize', side_effect=self.synthesize)
        self.player = patch.object(speech, 'play_prepared', side_effect=self.play)
        self.synthesizer.start()
        self.player.start()

    def tearDown(self):
        futures = list(self.controller.pending.values())
        if self.controller.playback:
            futures.append(self.controller.playback)
        self.stop.set()
        self.allow_synthesis.set()
        self.allow_start.set()
        self.allow_completion.set()
        self.controller.close()
        for future in futures:
            try:
                future.result(timeout=2)
            except Exception:
                pass
        self.player.stop()
        self.synthesizer.stop()
        self.temporary.cleanup()

    def synthesize(self, text, cfg, *, output):
        self.synthesized.append(text)
        self.synthesis_entered.set()
        if not self.allow_synthesis.wait(2):
            raise TimeoutError('Test did not release synthesis')
        sf.write(output, np.zeros(160), 16000)
        return speech.SpeechResult(Path(output), 'fixture', .01)

    def play(self, path, cfg, *, cancelled, origin, on_playback_start, home):
        self.assertEqual(home, self.home)
        self.assertEqual(origin, 'reading')
        self.played.append(path)
        self.playback_entered.set()
        for permission, callback in [(self.allow_start, on_playback_start),
                                     (self.allow_completion, lambda: None)]:
            deadline = time.monotonic() + 3
            while not permission.wait(.005):
                if cancelled():
                    raise state.PausedError('Fixture output cancelled')
                if time.monotonic() > deadline:
                    raise TimeoutError('Test did not release playback')
            if cancelled():
                raise state.PausedError('Fixture output cancelled')
            callback()
            self.output_started.set()

    def drive(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.controller.tick(self.stop)
            if time.monotonic() > deadline:
                self.fail(f'Reader did not reach expected state: {self.store.snapshot()}')
            time.sleep(.005)

    def current(self):
        return self.store.snapshot()['reading']

    def start(self, text='First sentence. Second sentence. Third sentence.'):
        return self.store.start(text)['reading']

    def test_highlight_waits_for_actual_output_and_progress_waits_for_completion(self):
        self.allow_start.clear()
        self.start('One short sentence.')
        self.drive(self.playback_entered.is_set)
        self.assertIsNone(self.current()['active_index'])
        self.assertIsNone(self.current()['last_started_index'])
        self.assertEqual(self.current()['status'], 'preparing')
        self.allow_start.set()
        self.drive(lambda: self.current()['active_index'] == 0)
        self.assertEqual(self.current()['position'], 0)
        self.assertEqual(self.current()['last_started_index'], 0)
        self.assertEqual(self.current()['heard_indices'], [])
        self.allow_completion.set()
        self.drive(lambda: self.current()['status'] == 'completed')
        self.assertEqual(self.current()['position'], 1)
        self.assertEqual(self.current()['heard_indices'], [0])

    def test_pause_during_preparation_never_submits_audio(self):
        self.allow_synthesis.clear()
        self.start()
        self.drive(self.synthesis_entered.is_set)
        self.store.control('pause')
        self.allow_synthesis.set()
        self.drive(lambda: not self.controller.pending)
        self.assertEqual(self.played, [])
        self.assertEqual(self.current()['position'], 0)
        self.assertEqual(self.current()['status'], 'paused')

    def test_pause_during_playback_resumes_same_sentence_and_reuses_cache(self):
        self.start('One sentence.')
        self.drive(self.output_started.is_set)
        self.store.control('pause')
        self.drive(lambda: self.controller.playback is None)
        self.assertEqual(self.current()['heard_indices'], [])
        self.assertEqual(self.current()['position'], 0)
        self.store.control('resume')
        self.allow_completion.set()
        self.drive(lambda: self.current()['status'] == 'completed')
        self.assertEqual(len(self.synthesized), 1)
        self.assertEqual(len(self.played), 2)

    def assert_current_bypasses_stale_prefetch(self, *, replacement):
        release = threading.Event()
        entered = {text: threading.Event() for text in ['Second sentence.', 'Third sentence.']}
        def prepare(text, cfg, *, output):
            if text in entered:
                entered[text].set()
                if not release.wait(3):
                    raise TimeoutError('Test did not release speculative synthesis')
            return self.synthesize(text, cfg, output=output)
        with patch.object(speech, 'synthesize', side_effect=prepare):
            try:
                self.start()
                self.drive(lambda: self.output_started.is_set()
                           and all(event.is_set() for event in entered.values()))
                self.store.control('pause')
                self.drive(lambda: self.controller.playback is None)
                self.output_started.clear()
                if replacement:
                    self.start('Replacement source.')
                else:
                    self.store.control('resume')
                self.drive(self.output_started.is_set, timeout=1)
                self.assertFalse(release.is_set())
                self.assertEqual(len(self.played), 2)
                self.assertEqual(len(self.synthesized), 2 if replacement else 1)
                self.assertEqual(self.current()['active_index'], 0)
                self.assertEqual(len(self.controller.speculative_jobs), 2)
                self.assertTrue(all(not future.done() for future in self.controller.speculative_jobs))
                self.assertLessEqual(len(self.controller.pending), 3)
            finally:
                release.set()
                for future in self.controller.speculative_jobs:
                    try:
                        future.result(timeout=2)
                    except state.PausedError:
                        pass

    def test_cached_resume_bypasses_two_blocked_stale_prefetch_calls(self):
        self.assert_current_bypasses_stale_prefetch(replacement=False)

    def test_new_current_passage_bypasses_two_blocked_stale_prefetch_calls(self):
        self.assert_current_bypasses_stale_prefetch(replacement=True)

    def test_replacement_cancels_pending_preparation(self):
        self.allow_synthesis.clear()
        old = self.start('Old source.')
        self.drive(self.synthesis_entered.is_set)
        replacement = self.start('Replacement source.')
        self.allow_synthesis.set()
        self.drive(self.output_started.is_set)
        self.assertEqual(self.current()['id'], replacement['id'])
        self.assertTrue(all(old['id'] not in str(p) for p in self.played))

    def test_seek_during_output_does_not_count_interrupted_passage(self):
        self.start()
        self.drive(self.output_started.is_set)
        self.store.control('seek', passage_index=2)
        self.drive(lambda: self.controller.playback is None)
        self.assertEqual(self.current()['position'], 2)
        self.assertEqual(self.current()['heard_indices'], [])
        self.assertEqual(self.current()['status'], 'paused')

    def test_global_mute_or_snooze_clears_automatic_resume(self):
        for action in ['mute', 'snooze']:
            with self.subTest(action=action):
                self.output_started.clear()
                self.start('A sentence.')
                self.drive(self.output_started.is_set)
                if action == 'mute':
                    voice_control.write(False, home=self.home)
                    voice_control.write(True, home=self.home)
                else:
                    state.write(True, time.time() + 3600, home=self.home)
                    state.write(True, home=self.home)
                self.drive(lambda: self.current()['status'] == 'paused' and self.controller.playback is None)
                revision = self.current()['revision']
                self.controller.tick(self.stop)
                self.assertEqual(self.current()['revision'], revision)
                self.assertEqual(self.current()['heard_indices'], [])

    def test_yields_to_human_and_normal_speech_before_playback(self):
        self.controller.speech_active = lambda: True
        self.start()
        self.drive(self.synthesis_entered.is_set)
        self.drive(lambda: bool(self.controller.pending) and all(f.done() for f in self.controller.pending.values()))
        self.controller.tick(self.stop)
        self.assertEqual(self.played, [])
        self.controller.speech_active = lambda: False
        self.controller.can_play = lambda: False
        self.controller.tick(self.stop)
        self.assertEqual(self.played, [])
        self.controller.can_play = lambda: True
        self.drive(self.output_started.is_set)

    def test_first_passage_prepares_alone_then_prefetch_is_bounded(self):
        self.allow_start.clear()
        self.start('First sentence. ' + 'Another sentence. ' * 20)
        self.drive(self.playback_entered.is_set)
        self.assertEqual(len(self.synthesized), 1)
        self.allow_start.set()
        self.drive(lambda: len(self.synthesized) == 3)
        for _ in range(10):
            self.controller.tick(self.stop)
        self.assertEqual(len(self.synthesized), 3)
        self.assertEqual(len(self.controller.pending), 3)

    def test_synthesis_failure_preserves_position_and_can_retry(self):
        self.synthesizer.stop()
        self.synthesizer = patch.object(speech, 'synthesize', side_effect=OSError('fixture failure'))
        self.synthesizer.start()
        self.start()
        with self.assertLogs('ramble.reading', level='ERROR'):
            self.drive(lambda: self.current()['status'] == 'failed')
        self.assertEqual(self.current()['position'], 0)
        self.assertEqual(self.current()['heard_indices'], [])
        self.assertEqual(self.played, [])
        self.synthesizer.stop()
        self.synthesizer = patch.object(speech, 'synthesize', side_effect=self.synthesize)
        self.synthesizer.start()
        self.store.control('resume')
        self.drive(self.output_started.is_set)

    def test_completed_without_start_receipt_fails_closed(self):
        self.player.stop()
        self.player = patch.object(speech, 'play_prepared', return_value=None)
        self.player.start()
        self.start()
        with self.assertLogs('ramble.reading', level='ERROR'):
            self.drive(lambda: self.current()['status'] == 'failed')
        self.assertEqual(self.current()['position'], 0)
        self.assertEqual(self.current()['heard_indices'], [])

    def test_output_failure_after_start_keeps_uncompleted_passage(self):
        def fail(path, cfg, **kwargs):
            kwargs['on_playback_start']()
            raise OSError('Fixture output disconnected')
        self.player.stop()
        self.player = patch.object(speech, 'play_prepared', side_effect=fail)
        self.player.start()
        self.start()
        with self.assertLogs('ramble.reading', level='ERROR'):
            self.drive(lambda: self.current()['status'] == 'failed')
        self.assertEqual(self.current()['position'], 0)
        self.assertEqual(self.current()['heard_indices'], [])
        self.assertIsNone(self.current()['active_index'])

    def test_idle_heartbeat_remains_online_without_changing_revision(self):
        thread = threading.Thread(target=self.controller.run_forever, args=(self.stop,))
        thread.start()
        try:
            deadline = time.monotonic() + 2
            while not self.store.snapshot()['controller_online']:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.start('Paused article.')
            self.store.control('pause')
            expected = self.current()
            time.sleep(1.1)
            self.assertTrue(self.store.snapshot()['controller_online'])
            self.assertEqual(self.current(), expected)
        finally:
            self.stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.store.snapshot()['controller_online'])

    def test_cache_is_bounded(self):
        self.controller.cache_files = 3
        for index in range(10):
            (self.controller.cache / f'cached-{index}.wav').write_bytes(b'fixture')
        self.controller._prune_cache()
        self.assertEqual(len(list(self.controller.cache.glob('*.wav'))), 3)

    def test_cache_reserves_protected_audio_before_retaining_old_files(self):
        protected = self.controller.cache / 'protected.wav'
        recent = self.controller.cache / 'recent.wav'
        protected.write_bytes(b'protected')
        time.sleep(.002)
        recent.write_bytes(b'recent')
        self.controller.cache_bytes = len(b'protected')
        self.controller._prune_cache({protected})
        self.assertTrue(protected.exists())
        self.assertFalse(recent.exists())


class PreparedPlaybackTests(unittest.TestCase):
    def test_prepared_playback_guard_and_origin_use_explicit_home(self):
        with tempfile.TemporaryDirectory(prefix='ramble-reading-', dir='/tmp') as folder:
            home = Path(folder) / 'reader'
            home.mkdir()
            unrelated = Path(folder) / 'unrelated'
            path = home / 'prepared.wav'
            sf.write(path, np.zeros(160), 16000)
            starts = []
            def play_file(audio_path, *, on_start, cancelled, **kwargs):
                with sqlite3.connect(home / 'ramble.db') as conn:
                    row = conn.execute('SELECT started_at,ended_at,origin FROM speech_playbacks '
                                       'JOIN speech_origins ON id=playback_id').fetchone()
                self.assertLess(row[0], time.time())
                self.assertGreater(row[1], time.time())
                self.assertEqual(row[2], 'reading')
                self.assertFalse(cancelled())
                on_start({'first_frame_mono_s': time.monotonic()})
            player = Mock(play_file=Mock(side_effect=play_file))
            with patch.multiple(config, HOME=unrelated, DB_PATH=unrelated / 'ramble.db',
                                STATE_PATH=unrelated / 'state.json'), \
                    patch.object(speech, 'shared_player', return_value=player):
                speech.play_prepared(path, config.DEFAULTS, home=home,
                                     on_playback_start=lambda: starts.append(True))
            self.assertEqual(starts, [True])
            self.assertFalse(unrelated.exists())

    def test_serialization_wait_can_be_cancelled_without_playing(self):
        with tempfile.TemporaryDirectory(prefix='ramble-reading-', dir='/tmp') as folder:
            home = Path(folder)
            cancelled = threading.Event()
            errors = []
            def run():
                try:
                    speech.play_prepared(home / 'unneeded.wav', config.DEFAULTS,
                                         home=home, cancelled=cancelled.is_set)
                except state.PausedError as exc:
                    errors.append(exc)
            with (home / 'speech.lock').open('a') as lock, patch.object(speech, 'play_audio') as player:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                thread = threading.Thread(target=run)
                thread.start()
                time.sleep(.05)
                cancelled.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(errors), 1)
                player.assert_not_called()


if __name__ == '__main__':
    unittest.main()
