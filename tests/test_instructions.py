"""Persistent preferences and prompt consumers; no mic, playback or agents."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import action_routing, config, db, instructions
from ramble.fastlane import FastLane, REPLY_SCHEMA
from ramble.router import Router, DECISION_SCHEMA
from ramble.sessions import Sessions


class InstructionsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ramble-test-instructions-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        patches = patch.multiple(config, HOME=self.home, DB_PATH=self.home / 'ramble.db',
                                 TRIAGE_DIR=self.home / 'triage', CONFIG_PATH=self.home / 'config.json',
                                 STATE_PATH=self.home / 'state.json')
        patches.start()
        self.addCleanup(patches.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.cfg = dict(config.DEFAULTS, dispatch_daily_cap=0, fast_speak=False, speak_questions=False,
                        audio_cues_enabled=False, audio_working_enabled=False, fast_coalesce_s=0)
        self.fast = FastLane(self.cfg)
        self.router = Router(self.cfg, threading.Event())
        self.thought = {'id': 'test', 'started_at': 100, 'ended_at': 103, 'text': 'Ramble, hello'}

    def edit(self, old='', new='Address the user as coach.'):
        return dict(operation='update', old_text=old, new_text=new,
                    version=instructions.snapshot()['version'])

    def item(self, text='Hey Ramble, call me coach', ident='first'):
        now = time.time()
        self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,text) VALUES (?,?,?,?)",
                          (ident, now, now, text))
        self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) "
                          "VALUES (?,?,?,1,'unused','transcribed',?,?)", (ident, now, now, text, ident))
        return {'seg': {'id': ident, 'started_at': now, 'ended_at': now}, 'text': text}

    def answer(self, action):
        return dict(reply='Unconfirmed model prose', handoff=False, route=None, reminder=None,
                    time_entry=None, global_instructions=action, resolution='action')

    def test_versioned_edits_preserve_other_preferences_and_support_removal(self):
        instructions.save('Use metric units.\nAddress the user as captain.')
        instructions.execute(self.edit('Address the user as captain.'), 'rename')
        self.assertEqual(instructions.snapshot()['text'], 'Use metric units.\nAddress the user as coach.')
        instructions.execute(self.edit('Address the user as coach.', ''), 'remove')
        self.assertEqual(instructions.snapshot()['text'], 'Use metric units.\n')

    def test_stale_ambiguous_and_oversized_edits_leave_saved_text_intact(self):
        instructions.save('Keep this.\nKeep this.')
        for action in (self.edit('Keep this.'), self.edit('Missing passage'),
                       self.edit(new='x' * instructions.MAX_BYTES), dict(self.edit(), version='stale')):
            with self.subTest(action=action['old_text']):
                with self.assertRaises(instructions.InstructionError):
                    instructions.execute(action, 'invalid')
                self.assertEqual(instructions.snapshot()['text'], 'Keep this.\nKeep this.')

    def test_named_preference_bypasses_work_owner_and_saves_once(self):
        instructions.save('Use metric units.')
        self.fast.sessions = Sessions(self.cfg, backend=Mock())
        item = self.item()
        with patch.object(self.fast.sessions, 'followup_owner', return_value={'session_id': 'owner'}), \
                patch.object(self.fast.sessions, 'route') as route, \
                patch.object(self.fast, 'run_codex', return_value=self.answer(self.edit())) as model:
            self.assertTrue(self.fast.offer(self.conn, item['seg'], item['text']))
            queued = self.fast.q.get_nowait()
            self.assertNotIn('direct_owner', queued)
            self.fast.respond(self.conn, [queued])
            self.fast.respond(self.conn, [queued])
        route.assert_not_called()
        self.assertEqual(instructions.snapshot()['text'], 'Use metric units.\nAddress the user as coach.\n')
        receipt = action_routing.current(self.conn)
        self.assertEqual(receipt['status'], 'completed')
        self.assertIn('Saved to your global instructions', receipt['reply'])
        self.assertEqual(model.call_args.kwargs['schema']['properties']['route'], {'type': 'null'})
        self.assertIn('Address the user as coach.', self.fast.build_prompt(self.conn, 'Hello'))
        self.assertIn('Address the user as coach.', self.router.build_input(self.conn, self.thought))

    def test_correction_during_generation_is_saved_after_coalescing(self):
        item = self.item()
        second = self.item('Actually, call me captain', 'second')
        calls = []
        def answer(prompt, **kwargs):
            calls.append(prompt)
            if len(calls) == 1:
                self.fast.q.put(second)
                return self.answer(self.edit())
            self.assertEqual(instructions.snapshot()['text'], '')
            return self.answer(self.edit(new='Address the user as captain.'))
        with patch.object(self.fast, 'run_codex', side_effect=answer):
            self.fast.respond(self.conn, [item])
        self.assertEqual(len(calls), 2)
        self.assertEqual(instructions.snapshot()['text'], 'Address the user as captain.\n')

    def test_editor_change_during_generation_and_failed_write_cannot_claim_saved(self):
        item = self.item()
        answer = self.answer(self.edit())
        def changed(prompt, **kwargs):
            instructions.save('Saved in Companion while generating.')
            return answer
        with patch.object(self.fast, 'run_codex', side_effect=changed):
            self.fast.respond(self.conn, [item])
        self.assertEqual(instructions.snapshot()['text'], 'Saved in Companion while generating.')
        self.assertEqual(action_routing.current(self.conn)['status'], 'failed')
        self.assertIn('No changes were saved', action_routing.current(self.conn)['reply'])
        with patch.object(self.fast, 'run_codex', return_value=self.answer(self.edit())), \
                patch('ramble.instructions.os.replace', side_effect=OSError('disk full')):
            self.fast.respond(self.conn, [self.item(ident='failed-write')])
        self.assertEqual(instructions.snapshot()['text'], 'Saved in Companion while generating.')
        self.assertEqual(action_routing.current(self.conn)['status'], 'unconfirmed')
        self.assertIn("couldn't confirm", action_routing.current(self.conn)['reply'])

    def test_pause_before_execution_prevents_preference_save(self):
        item = self.item()
        with patch('ramble.fastlane.state.activity_allowed', side_effect=[True, True, False]), \
                patch.object(self.fast, 'run_codex', return_value=self.answer(self.edit())):
            self.fast.respond(self.conn, [item])
        self.assertEqual(instructions.snapshot()['text'], '')

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

    def test_two_processes_cannot_overwrite_the_same_saved_version(self):
        saved = instructions.save('Original preferences.')
        script = '''
import sys, time
from ramble import instructions
replace = instructions.os.replace
def delayed_replace(*args):
    time.sleep(0.1)
    return replace(*args)
instructions.os.replace = delayed_replace
try:
    instructions.save(sys.argv[2], version=sys.argv[1])
except instructions.ConflictError:
    print('conflict')
else:
    print('saved')
'''
        processes = [subprocess.Popen(
            [sys.executable, '-c', script, saved['version'], text],
            env={**os.environ, 'RAMBLE_HOME': str(self.home)},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for text in ('Saved from voice.', 'Saved from Companion.')]
        outcomes = []
        for process in processes:
            output, error = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, error)
            outcomes.append(output.strip())
        self.assertEqual(sorted(outcomes), ['conflict', 'saved'])

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

    def test_personal_context_and_installation_paths_reach_prompts(self):
        instructions.save("Call me Alex. Home is in Example City.")
        cfg = dict(self.cfg, work_session_root="/tmp/project space")
        prompt = config.render_prompt(
            "{{RAMBLE_REPO}}/bin/reminders {{COMPANION_REPO}}/bin/publish {{WORK_ROOT}}", cfg)
        self.assertIn("'/tmp/project space'", prompt)
        self.assertNotIn("{{", prompt)
        self.assertIn("Call me Alex. Home is in Example City.",
                      self.fast.build_prompt(self.conn, "Hello"))
        self.assertIn("Call me Alex. Home is in Example City.",
                      self.router.build_input(self.conn, self.thought))

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
