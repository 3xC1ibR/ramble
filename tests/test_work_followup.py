"""Bounded admission and mixed thought timing; fake models/native owner, no audio."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import config
from ramble.fastlane import FastLane, FOLLOWUP_SCHEMA
from ramble.router import Router
from ramble.sessions import Sessions
from ramble.session_store import SessionStore
from ramble.transcribe import Transcriber


QUESTION = ('So if I were to ask you to show me something on the iPad on the companion app '
            'from this session, how would you do that? And where would it show up on the iPad?')


class WorkFollowupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-followup-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for name, path in {'HOME': self.home, 'DB_PATH': self.home / 'ramble.db',
                           'STATE_PATH': self.home / 'state.json',
                           'DIGEST_DIR': self.home / 'digest',
                           'TRIAGE_DIR': self.home / 'triage'}.items():
            self.enterContext(patch.object(config, name, path))
        config.ensure_dirs()
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False,
                        speak_questions=False, audio_cues_enabled=False, notify=False,
                        work_followup_s=120)
        (self.home / 'config.json').write_text(json.dumps(self.cfg))
        self.store = SessionStore(self.home)
        self.conn = self.enterContext(self.store.connect())
        self.now = time.time()
        self.backend = Mock()
        self.backend.create.side_effect = AssertionError('must never launch')
        self.backend.command.return_value = {'status': 'completed', 'thread_id': 'native-owner'}
        self.backend.snapshot.return_value = {
            'connected': True, 'status': 'idle', 'active_turn_id': None,
            'last_turn_id': None, 'events': [], 'cursor': 0,
        }
        self.sessions = Sessions(self.cfg, self.backend, self.store)
        self.fast = FastLane(self.cfg)
        self.fast.sessions = self.sessions
        self.router = Router(self.cfg, threading.Event())
        self.conn.execute(
            "INSERT INTO work_sessions(id,name,goal,cwd,kind,thread_id,status,connected,created_at,updated_at) "
            "VALUES ('owner','Ramble investigation','Trace voice to Companion',?,'interactive','native-owner','idle',1,?,?)",
            (str(self.home), self.now - 300, self.now),
        )
        self.conn.execute(
            "INSERT INTO session_events(session_id,event_key,kind,text,payload,created_at) "
            "VALUES ('owner','progress','message','I traced the spoken request to the existing session.',"
            "'{\"phase\":\"final_answer\"}',?)",
            (self.now - 60,),
        )

    def transcribe(self, text, identifier='candidate', start=None):
        start = self.now if start is None else start
        audio = self.home / (identifier + '.flac')
        self.conn.execute(
            "INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES (?,?,?,2,?)",
            (identifier, start, start + 2, str(audio)),
        )
        row = self.conn.execute('SELECT * FROM segments WHERE id=?', (identifier,)).fetchone()
        transcriber = Transcriber(self.cfg, threading.Event())
        transcriber.on_utterance = self.fast.offer
        with patch.object(transcriber, 'transcribe_path', return_value={
            'text': text, 'segments': [{'no_speech_prob': 0, 'avg_logprob': -.1}],
        }):
            transcriber.process(self.conn, row)
        return self.conn.execute('SELECT * FROM segments WHERE id=?', (identifier,)).fetchone()

    def classify(self, item, accept):
        with patch.object(self.fast, 'run_codex', return_value={'accept': accept}) as model:
            self.fast.respond_candidate(self.conn, item, threading.Event())
        return model

    def test_observed_question_reaches_exact_native_owner_without_new_worker(self):
        self.transcribe(QUESTION)
        item = self.fast.candidates.get_nowait()
        self.assertEqual(item['owner']['session_id'], 'owner')
        self.assertEqual(self.fast.request_until, 0)
        self.assertTrue(self.fast.q.empty())
        with patch.object(self.fast.cues, 'emit') as cue:
            model = self.classify(item, True)
        cue.assert_not_called()
        self.assertIn(QUESTION, model.call_args.args[0])
        self.assertEqual(model.call_args.kwargs['schema'], FOLLOWUP_SCHEMA)
        self.assertTrue(model.call_args.kwargs['read_only'])
        self.assertEqual(model.call_args.kwargs['request_id'], 'candidate')
        self.assertTrue(callable(model.call_args.kwargs['cancelled']))
        self.sessions.tick()
        self.backend.create.assert_not_called()
        submitted = [call for call in self.backend.command.call_args_list if call.args[2] == 'submit']
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].args[1], 'native-owner')
        self.assertIn(QUESTION, submitted[0].kwargs['message'])
        claim = self.conn.execute('SELECT * FROM session_claims').fetchone()
        self.assertEqual((claim['status'], claim['session_id']), ('handled', 'owner'))
        # Replayed durable input cannot submit another command or rerun the model.
        self.classify(item, True).assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 1)

    def test_ambient_rejection_returns_to_triage_without_ack_or_execution(self):
        row = self.transcribe('The iPad is on the desk.')
        item = self.fast.candidates.get_nowait()
        thought = self.conn.execute('SELECT * FROM thoughts WHERE id=?', (row['thought_id'],)).fetchone()
        with patch.object(self.router, 'run_triage') as triage:
            self.router.triage(self.conn, thought)
        triage.assert_not_called()  # unresolved candidate is reserved
        self.classify(item, False)
        self.assertFalse(self.conn.execute('SELECT 1 FROM session_claims').fetchone())
        self.assertEqual(self.sessions.pending_inputs(), [])
        self.assertIn('iPad is on the desk', self.router.unclaimed_text(self.conn, thought))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)

    def test_topic_shift_keeps_recent_owner_and_cannot_reach_background_dispatch(self):
        question = ('Concretely, what decisions do I have to make for the presentation? '
                    'Is there anything that I have to do today?')
        self.conn.execute("UPDATE work_sessions SET name='Plan today',goal='Prepare a PR before planning'")
        self.conn.execute("UPDATE session_events SET text='You have an hour tomorrow to finish the PR.',created_at=?",
                          (self.now - 13,))
        row = self.transcribe(question)
        item = self.fast.candidates.get_nowait()
        self.assertEqual(item['owner']['seconds_since_conversation'], 13)
        self.assertEqual(item['owner']['followup_window_s'], 120)
        # Keep the captured timing and owner across restart, even if processing
        # occurs later. Admission is evaluated against when the user spoke.
        restored = Sessions(self.cfg, self.backend, SessionStore(self.home))
        self.fast.sessions = restored
        saved, = restored.pending_inputs()
        self.assertEqual(saved['owner'], item['owner'])
        with patch('ramble.fastlane.latency.event') as event:
            model = self.classify(saved, True)
        context = json.loads(model.call_args.args[0].split('\n\n')[-1])
        self.assertEqual(context['work']['seconds_since_conversation'], 13)
        self.assertEqual(context['utterance'], question)
        event.assert_called_with('work_followup_decision', segment_id='candidate',
                                 session_id='owner', accepted=True, seconds_since_conversation=13)
        thought = self.conn.execute('SELECT * FROM thoughts WHERE id=?', (row['thought_id'],)).fetchone()
        with patch.object(self.router, 'run_triage') as triage:
            self.router.triage(self.conn, thought)
        triage.assert_not_called()
        restored.tick()
        self.backend.create.assert_not_called()
        submitted = [call for call in self.backend.command.call_args_list if call.args[2] == 'submit']
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].args[1], 'native-owner')
        self.assertIn(question, submitted[0].kwargs['message'])

    def test_rate_limit_backoff_is_shared_with_direct_requests_and_holds_candidates(self):
        from ramble.codex_runner import RateLimitedError
        self.transcribe(QUESTION)
        item = self.fast.candidates.get_nowait()
        with patch.object(self.fast, 'run_codex', side_effect=RateLimitedError(496)), self.assertLogs('ramble.fast', level='ERROR'):
            self.fast.respond_candidate(self.conn, item, threading.Event())
        self.assertGreater(self.fast.unavailable_until, time.monotonic() + 490)
        self.transcribe(QUESTION, 'second')
        pending = self.fast.candidates.get_nowait()
        self.classify(pending, True).assert_not_called()
        self.assertEqual(self.fast.candidates.get_nowait()['seg']['id'], 'second')
        self.assertEqual(self.conn.execute("SELECT status FROM session_claims WHERE segment_id='second'").fetchone()[0], 'candidate')
        with patch.object(self.fast, 'run_codex') as model:
            self.fast.respond(self.conn, [{'seg': {'id': 'direct', 'started_at': self.now, 'ended_at': self.now}, 'text': 'Ramble, hello'}])
        model.assert_not_called()
        self.assertIn('rate limited', self.fast.history[-1][2])

    def test_window_uses_conversation_not_heartbeat_and_does_not_slide_on_ambient(self):
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 121,))
        self.transcribe(QUESTION)
        self.assertTrue(self.fast.candidates.empty())
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 119,))
        self.transcribe('The iPad is on the desk.', 'ambient')
        self.classify(self.fast.candidates.get_nowait(), False)
        self.transcribe(QUESTION, 'late', self.now + 3)
        self.assertTrue(self.fast.candidates.empty())

    def test_recovered_history_does_not_open_window(self):
        self.conn.execute('UPDATE session_events SET payload=\'{"recovered":true}\'')
        self.transcribe(QUESTION)
        self.assertTrue(self.fast.candidates.empty())

    def test_disabled_disconnected_quiet_and_stopped_owners_do_not_admit(self):
        for changes in ('connected=0', 'quiet=1', "status='cancelled'", 'foreground=0'):
            with self.subTest(changes=changes):
                self.conn.execute("UPDATE work_sessions SET connected=1,quiet=0,status='idle',foreground=1")
                self.conn.execute('UPDATE work_sessions SET ' + changes)
                self.assertIsNone(self.sessions.followup_owner(self.conn, self.now))
        self.cfg['work_followup_s'] = 0
        self.assertIsNone(self.sessions.followup_owner(self.conn, self.now))

    def test_stop_quiet_and_focus_change_during_classification_prevent_routing(self):
        for change in ('epoch=epoch+1', 'speech_epoch=speech_epoch+1', 'foreground=0'):
            with self.subTest(change=change):
                self.conn.execute('UPDATE work_sessions SET foreground=1')
                self.transcribe(QUESTION, change)
                item = self.fast.candidates.get_nowait()
                self.conn.execute('UPDATE work_sessions SET ' + change)
                with self.assertLogs('ramble.fast', level='ERROR'):
                    self.classify(item, True)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)

    def test_restart_restores_pinned_candidate_and_handled_claim_stays_excluded(self):
        row = self.transcribe(QUESTION)
        restored = Sessions(self.cfg, self.backend, SessionStore(self.home))
        self.fast.sessions = restored
        item, = restored.pending_inputs()
        self.classify(item, True)
        thought = self.conn.execute('SELECT * FROM thoughts WHERE id=?', (row['thought_id'],)).fetchone()
        with patch.object(self.router, 'run_triage') as triage:
            self.router.triage(self.conn, thought)
        triage.assert_not_called()
        self.assertEqual(restored.pending_inputs(), [])

    def test_stale_restart_failure_and_pause_release_without_speech_or_execution(self):
        for outcome in ('expired', 'failure', 'pause'):
            with self.subTest(outcome=outcome):
                self.transcribe(QUESTION, outcome)
                item = self.fast.candidates.get_nowait()
                if outcome == 'expired':
                    item['owner']['expires_at'] = self.now - 1
                model = Mock(side_effect=RuntimeError('offline'))
                with patch.object(self.fast, 'run_codex', model), \
                     patch('ramble.fastlane.state.activity_allowed', return_value=outcome != 'pause'):
                    if outcome == 'failure':
                        with self.assertLogs('ramble.fast', level='ERROR'):
                            self.fast.respond_candidate(self.conn, item, threading.Event())
                    else:
                        self.fast.respond_candidate(self.conn, item, threading.Event())
                        model.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)
        self.assertEqual(self.sessions.pending_inputs(), [])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_claims').fetchone()[0], 0)

    def test_claimed_speech_and_candidates_do_not_extend_background_quiet(self):
        ambient = self.transcribe('I should consider a better desk layout.', 'ambient', self.now - 100)
        self.transcribe('Ramble, can you look at this?', 'addressed', self.now - 40)
        self.transcribe(QUESTION, 'candidate', self.now - 20)
        with patch('ramble.router.state.is_listening', return_value=True):
            self.router.close_stale(self.conn)
        thought = self.conn.execute('SELECT * FROM thoughts WHERE id=?', (ambient['thought_id'],)).fetchone()
        self.assertEqual(thought['status'], 'closed')
        self.assertEqual(thought['ended_at'], self.now - 98)
        self.assertEqual(len({r[0] for r in self.conn.execute('SELECT thought_id FROM segments')}), 3)
        self.assertIn('desk layout', self.router.unclaimed_text(self.conn, thought))

    def test_unclaimed_speech_keeps_its_own_quiet_period(self):
        self.cfg['work_followup_s'] = 0
        first = self.transcribe('A better desk layout might help.', 'one', self.now - 100)
        self.transcribe('I should move the monitor.', 'two', self.now - 30)
        with patch('ramble.router.state.is_listening', return_value=True):
            self.router.close_stale(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM thoughts WHERE id=?', (first['thought_id'],)).fetchone()[0], 'open')

    def test_legacy_mixed_thought_ignores_claimed_end_time(self):
        ambient = self.transcribe('A better desk layout might help.', 'ambient', self.now - 100)
        owned = self.transcribe('Ramble, investigate this.', 'owned', self.now - 10)
        self.conn.execute('UPDATE segments SET thought_id=? WHERE id=?', (ambient['thought_id'], owned['id']))
        self.conn.execute('UPDATE thoughts SET ended_at=? WHERE id=?', (self.now - 8, ambient['thought_id']))
        with patch('ramble.router.state.is_listening', return_value=True):
            self.router.close_stale(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM thoughts WHERE id=?', (ambient['thought_id'],)).fetchone()[0], 'closed')

    def test_later_transcription_backlog_cannot_starve_background(self):
        row = self.transcribe('A better desk layout might help.', 'ambient', self.now - 200)
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES ('pending',?,?,1,'fake')",
                          (self.now - 2, self.now - 1))
        with patch('ramble.router.state.is_listening', return_value=True):
            self.router.close_stale(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM thoughts WHERE id=?', (row['thought_id'],)).fetchone()[0], 'closed')

    def test_nearby_pending_transcription_still_holds_thought_open(self):
        row = self.transcribe('A better desk layout might help.', 'ambient', self.now - 100)
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path) VALUES ('pending',?,?,1,'fake')",
                          (self.now - 20, self.now - 19))
        with patch('ramble.router.state.is_listening', return_value=True):
            self.router.close_stale(self.conn)
        self.assertEqual(self.conn.execute('SELECT status FROM thoughts WHERE id=?', (row['thought_id'],)).fetchone()[0], 'open')


class DirectRoutingTests(unittest.TestCase):
    setUp = WorkFollowupTests.setUp
    transcribe = WorkFollowupTests.transcribe

    def respond(self, items):
        with patch.object(self.fast, 'run_codex', side_effect=AssertionError('No routing model')) as model:
            self.fast.respond(self.conn, items, threading.Event())
        model.assert_not_called()

    def test_direct_followup_keeps_thinking_feedback_until_owner_finishes(self):
        self.transcribe('Ramble, can you try that again?')
        item = self.fast.q.get_nowait()
        cues = self.fast.cues
        cues.cfg = dict(self.cfg, fast_speak=True, audio_cues_enabled=True)
        cues.session_work = self.sessions.thinking_work
        with patch('ramble.cues.time.monotonic', return_value=100) as clock:
            self.respond([item])
            self.assertIsNone(cues.work_due())
            clock.return_value = 102
            work = cues.work_due()
            self.assertIsNotNone(work)
            self.backend.snapshot.return_value.update(status='running', active_turn_id='turn')
            self.sessions.tick()
            clock.return_value = 162
            self.assertEqual(cues.work_due(), work)
            self.backend.snapshot.return_value.update(status='idle', active_turn_id=None)
            self.sessions.tick()
            self.assertFalse(cues.work_active(work))
            clock.return_value = 165
            self.assertIsNone(cues.work_due())

    def test_addressed_question_bypasses_model_cooldown_and_acknowledgement(self):
        self.cfg['work_followup_s'] = 3600
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 1800,))
        row = self.transcribe('Ramble, just to confirm, is there a deadline to decide what to build?')
        item = self.fast.q.get_nowait()
        self.assertEqual(item['direct_owner']['session_id'], 'owner')
        self.fast.unavailable_until = time.monotonic() + 500
        self.sessions.speech = Mock()
        self.respond([item])
        self.sessions.speech.enqueue.assert_not_called()
        thought = self.conn.execute('SELECT * FROM thoughts WHERE id=?', (row['thought_id'],)).fetchone()
        with patch.object(self.router, 'run_triage') as triage:
            self.router.triage(self.conn, thought)
        triage.assert_not_called()
        self.sessions.tick()
        self.backend.create.assert_not_called()
        submits = [c for c in self.backend.command.call_args_list if c.args[2] == 'submit']
        self.assertEqual(len(submits), 1)
        self.assertEqual(submits[0].args[1], 'native-owner')
        self.assertIn('is there a deadline', submits[0].kwargs['message'])
        self.respond([item])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 1)

    def test_one_hour_boundary_falls_back_and_can_reuse_same_session(self):
        self.cfg['work_followup_s'] = 3600
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 3599,))
        self.assertIsNotNone(self.sessions.followup_owner(self.conn, self.now))
        self.assertIsNone(self.sessions.followup_owner(self.conn, self.now + 2))
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 3601,))
        self.transcribe('Ramble, can we continue discussing the deadline?')
        item = self.fast.q.get_nowait()
        self.assertNotIn('direct_owner', item)
        with patch.object(self.fast, 'run_codex', return_value={
            'reply': 'Continue', 'handoff': False,
            'route': {'action': 'continue_session', 'session_id': 'owner'},
        }) as model:
            self.fast.respond(self.conn, [item], threading.Event())
        model.assert_called_once()
        command = self.conn.execute('SELECT * FROM session_commands').fetchone()
        self.assertEqual(command['session_id'], 'owner')
        self.backend.create.assert_not_called()

    def test_owner_and_pinned_input_survive_restart_and_user_input_renews_window(self):
        self.cfg['work_followup_s'] = 3600
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 3590,))
        self.transcribe('Ramble, what should I build?')
        self.sessions = Sessions(self.cfg, self.backend, SessionStore(self.home))
        self.fast = FastLane(self.cfg)
        self.fast.sessions = self.sessions
        items = self.sessions.pending_inputs()
        self.assertEqual(items[0]['direct_owner']['session_id'], 'owner')
        self.respond(items)
        self.assertIsNotNone(self.sessions.followup_owner(self.conn, self.now + 30))
        self.assertEqual(self.sessions.pending_inputs(), [])

    def test_progress_heartbeat_recovery_and_failed_playback_do_not_renew(self):
        self.cfg['work_followup_s'] = 3600
        self.conn.execute('UPDATE session_events SET created_at=?', (self.now - 3601,))
        for payload in ({'phase': 'commentary'}, {'phase': 'final_answer', 'recovered': True}):
            self.store.event(self.conn, 'owner', str(payload), 'message', 'Background update', payload)
        self.conn.execute('UPDATE work_sessions SET updated_at=?', (self.now,))
        self.assertIsNone(self.sessions.followup_owner(self.conn, self.now))
        self.conn.execute("INSERT INTO session_speech(id,session_id,text,kind,priority,epoch,status,created_at,expires_at,finished_at) "
                          "VALUES ('result','owner','Answer','result',1,0,'failed',?,?,?)",
                          (self.now - 200, self.now + 200, self.now - 20))
        self.assertIsNone(self.sessions.followup_owner(self.conn, self.now))
        self.conn.execute("UPDATE session_speech SET status='played',kind='progress'")
        self.assertIsNone(self.sessions.followup_owner(self.conn, self.now))
        self.conn.execute("UPDATE session_speech SET kind='result'")
        self.assertEqual(self.sessions.followup_owner(self.conn, self.now)['seconds_since_conversation'], 20)

    def test_controls_interrupt_even_inside_transcription_transaction_and_model_cooldown(self):
        self.transcribe('Ramble, what should I build?')
        item = self.fast.q.get_nowait()
        self.fast.unavailable_until = time.monotonic() + 500
        self.transcribe('Ramble, stop working.', 'stop')
        self.assertEqual(self.store.get('owner')['status'], 'stopping')
        self.assertTrue(self.fast.q.empty())
        with self.assertLogs('ramble.fast', level='WARNING'):
            self.respond([item])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM session_commands WHERE action='message'").fetchone()[0], 0)
        self.assertIsNotNone(self.conn.execute("SELECT error FROM session_inputs WHERE segment_id='candidate'").fetchone()[0])

    def test_focus_change_never_redirects_pinned_input(self):
        self.transcribe('Ramble, what should I build?')
        item = self.fast.q.get_nowait()
        self.conn.execute('UPDATE work_sessions SET foreground=0')
        with self.assertLogs('ramble.fast', level='WARNING'):
            self.respond([item])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)

    def test_direct_input_expires_without_reclassification_or_redispatch(self):
        self.transcribe('Ramble, what should I build?')
        item = self.fast.q.get_nowait()
        item['direct_owner']['expires_at'] = self.now - 1
        with self.assertLogs('ramble.fast', level='WARNING'):
            self.respond([item])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM session_inputs').fetchone()[0], 'failed')

    def test_quoted_controls_do_not_execute_locally(self):
        self.transcribe('Ramble, explain what "stop working" means.')
        self.assertEqual(self.store.get('owner')['status'], 'idle')
        self.assertEqual(self.fast.q.get_nowait()['direct_owner']['session_id'], 'owner')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)

    def test_observed_agent_discussion_stays_direct_without_model_or_ack(self):
        self.sessions.speech = Mock()
        utterances = (
            'Ramble, did you get my instructions about launching app agents?',
            'Ramble, okay, can you just respond to me first before you do any work? '
            'I asked you to show me the prompt in the companion app before you launch the agents. '
            'Can you do that now? Just show me the prompt first.',
        )
        for n, text in enumerate(utterances):
            self.transcribe(text, 'observed-' + str(n))
            item = self.fast.q.get_nowait()
            self.assertEqual(item['direct_owner']['session_id'], 'owner')
            self.respond([item])
        self.sessions.speech.enqueue.assert_not_called()
        commands = self.conn.execute("SELECT session_id,payload FROM session_commands WHERE action='message' ORDER BY created_at").fetchall()
        self.assertEqual([c['session_id'] for c in commands], ['owner', 'owner'])
        self.assertEqual([json.loads(c['payload'])['message'] for c in commands], list(utterances))
        self.backend.create.assert_not_called()

    def test_explicit_routing_and_adapter_requests_keep_model_path(self):
        for n, text in enumerate(('Ramble, remind me to call tomorrow.',
                                  'Ramble, change my time entry.',
                                  'Ramble, switch to the presentation session.',
                                  'Ramble, have an agent investigate this.',
                                  'Ramble, move this to another conversation.')):
            self.transcribe(text, str(n))
            self.assertNotIn('direct_owner', self.fast.q.get_nowait())

    def test_later_coalesced_switch_overrides_initial_pin(self):
        self.transcribe('Ramble, help me plan the day.')
        first = self.fast.q.get_nowait()
        self.transcribe('Actually, put this in a separate session.', 'switch')
        second = self.fast.q.get_nowait()
        with patch.object(self.fast, 'run_codex', return_value={'reply': 'Which session?', 'handoff': False}) as model:
            self.fast.respond(self.conn, [first, second], threading.Event())
        model.assert_called_once()
        self.assertNotIn('direct_owner', first)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
