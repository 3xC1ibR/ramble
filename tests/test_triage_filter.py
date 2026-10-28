"""Whole-thought Jev filtering, with isolated storage and no live model/agent calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import config, db, state
from ramble.jev import JevError
from ramble.router import DECISION_SCHEMA, Router
from ramble.triage_filter import CATEGORIES, parse_assessment


def response(category='personal', probability=.98, confidence=.93, review=.02):
    return {'model': 'jev-1.13.0', 'answers': {
        'category': {'type': 'choice', 'choice': category, 'confidence': confidence,
                     'probabilities': {key: probability if key == category else (1-probability)/4
                                       for key in CATEGORIES}},
        'needs_review': {'type': 'noul', 'noul': review},
    }}


FULL_DECISION = {'classification': 'log', 'summary': 'Kept a useful thought.',
                 'items': [], 'notify': None}


class TriageFilterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-triage-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        paths = {'HOME': self.home, 'DB_PATH': self.home/'ramble.db',
                 'CONFIG_PATH': self.home/'config.json', 'STATE_PATH': self.home/'state.json'}
        paths.update({name: self.home/name.lower() for name in
                      ('AUDIO_DIR', 'DIGEST_DIR', 'LOG_DIR', 'TRIAGE_DIR')})
        self.enterContext(patch.multiple(config, **paths))
        self.enterContext(patch.dict(os.environ, {'JEV_API_KEY': 'test-only'}, clear=True))
        config.CONFIG_PATH.write_text(json.dumps({
            'dispatch_daily_cap': 0, 'fast_speak': False, 'speak_questions': False,
            'audio_cues_enabled': False, 'notify': False, 'jev_api_key_file': None,
        }))
        self.cfg = config.load()
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.router = Router(self.cfg, threading.Event())
        self.post = self.enterContext(patch.object(self.router.triage_filter.jev, '_post',
                                                  return_value=response()))
        self.full = self.enterContext(patch.object(self.router, 'run_triage', return_value=FULL_DECISION))
        self.child = self.enterContext(patch('ramble.router.subprocess.run',
                                            side_effect=AssertionError('must never spawn')))
        self.notify = self.enterContext(patch('ramble.router.notify'))
        self.ask = self.enterContext(patch.object(self.router, 'ask'))

    def thought(self, text='Good boy, Buddy. Sit. Have a treat.', tid='new', age=0):
        now = time.time() - age
        self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,status,text) VALUES (?,?,?,'closed',?)",
                          (tid, now, now, text))
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) "
                          "VALUES (?,?,?,1,?,'transcribed',?,?)",
                          (tid, now, now, str(self.home/(tid+'.flac')), text, tid))
        return self.row(tid)

    def row(self, tid='new'):
        return self.conn.execute('SELECT * FROM thoughts WHERE id=?', (tid,)).fetchone()

    def audit(self, tid='new'):
        return json.loads((config.TRIAGE_DIR/f'{tid}.jev.json').read_text())

    def claim(self, tid='new', status='done'):
        self.conn.execute('INSERT INTO session_claims VALUES (?,NULL,?)', (tid, status))

    def test_confident_passive_thoughts_skip_full_triage_and_keep_transcripts(self):
        for category in ('ambient', 'personal'):
            with self.subTest(category=category):
                self.post.return_value = response(category)
                thought = self.thought(tid=category)
                self.router.triage(self.conn, thought)
                row = self.row(category)
                self.assertEqual((row['status'], row['classification']), ('done', category))
                self.assertEqual(row['text'], thought['text'])
                self.assertEqual(self.conn.execute('SELECT text FROM segments WHERE id=?',
                                                  (category,)).fetchone()[0], thought['text'])
                decision = json.loads(row['triage_json'])
                self.assertEqual(set(decision), set(DECISION_SCHEMA['required']))
                self.assertEqual(decision['items'], [])
                self.assertIsNone(decision['notify'])
                saved = json.loads((config.TRIAGE_DIR/f'{category}.json').read_text())
                self.assertEqual(saved['backend'], 'jev')
                self.assertEqual(json.loads(saved['result']), decision)
                self.assertEqual(self.audit(category)['route'], 'skip_full_triage')
        self.full.assert_not_called()
        self.child.assert_not_called()
        self.notify.assert_not_called()
        self.ask.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM dispatches').fetchone()[0], 0)

    def test_all_confidence_gates_must_pass(self):
        cases = [(response(probability=.95, confidence=.8, review=.1), True),
                 (response(probability=.949), False), (response(confidence=.799), False),
                 (response(review=.101), False), (response(review=.5), False)]
        for i, (answer, skip) in enumerate(cases):
            with self.subTest(i=i):
                self.full.reset_mock()
                self.post.return_value = answer
                self.router.triage(self.conn, self.thought(tid=str(i)))
                self.assertEqual(self.full.call_count, 0 if skip else 1)

    def test_useful_and_uncertain_categories_always_get_full_triage(self):
        for category in ('log', 'actionable', 'uncertain'):
            with self.subTest(category=category):
                self.post.return_value = response(category, 1, 1, 0)
                self.router.triage(self.conn, self.thought(tid=category))
                self.assertEqual(self.audit(category)['reason'], 'useful_or_uncertain')
                self.assertEqual(self.row(category)['summary'], FULL_DECISION['summary'])
        self.assertEqual(self.full.call_count, 3)

    def test_whole_mixed_thought_and_current_context_reach_both_models(self):
        text = ('Good dog. ' * 700) + 'It would be cool to build a calendar dashboard.'
        old = self.thought('Previous useful idea', tid='older', age=60)
        self.conn.execute("UPDATE thoughts SET status='done',classification='log',summary='Calendar prototype' WHERE id=?",
                          (old['id'],))
        (self.home/'instructions.md').write_text('Keep notes about calendar experiments.')
        self.post.return_value = response('actionable')
        self.router.triage(self.conn, self.thought(text))
        payload = json.loads(self.post.call_args.args[0])
        prompt = payload['state']['thought_input']
        self.assertIn(text, prompt)
        self.assertIn('Calendar prototype', prompt)
        self.assertIn('Keep notes about calendar experiments.', prompt)
        self.assertIn('0 of 0 remaining', prompt)
        self.assertEqual(payload['state']['triage_rules'], config.TRIAGE_PROMPT.read_text())
        self.assertEqual(self.full.call_args.args, (prompt, 'new'))

    def test_rule_and_preference_edits_are_reloaded(self):
        rules = self.home/'triage.md'
        with patch.object(config, 'TRIAGE_PROMPT', rules):
            for tid in ('first', 'second'):
                rules.write_text('Rules: ' + tid)
                (self.home/'instructions.md').write_text('Preference: ' + tid)
                self.router.triage(self.conn, self.thought(tid=tid))
                payload = json.loads(self.post.call_args.args[0])
                self.assertEqual(payload['state']['triage_rules'], 'Rules: ' + tid)
                self.assertIn('Preference: ' + tid, payload['state']['thought_input'])

    def test_recent_question_bypasses_jev_for_short_answers(self):
        self.thought('Build a chart', tid='question', age=120)
        question = {'items': [{'action': 'ask', 'question': 'Should it use calendar data?',
                               'text': 'Chart idea'}]}
        self.conn.execute("UPDATE thoughts SET status='done',triage_json=? WHERE id='question'",
                          (json.dumps(question),))
        self.router.triage(self.conn, self.thought('Yes.'))
        self.post.assert_not_called()
        self.assertIn('Should it use calendar data?', self.full.call_args.args[0])
        self.assertIn('<transcript>\nYes.\n</transcript>', self.full.call_args.args[0])
        self.assertEqual(self.audit()['reason'], 'recent_question')
        self.conn.execute("UPDATE thoughts SET started_at=? WHERE id='question'", (time.time()-7201,))
        self.router.triage(self.conn, self.thought(tid='later'))
        self.post.assert_called_once()

    def test_disabled_or_unavailable_jev_falls_back(self):
        for reason in ('disabled', 'unavailable'):
            with self.subTest(reason=reason):
                self.cfg['triage_jev_enabled'] = reason != 'disabled'
                self.router.triage_filter.jev._token = None
                self.router.triage(self.conn, self.thought(tid=reason))
                self.assertEqual(self.audit(reason)['reason'], reason)
        self.post.assert_not_called()
        self.assertEqual(self.full.call_count, 2)

    def test_transport_errors_and_malformed_answers_use_full_triage(self):
        for i, failure in enumerate((JevError('http_429'), JevError('timeout'),
                                      RuntimeError('private response and test-only'), None)):
            with self.subTest(i=i):
                self.post.side_effect = failure
                self.post.return_value = None
                self.router.triage_filter.jev.unavailable_until = 0
                with self.assertLogs('ramble.triage_filter', level='WARNING') as logs:
                    self.router.triage(self.conn, self.thought(tid=str(i)))
                self.assertEqual(self.row(str(i))['status'], 'done')
                self.assertNotIn('private response', str(logs.output))
                self.assertNotIn('test-only', str(logs.output))
        self.assertEqual(self.full.call_count, 4)

    def test_oversize_thought_is_never_truncated_or_dropped(self):
        text = 'background ' * 7000 + 'Please research calendar APIs.'
        with self.assertLogs('ramble.triage_filter', level='WARNING'):
            self.router.triage(self.conn, self.thought(text))
        self.post.assert_not_called()
        self.assertIn(text, self.full.call_args.args[0])
        self.assertEqual(self.audit()['reason'], 'request_too_large')

    def test_invalid_threshold_cannot_skip_triage(self):
        for i, (key, value) in enumerate((('triage_jev_min_probability', .5),
                                         ('triage_jev_max_review', .5),
                                         ('triage_jev_min_confidence', True))):
            with self.subTest(key=key), patch.dict(self.cfg, {key: value}):
                with self.assertLogs('ramble.triage_filter', level='WARNING'):
                    self.router.triage(self.conn, self.thought(tid=str(i)))
        self.post.assert_not_called()
        self.assertEqual(self.full.call_count, 3)

    def test_missing_audit_forces_full_triage(self):
        thought = self.thought()
        with patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            with self.assertLogs('ramble.triage_filter', level='WARNING'):
                self.router.triage(self.conn, thought)
        self.full.assert_called_once()
        self.assertEqual(self.row()['status'], 'done')

    def test_owned_and_candidate_speech_does_not_reach_either_model(self):
        for status in ('done', 'failed', 'candidate'):
            thought = self.thought(tid=status)
            self.claim(status, status)
            self.router.triage(self.conn, thought)
            self.assertEqual(self.row(status)['status'], 'closed' if status == 'candidate' else 'done')
        self.post.assert_not_called()
        self.full.assert_not_called()

    def test_mixed_claimed_speech_is_excluded_from_both_models(self):
        self.thought('Unclaimed calendar idea.')
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,text,thought_id) "
                          "VALUES ('owned',1,1,1,'unused','Already answered request.','new')")
        self.claim('owned')
        self.conn.execute("UPDATE thoughts SET text=text || '\nAlready answered request.' WHERE id='new'")
        self.post.return_value = response('log')
        self.router.triage(self.conn, self.row())
        prompt = json.loads(self.post.call_args.args[0])['state']['thought_input']
        self.assertIn('Unclaimed calendar idea.', prompt)
        self.assertNotIn('Already answered request.', prompt)
        self.assertEqual(self.full.call_args.args[0], prompt)

    def test_pause_or_new_claim_after_jev_prevents_fallback_and_side_effects(self):
        decide = self.router.triage_filter.decide
        for i, (change, category) in enumerate((('claim', 'log'), ('claim', 'personal'),
                                                ('pause', 'log'), ('pause', 'personal'))):
            with self.subTest(change=change, category=category):
                self.post.return_value = response(category)
                tid = str(i)
                thought = self.thought(tid=tid)
                def changed(*args, **kwargs):
                    answer = decide(*args, **kwargs)
                    if change == 'claim':
                        self.claim(tid)
                    else:
                        state.write(True, time.time()+3600)
                    return answer
                with patch.object(self.router.triage_filter, 'decide', side_effect=changed):
                    self.router.triage(self.conn, thought)
                self.assertEqual(self.row(tid)['status'], 'closed')
                self.assertIsNone(self.router.current)
                state.write(False)
        self.full.assert_not_called()
        self.child.assert_not_called()
        self.notify.assert_not_called()

    def test_full_triage_still_obeys_dispatch_cap(self):
        self.post.return_value = response('actionable')
        self.full.return_value = {'classification': 'actionable', 'summary': 'Calendar work',
            'items': [{'kind': 'task', 'text': 'Build a calendar.', 'action': 'dispatch',
                       'question': None, 'dispatch': {'name': 'Calendar', 'cwd': '/tmp',
                                                      'prompt': 'Build a calendar.'}}], 'notify': None}
        self.router.triage(self.conn, self.thought('Build a calendar.'))
        self.assertEqual(self.conn.execute('SELECT status FROM dispatches').fetchone()[0], 'skipped_cap')
        self.child.assert_not_called()


class AssessmentTests(unittest.TestCase):
    def test_invalid_answers_are_rejected(self):
        values = [None, [], {}, response(probability=float('nan')),
                  response(confidence=True), response(review='0.01'), response(review=-.1),
                  response(confidence=float('inf')), response(probability=1.1)]
        for mutate in (lambda x: x.update(model='jev-latest'),
                       lambda x: x['answers'].pop('needs_review'),
                       lambda x: x['answers'].update(extra={}),
                       lambda x: x['answers']['category'].update(type='noul'),
                       lambda x: x['answers']['category'].update(choice='actionable'),
                       lambda x: x['answers']['category'].update(choice=[]),
                       lambda x: x['answers']['category']['probabilities'].pop('uncertain'),
                       lambda x: x['answers']['category']['probabilities'].update(ambient=.5),
                       lambda x: x['answers']['category']['probabilities'].update(ambient=True),
                       lambda x: x['answers']['needs_review'].update(type='choice')):
            answer = copy.deepcopy(response())
            mutate(answer)
            values.append(answer)
        for answer in values:
            with self.subTest(answer=answer), self.assertRaises(JevError):
                parse_assessment(answer, 'jev-1.13.0')


if __name__ == '__main__':
    unittest.main()
