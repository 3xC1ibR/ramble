import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ramble import config
from ramble.router import Router, DECISION_SCHEMA


class TriageAuditTests(unittest.TestCase):
    def test_full_prompt_is_preserved_before_run_and_after_timeout_for_both_backends(self):
        with tempfile.TemporaryDirectory(prefix='ramble-test-audit-', dir='/tmp') as home:
            root = Path(home)
            policy = root/'policy.md'
            policy.write_text('Exact policy\n' + 'X'*8000)
            with patch.multiple(config, TRIAGE_DIR=root, TRIAGE_PROMPT=policy, HOME=root):
                router = Router(dict(config.DEFAULTS, triage_model='test', triage_effort='high'), threading.Event())
                for backend in ['codex','claude']:
                    def run(cmd, **kwargs):
                        saved = json.loads((root/(backend+'.json')).read_text())
                        self.assertEqual(saved['status'],'started')
                        self.assertIn(policy.read_text(),saved['input'])
                        self.assertIn('Executable Ramble actions',saved['input'])
                        self.assertEqual(saved['schema'],DECISION_SCHEMA)
                        self.assertEqual(saved['context_input'],'Exact new request')
                        self.assertEqual(saved['prompt_sha256'],hashlib.sha256(saved['input'].encode()).hexdigest())
                        if backend == 'codex': self.assertEqual(kwargs['input'],saved['input'])
                        else: self.assertEqual(cmd[cmd.index('--append-system-prompt')+1],saved['system_prompt'])
                        raise subprocess.TimeoutExpired(cmd,1)
                    with patch('ramble.router.subprocess.run',side_effect=run), self.assertRaises(subprocess.TimeoutExpired):
                        getattr(router,'run_'+backend)('Exact new request',backend)
                    self.assertEqual(json.loads((root/(backend+'.json')).read_text())['status'],'failed')
