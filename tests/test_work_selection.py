"""Model/effort selection for work sessions; fake native owner, no agents, mic or audio."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ramble import config, model_settings
from ramble.sessions import ROUTE_SCHEMA, Sessions, TURN_CHANGED
from ramble.session_store import SessionStore

CATALOG = [
    {'id': 'quick-model', 'label': 'Quick', 'efforts': ['low', 'medium', 'high'],
     'default_effort': 'low', 'is_default': True},
    {'id': 'deep-model', 'label': 'Deep', 'efforts': ['low', 'medium', 'high', 'xhigh'],
     'default_effort': 'medium', 'is_default': False},
]


class FakeBackend:
    """Mirrors the Agent Coord receipts Ramble relies on, including idle-only settings."""

    def __init__(self):
        self.calls = []
        self.receipts = {}
        self.applied = {'model': None, 'effort': None}
        self.reject_turn = 0
        self.settings_status = 'completed'
        self.live = {'connected': True, 'status': 'idle', 'active_turn_id': None,
                     'last_turn_id': None, 'events': [], 'cursor': 0}

    def create(self, key, payload):
        self.calls.append(('create', key, payload))
        self.applied = {'model': payload['model'], 'effort': payload['effort']}
        self.receipts[key] = {'status': 'completed', 'thread_id': 'thread-1', 'result': {}}
        return self.receipts[key]

    def command(self, key, thread, action, **kw):
        self.calls.append((action, key, kw))
        if action == 'submit' and self.reject_turn:
            self.reject_turn -= 1
            # The ownership guard rejects before provider submission.
            self.live.update(status='running', active_turn_id='turn-new')
            self.receipts[key] = {'status': 'failed', 'request_id': key, 'result': None,
                                  'error': TURN_CHANGED + '. Refresh before submitting another command.'}
            return self.receipts[key]
        if action == 'submit':
            self.live.update(status='running', active_turn_id=self.live['active_turn_id'] or 'turn-2')
        self.receipts[key] = {'status': 'completed', 'thread_id': thread, 'result': {'accepted': True}}
        return self.receipts[key]

    def change_settings(self, key, thread, model, effort):
        self.calls.append(('settings', key, {'model': model, 'effort': effort}))
        if self.settings_status == 'completed':
            self.applied = {'model': model, 'effort': effort}
        self.receipts[key] = {'status': self.settings_status, 'thread_id': thread, 'result': {}}
        return self.receipts[key]

    def settings(self, thread):
        return {**self.applied, 'permissions': {'pending_changes': []}}

    def status(self, key):
        return self.receipts[key]

    def cancel(self, key):
        return self.receipts[key]

    def snapshot(self, thread, after):
        return {**self.live, 'events': []}


class WorkSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-selection-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for name, path in {'HOME': self.home, 'DB_PATH': self.home / 'ramble.db',
                           'STATE_PATH': self.home / 'state.json',
                           'TRIAGE_DIR': self.home / 'triage', 'DIGEST_DIR': self.home / 'digest'}.items():
            self.enterContext(patch.object(config, name, path))
        config.ensure_dirs()
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=5, fast_speak=False, speak_questions=False,
                        audio_cues_enabled=False, work_session_root=str(self.home),
                        fast_model='quick-model', fast_effort='low',
                        work_session_model='deep-model', work_session_effort='xhigh')
        (self.home / 'config.json').write_text(json.dumps(self.cfg))
        self.catalog = self.enterContext(patch.object(model_settings, 'known_catalog', return_value=CATALOG))
        self.enterContext(patch.object(model_settings, 'catalog', return_value=CATALOG))
        self.limits = self.enterContext(patch.object(model_settings, 'known_limits', return_value=None))
        self.store = SessionStore(self.home)
        self.backend = FakeBackend()
        self.sessions = Sessions(self.cfg, self.backend, self.store)

    def items(self, identifier):
        now = time.time()
        return [{'seg': {'id': identifier, 'started_at': now - 2, 'ended_at': now - 1},
                 'text': 'request ' + identifier, 'why': 'named'}]

    def start(self, workload, **route):
        sid, _ = self.sessions.route({'action': 'work', 'cwd': str(self.home), 'name': 'Talk',
                                      'workload': workload, **route}, 'Can we talk?', self.items('start'))
        self.sessions.tick()
        return sid

    def actions(self):
        return [c[0] for c in self.backend.calls if c[0] != 'watch']

    def created(self):
        return [c[2] for c in self.backend.calls if c[0] == 'create'][-1]

    def events(self, sid, kind=None):
        with self.store.connect() as c:
            return [dict(r) for r in c.execute(
                'SELECT * FROM session_events WHERE session_id=? ORDER BY seq', (sid,))
                if kind is None or r['kind'] == kind]

    # ---- creation -----------------------------------------------------------
    def test_simple_conversation_uses_low_latency_pair_and_records_decision(self):
        sid = self.start('conversation')
        payload = self.created()
        self.assertEqual((payload['model'], payload['effort']), ('quick-model', 'low'))
        self.assertEqual(payload['selection']['basis'], 'conversation')
        self.assertTrue(payload['selection']['validated'])
        applied = [e for e in self.events(sid, 'status') if 'settings-applied' in e['event_key']]
        self.assertEqual(json.loads(applied[0]['payload'])['applied'], {'model': 'quick-model', 'effort': 'low'})

    def test_substantive_work_uses_saved_work_pair(self):
        self.start('work')
        payload = self.created()
        self.assertEqual((payload['model'], payload['effort']), ('deep-model', 'xhigh'))
        self.assertEqual(payload['selection']['basis'], 'work')

    def test_missing_workload_keeps_saved_work_pair(self):
        self.start(None)
        self.assertEqual((self.created()['model'], self.created()['effort']), ('deep-model', 'xhigh'))

    def test_configured_conversation_pair_overrides_quick_reply_pair(self):
        (self.home / 'config.json').write_text(json.dumps({
            **self.cfg, 'work_conversation_model': 'deep-model', 'work_conversation_effort': 'low'}))
        self.start('conversation')
        self.assertEqual((self.created()['model'], self.created()['effort']), ('deep-model', 'low'))

    def test_explicit_user_choice_overrides_workload(self):
        self.start('conversation', model='deep-model', effort='high')
        payload = self.created()
        self.assertEqual((payload['model'], payload['effort']), ('deep-model', 'high'))
        self.assertEqual(payload['selection']['basis'], 'explicit')

    def test_explicit_unsupported_pair_is_rejected_not_substituted(self):
        with self.assertRaisesRegex(ValueError, 'not supported'):
            self.sessions.route({'action': 'work', 'workload': 'conversation', 'model': 'quick-model',
                                 'effort': 'xhigh'}, 'Use quick xhigh', self.items('bad'))
        self.assertEqual(self.store.sessions(), [])

    def test_unsupported_policy_effort_uses_model_default_with_visible_fallback(self):
        self.cfg['fast_effort'] = 'ultra'
        (self.home / 'config.json').write_text(json.dumps(self.cfg))
        self.start('conversation')
        payload = self.created()
        self.assertEqual((payload['model'], payload['effort']), ('quick-model', 'low'))
        self.assertIn('unsupported', payload['selection']['fallback'])

    def test_unadvertised_conversation_model_falls_back_to_work_pair(self):
        self.cfg['fast_model'] = 'retired-model'
        (self.home / 'config.json').write_text(json.dumps(self.cfg))
        self.start('conversation')
        payload = self.created()
        self.assertEqual((payload['model'], payload['effort']), ('deep-model', 'xhigh'))
        self.assertEqual(payload['selection']['basis'], 'work')
        self.assertIn('not advertised', payload['selection']['fallback'])

    def test_missing_discovery_proceeds_unvalidated_without_blocking(self):
        self.catalog.return_value = None
        self.start('conversation')
        payload = self.created()
        self.assertEqual((payload['model'], payload['effort']), ('quick-model', 'low'))
        self.assertFalse(payload['selection']['validated'])

    def test_exhausted_provider_limit_starts_no_session(self):
        self.limits.return_value = {'windows': [{'label': 'Weekly', 'used_percent': 100,
                                                 'resets_at': time.time() + 3600}]}
        with self.assertRaisesRegex(ValueError, 'Weekly limit is used up'):
            self.sessions.route({'action': 'work', 'workload': 'conversation'}, 'Talk', self.items('limit'))
        self.assertEqual(self.store.sessions(), [])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM session_launches').fetchone()[0], 0)

    def test_expired_limit_window_does_not_block(self):
        self.limits.return_value = {'windows': [{'used_percent': 100, 'resets_at': time.time() - 5}]}
        self.start('conversation')
        self.assertEqual(len([c for c in self.backend.calls if c[0] == 'create']), 1)

    def test_applied_mismatch_is_reported_not_silent(self):
        self.backend.create = lambda key, payload: (
            self.backend.calls.append(('create', key, payload)),
            self.backend.applied.update(model='quick-model', effort='medium'),
            {'status': 'completed', 'thread_id': 'thread-1', 'result': {}})[-1]
        sid = self.start('conversation')
        errors = self.events(sid, 'error')
        self.assertEqual(len(errors), 1)
        self.assertIn('provider reports quick-model medium', errors[0]['text'])

    def test_route_schema_stays_strict(self):
        self.assertEqual(set(ROUTE_SCHEMA['properties']), set(ROUTE_SCHEMA['required']))
        self.assertFalse(ROUTE_SCHEMA['additionalProperties'])
        self.assertIn(None, ROUTE_SCHEMA['properties']['workload']['enum'])

    # ---- reused sessions --------------------------------------------------
    def continue_(self, sid, identifier, **route):
        self.sessions.route({'action': 'continue_session', 'session_id': sid, **route},
                            'follow-up ' + identifier, self.items(identifier))

    def test_conversation_escalates_before_substantive_turn_and_only_once(self):
        sid = self.start('conversation')
        self.continue_(sid, 'build', workload='work')
        self.sessions.tick()
        self.assertEqual(self.actions()[-2:], ['settings', 'submit'])
        change = [c for c in self.backend.calls if c[0] == 'settings'][0]
        self.assertEqual(change[2], {'model': 'deep-model', 'effort': 'xhigh'})
        self.assertEqual(self.backend.applied, {'model': 'deep-model', 'effort': 'xhigh'})
        # Later substantive follow-ups keep the escalated pair without another change.
        self.backend.live.update(status='idle', active_turn_id=None)
        self.continue_(sid, 'more', workload='work')
        self.sessions.tick()
        self.assertEqual([c[0] for c in self.backend.calls].count('settings'), 1)

    def test_conversation_followup_keeps_low_latency_pair(self):
        sid = self.start('conversation')
        self.continue_(sid, 'chat', workload='conversation')
        self.sessions.tick()
        self.assertNotIn('settings', [c[0] for c in self.backend.calls])

    def test_work_session_is_not_downgraded_by_conversation(self):
        sid = self.start('work')
        self.continue_(sid, 'chat', workload='conversation')
        self.sessions.tick()
        self.assertNotIn('settings', [c[0] for c in self.backend.calls])

    def test_explicit_effort_change_keeps_session_model(self):
        sid = self.start('conversation')
        self.continue_(sid, 'effort', effort='high')
        self.sessions.tick()
        change = [c for c in self.backend.calls if c[0] == 'settings'][0][2]
        self.assertEqual(change, {'model': 'quick-model', 'effort': 'high'})

    def test_explicit_unsupported_change_is_rejected_without_message(self):
        sid = self.start('conversation')
        with self.assertRaisesRegex(ValueError, 'not supported'):
            self.continue_(sid, 'bad', model='quick-model', effort='xhigh')
        with self.store.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM session_commands WHERE action IN ('message','settings')")
                             .fetchone()[0], 0)

    def test_message_waits_briefly_for_idle_settings_change(self):
        sid = self.start('conversation')
        self.backend.settings_status = 'queued'  # Agent Coord applies settings only while idle.
        self.continue_(sid, 'build', workload='work')
        self.sessions.tick()
        self.assertEqual(self.actions()[-1], 'settings')
        self.backend.receipts[[c for c in self.backend.calls if c[0] == 'settings'][0][1]]['status'] = 'completed'
        self.backend.applied = {'model': 'deep-model', 'effort': 'xhigh'}
        self.sessions.tick()
        self.assertEqual(self.actions()[-1], 'submit')

    def test_running_turn_is_steered_without_waiting_for_settings(self):
        sid = self.start('conversation')
        self.backend.settings_status = 'queued'
        self.backend.live.update(status='running', active_turn_id='turn-9')
        self.continue_(sid, 'build', workload='work')
        self.sessions.tick()
        submit = [c for c in self.backend.calls if c[0] == 'submit'][0]
        self.assertEqual(submit[2]['expected'], 'turn-9')

    def test_existing_adopted_session_is_never_changed_implicitly(self):
        sid = self.start('conversation')
        with self.store.connect() as c:
            c.execute("UPDATE work_sessions SET kind='existing' WHERE id=?", (sid,))
        self.continue_(sid, 'build', workload='work')
        self.sessions.tick()
        self.assertNotIn('settings', [c[0] for c in self.backend.calls])

    # ---- named direct follow-ups ----------------------------------------
    def direct(self, sid, identifier, text):
        from ramble.fastlane import FastLane
        fast = FastLane(self.cfg)
        fast.sessions = self.sessions
        items = self.items(identifier)
        now = time.time()
        items[0].update(text=text, seg={'id': identifier, 'started_at': now, 'ended_at': now + 1})
        with self.store.connect() as c:
            owner = self.sessions.followup_owner(c, items[0]['seg']['started_at'])
            self.assertEqual(owner['session_id'], sid)
            items[0]['direct_owner'] = owner
            self.sessions.accept_input(c, items[0])
            with patch.object(fast, 'run_codex', side_effect=AssertionError('no model call')):
                fast.respond(c, items)
        self.sessions.tick()

    def test_named_substantive_followup_escalates_without_model_call(self):
        sid = self.start('conversation')
        self.direct(sid, 'named-build', 'Ramble, okay, go ahead and implement that in the repo.')
        self.assertEqual(self.actions()[-2:], ['settings', 'submit'])
        self.assertEqual(self.backend.applied, {'model': 'deep-model', 'effort': 'xhigh'})

    def test_named_acknowledgement_keeps_conversation_pair(self):
        sid = self.start('conversation')
        self.direct(sid, 'named-ack', 'Ramble, thanks, that name works. How would you build it?')
        self.assertNotIn('settings', self.actions())
        self.assertEqual(self.actions()[-1], 'submit')

    def test_named_substantive_followup_preserves_explicit_choice(self):
        sid = self.start('conversation', model='quick-model', effort='high')
        self.direct(sid, 'named-fix', 'Ramble, fix the failing tests.')
        self.assertNotIn('settings', self.actions())
        self.assertEqual(self.backend.applied, {'model': 'quick-model', 'effort': 'high'})

    def test_work_hint_is_local_and_conservative(self):
        from ramble.direct_routing import workload_hint
        self.assertEqual(workload_hint('Ramble, can you debug why the recorder drops audio?'), 'work')
        self.assertEqual(workload_hint("Ramble, let's write a spec for it"), 'work')
        for text in ('Ramble, thanks.', 'Ramble, what would you call it?', 'Ramble, how would you build it?'):
            self.assertIsNone(workload_hint(text))

    # ---- split utterances --------------------------------------------------
    def test_turn_change_race_resubmits_with_fresh_turn_and_new_request_key(self):
        sid = self.start('conversation')
        self.backend.reject_turn = 1
        self.continue_(sid, 'fragment', workload='conversation')
        self.sessions.tick()
        self.sessions.tick()
        submits = [c for c in self.backend.calls if c[0] == 'submit']
        self.assertEqual(len(submits), 2)
        self.assertTrue(submits[1][1].endswith('-retry1'))
        self.assertEqual(submits[1][2]['expected'], 'turn-new')  # steers the turn that won the race
        with self.store.connect() as c:
            row = c.execute("SELECT status FROM session_commands WHERE action='message'").fetchone()
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(self.events(sid, 'error'), [])

    def test_turn_change_retries_are_bounded_and_reported(self):
        sid = self.start('conversation')
        self.backend.reject_turn = 10
        self.continue_(sid, 'fragment', workload='conversation')
        for _ in range(6):
            self.sessions.tick()
        self.assertEqual(len([c for c in self.backend.calls if c[0] == 'submit']), 3)
        self.assertEqual(len(self.events(sid, 'error')), 1)

    def test_turn_starting_without_id_waits_instead_of_racing(self):
        sid = self.start('conversation')
        self.backend.live.update(status='running', active_turn_id=None)
        self.continue_(sid, 'fragment', workload='conversation')
        self.sessions.tick()
        self.assertNotIn('submit', [c[0] for c in self.backend.calls])


class FollowupSelectionTests(unittest.TestCase):
    def test_admission_uses_low_effort_on_quick_model(self):
        cfg = dict(config.DEFAULTS, fast_model='quick-model', fast_effort='medium')
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp, patch.object(config, 'HOME', Path(tmp)), \
                patch.object(model_settings, 'known_catalog', return_value=CATALOG):
            self.assertEqual(model_settings.followup_selection(cfg), {'model': 'quick-model', 'effort': 'low'})
            # An unsupported admission effort keeps the quick-reply effort.
            cfg['work_followup_effort'] = 'ultra'
            self.assertEqual(model_settings.followup_selection(cfg)['effort'], 'medium')

    def test_exhausted_window_ignores_partial_and_expired_windows(self):
        now = 1000
        self.assertIsNone(model_settings.exhausted_window({'windows': [{'used_percent': 99}]}, now))
        self.assertIsNone(model_settings.exhausted_window({'windows': [{'used_percent': 100, 'resets_at': 900}]}, now))
        self.assertIsNotNone(model_settings.exhausted_window({'windows': [{'used_percent': 100, 'resets_at': 1100}]}, now))
        self.assertIsNone(model_settings.exhausted_window(None, now))


if __name__ == '__main__':
    unittest.main()
