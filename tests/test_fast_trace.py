"""Failure diagnostics survive process termination without retaining payloads."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ramble.codex_runner import run
from ramble.fast_trace import FastTrace


class FastTraceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='ramble-test-trace-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def read(self, trace):
        return [json.loads(line) for line in trace.path.read_text().splitlines()]

    def test_partial_events_keep_tool_timing_without_payload_or_credentials(self):
        trace = FastTrace(self.home, request_id='segment')
        command = 'curl https://wttr.in/Example -H "Authorization: Bearer private-token"'
        events = [
            {'type':'thread.started', 'thread_id':'native-thread'},
            {'type':'item.started', 'item':{'id':'item_1','type':'command_execution','command':command,'status':'in_progress'}},
            {'type':'item.completed','item':{'id':'item_1','type':'command_execution','command':command,'status':'completed','exit_code':0,'aggregated_output':'private-token weather data'}},
            {'type':'item.completed','item':{'type':'agent_message','text':'private spoken reply'}},
        ]
        output = '\n'.join(json.dumps(e) for e in events) + '\n'
        trace.consume(output[:40], '')
        trace.consume(output, 'Authorization: Bearer private-token\n')
        trace.consume(output, 'Authorization: Bearer private-token\n')
        trace.finish('completed')
        rows = self.read(trace)
        self.assertEqual(sum(r['stage']=='codex_event' for r in rows), 4)
        self.assertEqual(rows[2]['tool'], 'weather_http')
        self.assertEqual(rows[-1]['active_tools'], 0)
        self.assertEqual(os.stat(trace.path).st_mode & 0o777, 0o600)
        for private in ('private-token', 'private spoken reply', 'Authorization', command):
            self.assertNotIn(private, trace.path.read_text())

    def test_timeout_preserves_last_started_tool_and_elapsed_time(self):
        trace = FastTrace(self.home, request_id='slow-request')
        event = json.dumps({'type':'item.started','item':{'id':'item_1','type':'command_execution','command':'curl https://wttr.in/Example'}})
        script = f'import time; print({event!r},flush=True); time.sleep(20)'
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable,'-c',script],input='',env=dict(os.environ),timeout=.25,cwd=str(self.home),trace=trace)
        trace.finish('timeout')
        rows = self.read(trace)
        self.assertEqual(rows[-1]['last_event'], 'item.started')
        self.assertEqual(rows[-1]['active_tools'], 1)
        self.assertGreaterEqual(rows[-1]['elapsed_s'], .25)
        self.assertTrue(any(r['stage']=='process_reaped' for r in rows))

    def test_retention_is_bounded_and_end_survives_event_limit(self):
        old = self.home/'20000101-old.jsonl'; old.touch(); os.utime(old,(0,0))
        unrelated = self.home/'keep.txt'; unrelated.write_text('keep')
        trace = FastTrace(self.home)
        for _ in range(1200):
            trace.event('tick')
        trace.finish('timeout')
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(len(self.read(trace)), 1001)
        self.assertEqual(self.read(trace)[-1]['outcome'], 'timeout')
