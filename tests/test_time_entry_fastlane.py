"""Time-entry writes wait for complete speech and leave durable thought receipts."""
import sqlite3
import unittest
from unittest.mock import patch

from ramble import config, db, time_entries
from ramble.fastlane import FastLane
from ramble.reminders import UncertainError


class FastTimeEntryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.addCleanup(self.conn.close)
        self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,text) VALUES ('thought',100,104,'Rename timer')")
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,thought_id) VALUES ('first',100,104,4,'unused','transcribed','thought')")
        self.fast = FastLane(dict(config.DEFAULTS, fast_speak=False, dispatch_daily_cap=0, fast_coalesce_s=0))
        self.item = {'seg': {'id': 'first', 'started_at': 100, 'ended_at': 104}, 'text': 'Ramble rename my current timer'}
        self.action = {'operation': 'update', 'entry_id': 71, 'description': 'debugging', 'project_id': 3, 'clear_project': False}
        self.ans = {'reply': 'Not confirmed', 'handoff': True, 'reminder': None, 'time_entry': self.action}
        allowed = patch('ramble.fastlane.state.activity_allowed', return_value=True)
        allowed.start()
        self.addCleanup(allowed.stop)

    def test_records_saved_project_before_playback_failure(self):
        self.fast.cfg['fast_speak'] = True
        def playback(*args):
            self.assertIn('time entry handled', self.conn.execute('SELECT text FROM thoughts').fetchone()[0])
            raise RuntimeError('speaker failed')
        with patch.object(self.fast, 'run_codex', return_value=dict(self.ans)), patch.object(time_entries.TimeEntries, 'execute', return_value={'reply': 'Updated debugging under engineering.'}) as execute, patch('ramble.speech.speak', side_effect=playback), self.assertLogs('ramble.fast', level='ERROR'):
            self.fast.respond(self.conn, [self.item])
        execute.assert_called_once()
        text = self.conn.execute('SELECT text FROM thoughts').fetchone()[0]
        self.assertIn('engineering', text)
        self.assertIn('spoken reply FAILED', text)
        self.assertNotIn('handoff: dispatch', text)

    def test_continuation_changes_project_before_single_write(self):
        second = {'seg': {'id': 'second', 'started_at': 105, 'ended_at': 108}, 'text': 'Actually put it in life'}
        def answer(prompt, **kwargs):
            if 'Actually put it in life' not in prompt:
                self.fast.q.put(second)
                return dict(self.ans)
            return dict(self.ans, time_entry=dict(self.action, project_id=4))
        with patch.object(self.fast, 'run_codex', side_effect=answer), patch.object(time_entries.TimeEntries, 'execute', return_value={'reply': 'Updated under life.'}) as execute:
            self.fast.respond(self.conn, [self.item])
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0]['project_id'], 4)

    def test_uncertain_result_is_not_handed_to_worker(self):
        with patch.object(self.fast, 'run_codex', return_value=dict(self.ans)), patch.object(time_entries.TimeEntries, 'execute', side_effect=UncertainError('Check Toggl first.')):
            self.fast.respond(self.conn, [self.item])
        text = self.conn.execute('SELECT text FROM thoughts').fetchone()[0]
        self.assertIn('unconfirmed', text)
        self.assertNotIn('handoff: dispatch', text)

    def test_pause_prevents_write(self):
        with patch('ramble.fastlane.state.activity_allowed', side_effect=[True, True, False]), patch.object(self.fast, 'run_codex', return_value=dict(self.ans)), patch.object(time_entries.TimeEntries, 'execute') as execute:
            self.fast.respond(self.conn, [self.item])
        execute.assert_not_called()

    def test_multiple_actions_are_clarified_before_either_write(self):
        with patch.object(self.fast, 'run_codex', return_value=dict(self.ans, reminder={'operation': 'create'})), patch.object(time_entries.TimeEntries, 'execute') as execute, patch('ramble.reminders.Reminders.execute') as reminder:
            self.fast.respond(self.conn, [self.item])
        execute.assert_not_called()
        reminder.assert_not_called()

    def test_start_uses_structured_adapter_and_records_receipt_without_dispatch(self):
        action = {'operation': 'start', 'entry_id': None, 'workspace_id': 22,
                  'project_id': 4, 'description': 'debugging', 'clear_project': False}
        with patch.object(self.fast, 'run_codex', return_value=dict(self.ans, time_entry=action)), patch.object(
                time_entries.TimeEntries, 'execute', return_value={'reply': 'Started your timer: debugging, under engineering.'}) as execute:
            self.fast.respond(self.conn, [dict(self.item, text='Ramble start a new timer for debugging')])
        self.assertEqual(execute.call_args.args[0], action)
        text = self.conn.execute('SELECT text FROM thoughts').fetchone()[0]
        self.assertIn('Started your timer', text)
        self.assertIn('time entry handled: never dispatch or repeat this request', text)
        self.assertNotIn('handoff: dispatch', text)
