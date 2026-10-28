"""Transport and decision boundaries, without network access or real credentials."""
import copy
import json
import os
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble.jev import JevClient, JevError, MAX_RESPONSE, NoRedirect


def response(directed=.87, related=.89):
    return {'model': 'jev-1.13.0', 'answers': {
        'directed': {'type': 'noul', 'noul': directed},
        'related': {'type': 'noul', 'noul': related},
    }}


class JevTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'JEV_API_KEY': 'test-only'}, clear=True))
        self.client = JevClient({'jev_timeout_s': .1})

    def test_both_scores_must_pass(self):
        for directed, related, accepted in ((.87,.89,True),(.16,.32,False),(.35,.89,False),(.9,.04,False),(.58,.58,False)):
            with self.subTest(directed=directed, related=related), patch.object(self.client, '_post', return_value=response(directed, related)):
                answer = self.client.classify([{'speaker':'Ramble','text':'A very elaborate stopwatch.'}], 'Will you respond?', 110)
                self.assertEqual(answer['accept'], accepted)

    def test_bounded_payload_uses_two_distinct_questions(self):
        with patch.object(self.client, '_post', return_value=response()) as post:
            self.client.classify([{'text':str(i)} for i in range(12)], 'Continue?', 110)
        payload = json.loads(post.call_args.args[0])
        self.assertEqual(len(payload['state']['recent_conversation']), 8)
        self.assertEqual(payload['state']['new_utterance'], 'Continue?')
        self.assertEqual(set(payload['questions']), {'directed','related'})
        self.assertNotIn('test-only', post.call_args.args[0].decode())

    def test_malformed_responses_fail_closed(self):
        malformed = [None, [], {'answers':{}}, response(True), response('0.9'), response(float('nan')),
                     response(float('inf')), response(-.1), response(1.1)]
        for mutate in (lambda x: x.update(model='jev-latest'),
                       lambda x: x['answers'].pop('related'),
                       lambda x: x['answers'].update(extra={'type':'noul','noul':1}),
                       lambda x: x['answers']['related'].update(type='choice')):
            value = copy.deepcopy(response()); mutate(value); malformed.append(value)
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(JevError):
                JevClient.parse(value, 'jev-1.13.0')

    def test_timeout_bounds_whole_request_and_prevents_overlapping_transports(self):
        release = threading.Event()
        finished = threading.Event()
        def slow(*args):
            release.wait(2)
            finished.set()
            return response()
        with patch.object(self.client, '_post', side_effect=slow) as post:
            start = time.monotonic()
            try:
                with self.assertRaisesRegex(JevError, 'timeout'):
                    self.client.classify([], 'Continue?', 110)
                self.assertLess(time.monotonic() - start, .5)
                self.assertFalse(self.client.available)
                self.client.unavailable_until = 0
                with self.assertRaisesRegex(JevError, 'unavailable'):
                    self.client.classify([], 'Continue?', 110)
                self.assertEqual(post.call_count, 1)
            finally:
                release.set()
                finished.wait(1)

    def test_error_body_and_credentials_are_not_exposed(self):
        with patch.object(self.client, '_post', side_effect=RuntimeError('private provider body test-only')):
            with self.assertRaisesRegex(JevError, '^transport_error$'):
                self.client.classify([], 'Continue?', 110)
        self.assertFalse(self.client.available)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'',{},'https://elsewhere.test'))

    def test_response_size_is_bounded(self):
        reply = Mock(status=200)
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        reply.read1.side_effect = lambda size: b'x' * size
        opener = Mock(); opener.open.return_value = reply
        with patch('ramble.jev.urllib.request.build_opener', return_value=opener):
            with self.assertRaisesRegex(JevError, 'response_too_large'):
                self.client._post(b'{}', time.monotonic() + 1)
        self.assertLessEqual(sum(c.args[0] for c in reply.read1.call_args_list), MAX_RESPONSE + 1)

    def test_missing_key_disables_and_oversize_input_never_sends(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(JevClient({}).available)
        with patch.object(self.client, '_post') as post:
            with self.assertRaisesRegex(JevError, 'request_too_large'):
                self.client.classify([], 'x' * 65536, 110)
        post.assert_not_called()
        self.assertFalse(self.client._slot.locked())


if __name__ == '__main__':
    unittest.main()
