"""Reminder delivery contracts with a fake Stoic service; no live writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from ramble.reminders import Reminders, ReminderError, UncertainError, due_time


FUTURE = (datetime.now(timezone.utc) + timedelta(days=3)).replace(second=0, microsecond=0).isoformat()


class FakeStoic(Reminders):
    def __init__(self, home):
        super().__init__(home)
        self.data = {}
        self.calls = []
        self.lose_create_response = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if method == 'GET' and '?' in path:
            return deepcopy(list(self.data.values()))
        if method == 'POST':
            key = 'job-' + str(len(self.data) + 1)
            job = dict(body, note_id=key, status='active', due_instances=[{'id': 'occ-1', 'due_at': body['due_at'], 'status': 'pending'}])
            self.data[key] = job
            if self.lose_create_response:
                raise UncertainError('Lost response; check Stoic.')
            return deepcopy(job)
        job = self.data[path.split('/')[3]]
        if method == 'PATCH':
            if path.endswith('/occurrences'):
                job['due_instances'][0]['status'] = body['status']
                if job['recurrence_type'] != 'none':
                    job['due_instances'].append({'id': 'occ-2', 'due_at': FUTURE, 'status': 'pending'})
            elif '/due-instances/' in path:
                job['due_instances'][0].update(body)
            else:
                job.update(body)
        return deepcopy(job)


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ramble-reminder-test-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.client = FakeStoic(self.home)
        self.action = dict(operation='create', title='Text a friend', description='A quick hello', due_at=FUTURE, recurrence_type='none', kind='reminder')

    def test_create_receipt_replay_and_semantic_duplicate(self):
        result = self.client.execute(self.action, 'request-1')
        self.assertEqual(result['job']['title'], 'Text a friend')
        self.assertNotIn('Ramble request', result['job']['description'])
        self.assertEqual(self.client.data['job-1']['owner'], 'user')
        self.assertEqual(self.client.execute(self.action, 'request-1'), result)
        result = self.client.execute(self.action, 'different-capture')
        self.assertIn('Already saved', result['reply'])
        self.assertEqual(sum(c[0] == 'POST' for c in self.client.calls), 1)
        self.assertEqual((self.home / 'reminders.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_uncertain_create_reconciles_across_restart_without_reposting(self):
        self.client.lose_create_response = True
        with self.assertRaises(UncertainError):
            self.client.execute(self.action, 'lost-response')
        replacement = FakeStoic(self.home)
        replacement.data = self.client.data
        result = replacement.execute(self.action, 'lost-response')
        self.assertTrue(result['ok'])
        self.assertFalse(any(c[0] == 'POST' for c in replacement.calls))

    def test_uncertain_request_is_not_replayed_when_no_remote_receipt(self):
        with patch.object(self.client, 'mutate', side_effect=UncertainError('Offline')):
            with self.assertRaises(UncertainError):
                self.client.execute(self.action, 'unknown')
        with self.assertRaises(UncertainError):
            self.client.execute(self.action, 'unknown')
        self.assertFalse(any(c[0] == 'POST' for c in self.client.calls))

    def test_request_id_cannot_be_reused_for_different_payload(self):
        self.client.execute(self.action, 'same')
        with self.assertRaisesRegex(ReminderError, 'already used'):
            self.client.execute(dict(self.action, title='Different'), 'same')

    def test_move_complete_and_cancel_use_correct_endpoints(self):
        self.client.execute(dict(self.action, kind='check-in', recurrence_type='weekly'), 'create')
        self.assertEqual(self.client.data['job-1']['owner'], 'assistant')
        target = dict(job_id='job-1', occurrence_id='occ-1')
        later = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
        moved = self.client.execute(dict(operation='reschedule', due_at=later, **target), 'move')
        self.assertIn('Moved', moved['reply'])
        self.assertEqual(self.client.calls[-1][1], '/api/assistant-jobs/job-1/due-instances/occ-1')
        done = self.client.execute(dict(operation='complete', **target), 'done')
        self.assertIn('Marked done', done['reply'])
        self.assertEqual(done['job']['due_instances'][0]['id'], 'occ-2')
        cancelled = self.client.execute(dict(operation='cancel', job_id='job-1'), 'cancel')
        self.assertEqual(cancelled['job']['status'], 'cancelled')

    def test_stale_occurrence_cannot_be_changed(self):
        self.client.execute(self.action, 'create')
        with self.assertRaisesRegex(ReminderError, 'no longer pending'):
            self.client.execute(dict(operation='complete', job_id='job-1', occurrence_id='missing'), 'bad')
        self.assertEqual(sum(c[0] == 'PATCH' for c in self.client.calls), 0)

    def test_validation_and_daylight_saving(self):
        for changes in ({'title': ''}, {'kind': 'investigation'}, {'recurrence_type': 'hourly'}, {'due_at': 'yesterday'}, {'due_at': '2000-01-01T10:00:00Z'}):
            with self.subTest(changes=changes), self.assertRaises(ReminderError):
                self.client.execute(dict(self.action, **changes), 'invalid')
        with self.assertRaisesRegex(ReminderError, 'daylight saving'):
            due_time('2030-03-10T02:30:00')
        with self.assertRaisesRegex(ReminderError, 'daylight saving'):
            due_time('2030-11-03T01:30:00')
        self.assertTrue(due_time('2030-11-03T01:30:00-06:00').endswith('-06:00'))
        self.assertTrue(due_time('2030-07-04T16:00:00').endswith('-05:00'))
        self.assertEqual(self.client.calls, [])

    def test_result_must_confirm_requested_mutation(self):
        self.client.execute(self.action, 'create')
        self.client.data['job-1']['due_instances'] = []
        with self.assertRaises(UncertainError):
            self.client.result('create', self.client.data['job-1'])
        with self.assertRaises(UncertainError):
            self.client.result('cancel', self.client.data['job-1'])

    def test_changed_time_is_checked_against_stoic_response(self):
        self.client.execute(self.action, 'create')
        old = deepcopy(self.client.data['job-1'])
        action = dict(operation='reschedule', job_id='job-1', occurrence_id='occ-1', due_at='2030-07-04T16:00:00-05:00')
        with patch.object(self.client, 'request', return_value=old):
            with self.assertRaisesRegex(UncertainError, 'requested time'):
                self.client.execute(action, 'move-unconfirmed')

    def test_malformed_write_response_stays_uncertain(self):
        with patch.object(self.client, 'mutate', side_effect=TypeError('bad response')):
            with self.assertRaises(UncertainError): self.client.execute(self.action, 'malformed')
        with self.assertRaises(UncertainError): self.client.execute(self.action, 'malformed')
        self.assertFalse(any(c[0] == 'POST' for c in self.client.calls))

    def test_http_error_messages_do_not_leak_secrets_and_writes_do_not_retry(self):
        client = Reminders(self.home)
        for method in ['GET', 'POST']:
            with patch.dict('os.environ', {'STOIC_API_TOKEN': 'SECRET'}), patch('urllib.request.urlopen', side_effect=TimeoutError('SECRET')) as call:
                with self.assertRaises(ReminderError) as error:
                    client.request(method, '/api/assistant-jobs', {} if method == 'POST' else None)
                self.assertNotIn('SECRET', str(error.exception))
                self.assertEqual(call.call_count, 2 if method == 'GET' else 1)


if __name__ == '__main__':
    unittest.main()
