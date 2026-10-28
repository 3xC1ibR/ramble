"""Persistent preferences and prompt consumers; no mic, playback or agents."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ramble import config, db, instructions
from ramble.fastlane import FastLane, REPLY_SCHEMA
from ramble.router import Router, DECISION_SCHEMA


class InstructionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ramble-test-instructions-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patches = patch.multiple(config, HOME=self.home, DB_PATH=self.home / 'ramble.db',
                                 TRIAGE_DIR=self.home / 'triage', CONFIG_PATH=self.home / 'config.json')
        patches.start()
        self.addCleanup(patches.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False, speak_questions=False)
        self.fast = FastLane(self.cfg)
        self.router = Router(self.cfg, threading.Event())
        self.thought = {'id': 'test', 'started_at': 100, 'ended_at': 103, 'text': 'Ramble, hello'}

    def test_save_reload_clear_and_existing_settings(self):
        config.CONFIG_PATH.write_text('{"dispatch_daily_cap":0,"tts_voice":"custom"}')
        before = config.CONFIG_PATH.read_bytes()
        self.assertEqual(instructions.snapshot()['text'], '')
        text = 'Use concise answers.\nRemember café preferences.\n'
        instructions.save(text)
        self.assertEqual(instructions.snapshot()['text'], text)
        self.assertEqual(instructions.path().stat().st_mode & 0o777, 0o600)
        # Fresh Python process proves no in-memory preference cache is required.
        child = subprocess.run([sys.executable, '-c',
            'from ramble.instructions import snapshot; print(snapshot()["text"], end="")'],
            env={**__import__('os').environ, 'RAMBLE_HOME': str(self.home)},
            capture_output=True, text=True, check=True)
        self.assertEqual(child.stdout, text)
        instructions.save('')
        self.assertEqual(instructions.snapshot()['text'], '')
        self.assertEqual(config.CONFIG_PATH.read_bytes(), before)
        self.assertFalse((self.home / 'state.json').exists())

    def test_running_prompt_builders_reload_for_next_request(self):
        for text in ('Prefer brief answers.', 'Prefer detailed answers.\nUse metric units.'):
            instructions.save(text)
            self.assertIn(text, self.fast.build_prompt(self.conn, 'Ramble, hello'))
            self.assertIn(text, self.router.build_input(self.conn, self.thought))
        instructions.save('')
        self.assertNotIn('Prefer detailed answers.', self.fast.build_prompt(self.conn, 'Hello'))
        self.assertNotIn('Prefer detailed answers.', self.router.build_input(self.conn, self.thought))

    def test_write_failure_invalid_text_and_conflict_preserve_original(self):
        original = instructions.save('Keep this')
        for text in ('x' * (instructions.MAX_BYTES + 1), 12):
            with self.assertRaises(ValueError):
                instructions.save(text)
        with patch('ramble.instructions.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                instructions.save('Failed draft')
        self.assertEqual(instructions.snapshot(), original)
        self.assertEqual(list(self.home.glob('.instructions-*')), [])
        instructions.save('Changed on another device')
        with self.assertRaises(instructions.ConflictError):
            instructions.save('Old draft', version=original['version'])
        self.assertEqual(instructions.snapshot()['text'], 'Changed on another device')

    def test_bad_file_is_not_silently_ignored(self):
        instructions.path().write_bytes(b'\xff')
        with self.assertRaises(UnicodeError):
            self.fast.build_prompt(self.conn, 'Hello')
        with self.assertRaises(UnicodeError):
            self.router.build_input(self.conn, self.thought)

    def test_both_triage_backends_receive_saved_instructions_and_disable_hooks(self):
        instructions.save('Call me the user. Include runnable examples in relevant briefs.')
        prompt = self.router.build_input(self.conn, self.thought)
        decision = {'classification': 'log', 'summary': 'Hello', 'items': [], 'notify': None}
        seen = []
        def run(cmd, **kwargs):
            seen.append((cmd, kwargs['input']))
            if cmd[0] == 'codex':
                Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps(decision))
            return subprocess.CompletedProcess(cmd, 0, json.dumps({'structured_output': decision}), '')
        for backend in ('codex', 'claude'):
            self.cfg['triage_backend'] = backend
            with patch('ramble.router.subprocess.run', side_effect=run):
                self.assertEqual(self.router.run_triage(prompt, backend), decision)
        for cmd, sent in seen:
            self.assertIn('Call me the user.', sent)
            self.assertIn('dispatch-budget', sent)
            if cmd[0] == 'codex':
                self.assertTrue({'--ignore-user-config', '--ephemeral', '--disable', 'hooks'} <= set(cmd))
            else:
                self.assertIn('disableAllHooks', ' '.join(cmd))

    def test_schemas_remain_strict(self):
        def check(schema):
            if 'properties' in schema:
                self.assertEqual(set(schema['properties']), set(schema['required']))
                self.assertIs(schema['additionalProperties'], False)
                for child in schema['properties'].values():
                    check(child)
            if 'items' in schema:
                check(schema['items'])
        check(DECISION_SCHEMA)
        check(REPLY_SCHEMA)


if __name__ == '__main__':
    unittest.main()
