"""Reminder writes execute after coalescing, survive playback failure, and respect pause."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ramble import config, db, reminders
from ramble.fastlane import FastLane, REPLY_SCHEMA


class FastReminderTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,text) VALUES ('thought',100,104,'Remind me')")
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,thought_id) VALUES ('first',100,104,4,'unused','transcribed','thought')")
        self.fast = FastLane(dict(config.DEFAULTS, fast_speak=False, dispatch_daily_cap=0, fast_coalesce_s=0))
        self.item = {'seg': {'id': 'first', 'started_at': 100, 'ended_at': 104}, 'text': 'Ramble remind me tomorrow'}
        self.action = {'operation': 'create', 'title': 'Text a friend', 'due_at': '2030-01-01T16:00:00-06:00'}
        self.ans = {'reply': 'Not yet confirmed', 'handoff': True, 'reminder': self.action}
        self.allow = patch('ramble.fastlane.state.activity_allowed', return_value=True)
        self.allow.start(); self.addCleanup(self.allow.stop)

    def test_executes_once_and_records_result_before_playback(self):
        self.fast.cfg['fast_speak'] = True
        def playback(*args):
            text = self.conn.execute('SELECT text FROM thoughts').fetchone()[0]
            self.assertIn('reminder handled', text)
            raise RuntimeError('speaker failed')
        with patch.object(self.fast, 'run_codex', return_value=self.ans), patch.object(reminders.Reminders, 'execute', return_value={'reply': 'Saved for tomorrow.'}) as execute, patch('ramble.speech.speak', side_effect=playback), self.assertLogs('ramble.fast', level='ERROR'):
            self.fast.respond(self.conn, [self.item])
        execute.assert_called_once()
        text = self.conn.execute('SELECT text FROM thoughts').fetchone()[0]
        self.assertIn('Saved for tomorrow.', text)
        self.assertIn('spoken reply FAILED', text)
        self.assertNotIn('handoff: dispatch', text)

    def test_waits_for_continuation_before_executing(self):
        second = {'seg': {'id': 'second', 'started_at': 105, 'ended_at': 108}, 'text': 'Actually Friday'}
        count = 0
        revised = dict(self.action, due_at='2030-01-04T16:00:00-06:00')
        def answer(prompt, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                self.fast.q.put(second)
                return dict(self.ans)
            return dict(self.ans, reminder=revised)
        with patch.object(self.fast, 'run_codex', side_effect=answer), patch.object(reminders.Reminders, 'execute', return_value={'reply': 'Saved Friday.'}) as execute:
            self.fast.respond(self.conn, [self.item])
        self.assertEqual(count, 2)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], revised)

    def test_uncertain_result_never_claims_saved_or_hands_off(self):
        with patch.object(self.fast, 'run_codex', return_value=self.ans), patch.object(reminders.Reminders, 'execute', side_effect=reminders.UncertainError('Check Stoic first.')):
            self.fast.respond(self.conn, [self.item])
        text = self.conn.execute('SELECT text FROM thoughts').fetchone()[0]
        self.assertIn('unconfirmed', text)
        self.assertIn('Check Stoic first.', text)
        self.assertNotIn('handoff: dispatch', text)

    def test_pause_prevents_mutation(self):
        with patch('ramble.fastlane.state.activity_allowed', side_effect=[True, True, False]), patch.object(self.fast, 'run_codex', return_value=self.ans), patch.object(reminders.Reminders, 'execute') as execute:
            self.fast.respond(self.conn, [self.item])
        execute.assert_not_called()

    def test_schema_is_strict_for_every_object(self):
        def check(schema):
            if 'properties' in schema:
                self.assertFalse(schema['additionalProperties'])
                self.assertEqual(set(schema['required']), set(schema['properties']))
                for child in schema['properties'].values(): check(child)
        check(REPLY_SCHEMA)


if __name__ == '__main__':
    unittest.main()
