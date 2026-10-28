"""No live credentials, timers, playback or dispatches are used in these tests."""
import tempfile
import urllib.error
from pathlib import Path
import unittest
from unittest.mock import patch

from ramble.reminders import ReminderError, UncertainError
from ramble.time_entries import TimeEntries

ENTRY = {'id': 71, 'description': 'old', 'project_id': 3, 'project_name': 'engineering', 'stop': None}
SNAPSHOT = {'entry': ENTRY, 'projects': [{'id': 3, 'name': 'engineering'}, {'id': 4, 'name': 'life'}]}


class TimeEntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = TimeEntries(Path(self.temp.name))
        self.action = {'operation': 'update', 'entry_id': 71, 'description': 'new', 'project_id': 4, 'clear_project': False}

    def test_assigns_project_and_replays_receipt_without_second_write(self):
        saved = {**ENTRY, 'description': 'new', 'project_id': 4, 'project_name': 'life'}
        with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, {'entry': saved}]) as request:
            result = self.adapter.execute(self.action, 'voice-one')
            self.assertIn('life', result['reply'])
            self.assertEqual(self.adapter.execute(self.action, 'voice-one'), result)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args, ('PATCH', {'expected_entry_id': 71, 'description': 'new', 'project_id': 4}))

    def test_description_only_preserves_project_and_project_only_preserves_description(self):
        for changes in ({'description': 'new', 'project_id': None}, {'description': None, 'project_id': 4}):
            action = {**self.action, **changes}
            wanted = {k: v for k, v in changes.items() if v is not None}
            with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, {'entry': {**ENTRY, **wanted}}]) as request:
                self.adapter.execute(action, 'voice-' + str(changes['project_id']))
            self.assertEqual(request.call_args.args[1], {'expected_entry_id': 71, **wanted})

    def test_missing_or_changed_timer_and_invalid_project_prevent_write(self):
        for snapshot, action in [
            ({**SNAPSHOT, 'entry': None}, self.action),
            ({**SNAPSHOT, 'entry': {**ENTRY, 'id': 72}}, self.action),
            (SNAPSHOT, {**self.action, 'project_id': 999}),
        ]:
            with patch.object(self.adapter, 'request', return_value=snapshot) as request, self.assertRaises(ReminderError):
                self.adapter.execute(action, 'voice-one')
            request.assert_called_once_with()

    def test_uncertain_write_never_retries(self):
        with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, UncertainError('lost response')]) as request:
            with self.assertRaises(UncertainError): self.adapter.execute(self.action, 'voice-one')
            with self.assertRaises(UncertainError): self.adapter.execute(self.action, 'voice-one')
        self.assertEqual(request.call_count, 2)

    def test_wrong_saved_project_is_not_success(self):
        with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, {'entry': ENTRY}]), self.assertRaises(UncertainError):
            self.adapter.execute(self.action, 'voice-one')

    def test_clear_project_is_explicit(self):
        action = {**self.action, 'description': None, 'project_id': None, 'clear_project': True}
        with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, {'entry': {**ENTRY, 'project_id': None, 'project_name': None}}]) as request:
            result = self.adapter.execute(action, 'voice-one')
        self.assertEqual(request.call_args.args[1], {'expected_entry_id': 71, 'project_id': None})
        self.assertIn('no project', result['reply'])

    def test_read_quota_failure_reports_wait_without_retry(self):
        error = urllib.error.HTTPError('https://test', 429, 'limited', {'Retry-After':'555'}, None)
        with patch.dict('os.environ', {'STOIC_API_TOKEN':'synthetic'}), patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(ReminderError, '10 minutes'):
                self.adapter.snapshot()
        request.assert_called_once()

    def test_write_quota_failure_stays_uncertain_and_never_retries(self):
        error = urllib.error.HTTPError('https://test', 429, 'limited', {'Retry-After':'bad'}, None)
        with patch.dict('os.environ', {'STOIC_API_TOKEN':'synthetic'}), patch('urllib.request.urlopen', side_effect=error) as request:
            with self.assertRaisesRegex(UncertainError, 'Check the current timer'):
                self.adapter.request('POST', {}, path='start')
        request.assert_called_once()


class TimerStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.adapter = TimeEntries(Path(self.temp.name))
        self.action = {'operation': 'start', 'entry_id': 71, 'workspace_id': 22,
                       'description': 'debugging', 'project_id': 4, 'clear_project': False}
        self.workspaces = {'workspaces': [{'id': 22, 'projects': SNAPSHOT['projects']}]}
        self.saved = {'entry': {**ENTRY, 'id': 72, 'workspace_id': 22, 'project_id': 4,
                               'description': 'debugging', 'start': '2026-10-27T03:00:00Z'}}

    def test_start_replays_receipt_without_second_creation(self):
        with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, self.workspaces, self.saved]) as request:
            result = self.adapter.execute(self.action, 'voice-start')
            self.assertIn('Started', result['reply'])
            self.assertEqual(self.adapter.execute(self.action, 'voice-start'), result)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args.args, ('POST', {'workspace_id': 22, 'project_id': 4,
            'description': 'debugging', 'expected_entry_id': 71}))
        self.assertEqual(request.call_args.kwargs, {'path': 'start'})

    def test_start_without_running_timer_or_project(self):
        action = {**self.action, 'entry_id': None, 'project_id': None}
        saved = {'entry': {**self.saved['entry'], 'project_id': None, 'project_name': None}}
        with patch.object(self.adapter, 'request', side_effect=[{'entry': None}, self.workspaces, saved]) as request:
            self.assertIn('no project', self.adapter.execute(action, 'voice-idle')['reply'])
        self.assertIsNone(request.call_args.args[1]['expected_entry_id'])

    def test_changed_timer_prevents_start(self):
        with patch.object(self.adapter, 'request', return_value={'entry': {**ENTRY, 'id': 99}}) as request:
            with self.assertRaises(ReminderError):
                self.adapter.execute(self.action, 'voice-start')
        request.assert_called_once_with()

    def test_invalid_workspace_or_project_prevents_start(self):
        for changes in ({'workspace_id': 99}, {'project_id': 99}):
            with self.subTest(changes=changes), patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, self.workspaces]) as request:
                with self.assertRaises(ReminderError):
                    self.adapter.execute({**self.action, **changes}, 'voice-start')
                self.assertEqual(request.call_count, 2)

    def test_missing_or_invalid_fields_prevent_reads_and_writes(self):
        actions = [{k: v for k, v in self.action.items() if k != 'entry_id'}]
        actions += [{**self.action, **changes} for changes in (
            {'workspace_id': True}, {'project_id': True}, {'entry_id': True},
            {'description': None}, {'clear_project': True})]
        for action in actions:
            with self.subTest(action=action), patch.object(self.adapter, 'request') as request:
                with self.assertRaises(ReminderError):
                    self.adapter.execute(action, 'voice-start')
                request.assert_not_called()

    def test_uncertain_start_never_retries(self):
        with patch.object(self.adapter, 'request', side_effect=[SNAPSHOT, self.workspaces, UncertainError('lost response')]) as request:
            for _ in range(2):
                with self.assertRaises(UncertainError):
                    self.adapter.execute(self.action, 'voice-start')
        self.assertEqual(request.call_count, 3)

    def test_mismatched_or_unconfirmed_entry_is_not_success(self):
        for changes in ({'id': 71}, {'workspace_id': 99}, {'project_id': 3}, {'description': 'wrong'}, {'start': None}, {'stop': 'now'}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as home:
                adapter = TimeEntries(Path(home))
                saved = {'entry': {**self.saved['entry'], **changes}}
                with patch.object(adapter, 'request', side_effect=[SNAPSHOT, self.workspaces, saved]):
                    with self.assertRaises(UncertainError):
                        adapter.execute(self.action, 'voice-start')
