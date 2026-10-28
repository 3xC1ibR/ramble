"""Action routing regressions: no workers, microphone, playback or live API writes."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import action_routing, config, db, reminders, time_entries
from ramble.fastlane import FastLane, REPLY_SCHEMA
from ramble.router import Router
from ramble.sessions import Sessions
from ramble.session_store import SessionStore


class ActionRoutingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-actions-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        paths = dict(HOME=self.home, DB_PATH=self.home/'ramble.db', CONFIG_PATH=self.home/'config.json',
                     STATE_PATH=self.home/'state.json', TRIAGE_DIR=self.home/'triage',
                     DIGEST_DIR=self.home/'digest', LOG_DIR=self.home/'logs', AUDIO_DIR=self.home/'audio')
        self.enterContext(patch.multiple(config, **paths))
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False, speak_questions=False,
                        notify=False, fast_coalesce_s=0, audio_cues_enabled=False, audio_working_enabled=False)
        config.CONFIG_PATH.write_text(json.dumps(self.cfg))
        self.store = SessionStore(self.home)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.sessions = Sessions(self.cfg, backend=Mock(), store=self.store)
        self.fast = FastLane(self.cfg)
        self.fast.sessions = self.sessions
        self.enterContext(patch('ramble.fastlane.state.activity_allowed', return_value=True))
        self.enterContext(patch('ramble.fastlane.state.is_paused', return_value=False))
        self.enterContext(patch('ramble.speech.speak', side_effect=AssertionError('no speaker')))
        self.enterContext(patch('ramble.router.subprocess.run', side_effect=AssertionError('no agents')))
        now = time.time()
        self.conn.execute("INSERT INTO work_sessions(id,name,goal,cwd,kind,status,connected,created_at,updated_at) "
                          "VALUES ('s','Task','Discuss work',?,'interactive','idle',1,?,?)", (str(self.home),now,now))
        self.action = dict(operation='start', entry_id=None, workspace_id=22, project_id=4,
                           description='Example', clear_project=False)

    def item(self, text='Ramble, start a new Toggl timer for Example Engineering', ident='segment'):
        now = time.time()
        return {'seg': {'id': ident, 'started_at': now, 'ended_at': now}, 'text': text, 'why': 'named'}

    def answer(self, **changes):
        return dict(reply='Unverified model claim', handoff=False, route=None, reminder=None,
                    time_entry=self.action, resolution='action', **changes)

    def test_command_recognition_does_not_turn_discussion_or_quotes_into_actions(self):
        for text in ['Ramble start a new follow timer for Quizzle Engineering',
                     'Can you start a new Toggl timer?', 'Start tracking Example',
                     'Start, toggle, time tracking, start a new timer and toggle for Example Engineering']:
            self.assertEqual(action_routing.intent(text), ('time_entry', 'start'), text)
        for text in ['Fix timer routing', 'Explain how to start a timer', 'I should start a timer',
                     'He said “start a timer”', 'The timer is running', 'Add support for stopping timers']:
            self.assertIsNone(action_routing.intent(text), text)
        self.assertEqual(action_routing.intent('Stop my Toggl timer'), ('time_entry', 'stop'))

    def test_capabilities_and_strict_schema_are_shared_with_executor(self):
        self.assertEqual(list(action_routing.capabilities()['time_entry']),
                         time_entries.ACTION_SCHEMA['properties']['operation']['enum'])
        schema = action_routing.reply_schema(REPLY_SCHEMA, ('time_entry', 'start'))
        def strict(node):
            if isinstance(node, dict):
                if 'properties' in node:
                    self.assertEqual(set(node['required']), set(node['properties']))
                    self.assertFalse(node['additionalProperties'])
                for child in node.values(): strict(child)
            elif isinstance(node, list):
                for child in node: strict(child)
        strict(schema)
        self.assertEqual(schema['properties']['route'], {'type':'null'})

    def test_supported_action_cannot_be_handed_to_worker_by_model(self):
        bad = dict(reply='Ask Ramble again', route={'action':'delegate'}, handoff=True,
                   reminder=None, time_entry=None)
        with patch.object(self.fast, 'run_codex', return_value=bad) as model, \
                patch.object(time_entries.TimeEntries, 'execute') as execute:
            self.fast.respond(self.conn, [self.item()])
        execute.assert_not_called()
        self.sessions.backend.create.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_sessions').fetchone()[0], 1)
        self.assertEqual(action_routing.current(self.conn)['status'], 'failed')
        self.assertEqual(model.call_args.kwargs['schema']['properties']['route'], {'type':'null'})
        self.assertNotIn('Ask Ramble', action_routing.current(self.conn)['reply'])

    def test_unsupported_action_fails_without_model_or_worker(self):
        with patch.object(self.fast, 'run_codex') as model:
            self.fast.respond(self.conn, [self.item('Ramble, stop my Toggl timer')])
        model.assert_not_called()
        result = action_routing.current(self.conn)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('cannot stop', result['reply'])

    def test_missing_action_cannot_be_reported_as_success_or_repeat_request(self):
        for reply in ['Started your timer.', 'Tell Ramble to start it.', 'Could you repeat your request?']:
            answer = dict(reply=reply, handoff=False, route=None, reminder=None,
                          time_entry=None, resolution='needs_input')
            self.assertFalse(action_routing.validate_answer(answer, ('time_entry','start')))
        answer['reply'] = 'Which engineering project do you mean?'
        self.assertTrue(action_routing.validate_answer(answer, ('time_entry','start')))

    def test_lookup_failure_is_failed_instead_of_needing_user_input(self):
        answer = dict(reply="Stoic's workspace lookup failed.", handoff=False, route=None,
                      reminder=None, time_entry=None, resolution='failed')
        with patch.object(self.fast, 'run_codex', return_value=answer):
            self.fast.respond(self.conn,[self.item()])
        self.assertEqual(action_routing.current(self.conn)['status'],'failed')

    def test_confirmed_receipt_wins_over_model_prose_and_duplicate_delivery(self):
        item = self.item()
        with patch.object(self.fast, 'run_codex', return_value=self.answer()), \
                patch.object(time_entries.TimeEntries, 'execute', return_value={'reply':'Started Example.'}) as execute:
            self.fast.respond(self.conn, [item])
            self.fast.respond(self.conn, [item])
        execute.assert_called_once()
        self.assertEqual(action_routing.current(self.conn)['reply'], 'Started Example.')
        self.assertEqual(self.store.snapshot()['action']['status'], 'completed')

    def test_uncertain_write_and_restart_never_replay(self):
        item = self.item()
        with patch.object(self.fast, 'run_codex', return_value=self.answer()), \
                patch.object(time_entries.TimeEntries, 'execute', side_effect=reminders.UncertainError('Check Toggl.')) as execute:
            self.fast.respond(self.conn, [item])
            action_routing.recover_interrupted(self.conn)
            self.fast.respond(self.conn, [item])
        execute.assert_called_once()
        self.assertEqual(action_routing.current(self.conn)['status'], 'unconfirmed')
        self.assertIsNone(action_routing.pending(self.conn))

    def test_work_route_recovers_original_request_without_creating_worker(self):
        item = self.item()
        self.sessions.route({'action':'delegate'}, item['text'], [item])
        queued = action_routing.pending(self.conn)
        self.assertEqual(queued['id'], reminders.voice_key([item]))
        self.assertEqual(json.loads(queued['items'])[0]['seg']['id'], item['seg']['id'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM session_commands').fetchone()[0], 0)
        with patch.object(self.fast, 'run_codex', return_value=self.answer()), \
                patch.object(time_entries.TimeEntries, 'execute', return_value={'reply':'Started Example.'}) as execute:
            self.fast.respond(self.conn, json.loads(queued['items']))
        execute.assert_called_once()

    def test_companion_message_queues_same_executor_and_survives_restart(self):
        command = self.store.control('s', 'message', 'typed-1', message=self.item()['text'])
        command = dict(self.conn.execute('SELECT * FROM session_commands WHERE id=?', (command['id'],)).fetchone())
        self.sessions._process(command)
        queued = action_routing.pending(self.conn)
        self.assertEqual(queued['session_id'], 's')
        Sessions(self.cfg, backend=Mock(), store=SessionStore(self.home))._process(command)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM action_requests').fetchone()[0], 1)
        self.sessions.backend.submit.assert_not_called()
        action_routing.finish(self.conn, queued['id'], 'running', '')
        action_routing.recover_interrupted(self.conn)
        self.assertEqual(self.store.snapshot('s')['action']['status'], 'unconfirmed')
        self.assertIsNone(action_routing.pending(self.conn))

    def test_stop_cancels_queued_action(self):
        action_routing.enqueue(self.conn, [self.item()], session_id='s', epoch=0)
        self.store.control('s', 'stop_work', 'stop')
        self.assertEqual(self.store.snapshot('s')['action']['status'], 'cancelled')
        self.assertIsNone(action_routing.pending(self.conn))

    def test_named_triage_request_recovers_but_passive_or_old_speech_does_not(self):
        router = Router(self.cfg, threading.Event())
        for ident, text, age in [('named', self.item()['text'], 0),
                                  ('passive','Start a Toggl timer for Example',0),
                                  ('old',self.item()['text'],600)]:
            now = time.time()-age
            self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,status,text) VALUES (?,?,?,'closed',?)",
                              (ident,now,now,text))
            self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) "
                              "VALUES (?,?,?,1,'unused','transcribed',?,?)", (ident,now,now,text,ident))
            decision = {'classification':'actionable','summary':'Timer request','notify':None,'items':[
                {'kind':'task','text':'Start a Toggl timer for Example','action':'dispatch','question':None,
                 'dispatch':{'name':'timer','cwd':str(self.home),'prompt':text}}]}
            with patch.object(router.triage_filter,'decide',return_value=None), \
                    patch.object(router,'run_triage',return_value=decision), \
                    patch.object(router,'ask') as ask, patch.object(router,'dispatch') as dispatch:
                router.triage(self.conn,self.conn.execute('SELECT * FROM thoughts WHERE id=?',(ident,)).fetchone())
            dispatch.assert_not_called()
            if ident == 'named': ask.assert_not_called()
            else: self.assertEqual(ask.call_args.args[0], 'Do you want me to handle that timer request?')
        rows = self.conn.execute('SELECT items FROM action_requests').fetchall()
        self.assertEqual(len(rows),1)
        self.assertEqual(json.loads(rows[0][0])[0]['seg']['id'],'named')


if __name__ == '__main__': unittest.main()
