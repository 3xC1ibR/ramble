"""Shared settings and real invocation arguments; no agents, audio or mic."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ramble import config, model_settings
from ramble.fastlane import FastLane
from ramble.router import Router
from ramble.session_backend import SessionBackend

MODELS = [
    {'id': 'test-fast', 'label': 'Fast', 'efforts': ['low', 'high'], 'default_effort': 'low', 'is_default': True},
    {'id': 'test-deep', 'label': 'Deep', 'efforts': ['medium', 'high'], 'default_effort': 'medium', 'is_default': False},
]


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ramble-test-models-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        self.path = self.home / 'config.json'
        self.original = {'dispatch_daily_cap': 0, 'fast_speak': False, 'speak_questions': False,
                         'tts_voice': 'custom', 'unknown_option': {'keep': True}}
        self.path.write_text(json.dumps(self.original))
        patches = patch.multiple(config, HOME=self.home, CONFIG_PATH=self.path,
                                 TRIAGE_DIR=self.home / 'triage')
        patches.start(); self.addCleanup(patches.stop)
        discover = patch.object(model_settings, 'catalog', return_value=MODELS)
        discover.start(); self.addCleanup(discover.stop)

    def changed(self):
        saved = model_settings.snapshot()
        settings = copy.deepcopy(saved['settings'])
        settings.update(fast={'model':'test-fast', 'effort':'low'},
                        triage={'model':'test-deep', 'effort':'high'},
                        work={'model':'test-deep', 'effort':'medium'})
        return saved, settings

    def test_save_persists_preserves_other_keys_and_never_touches_capture(self):
        before, settings = self.changed()
        saved = model_settings.save(settings, version=before['version'])
        self.assertEqual(saved, model_settings.snapshot())
        self.assertEqual(saved['settings'], settings)
        for key, value in self.original.items():
            self.assertEqual(json.loads(self.path.read_text())[key], value)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / 'state.json').exists())
        child = subprocess.run([__import__('sys').executable, '-c',
            'from ramble.config import load; print(load()["fast_model"])'],
            env={**__import__('os').environ, 'RAMBLE_HOME':str(self.home)}, capture_output=True, text=True, check=True)
        self.assertEqual(child.stdout.strip(), 'test-fast')

    def test_invalid_combo_stale_editor_and_failed_write_leave_file_intact(self):
        before, settings = self.changed()
        raw = self.path.read_bytes()
        invalid = copy.deepcopy(settings); invalid['fast']['effort'] = 'ultra'
        with self.assertRaises(ValueError):
            model_settings.save(invalid, version=before['version'])
        self.assertEqual(self.path.read_bytes(), raw)
        with patch.object(model_settings.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                model_settings.save(settings, version=before['version'])
        self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(list(self.home.glob('.model-settings-*')), [])
        model_settings.save(settings, version=before['version'])
        with self.assertRaises(model_settings.ConflictError):
            model_settings.save(settings, version=before['version'])

    def test_concurrent_devices_cannot_overwrite_newer_save(self):
        before, settings = self.changed()
        outcomes = []
        def write():
            try:
                model_settings.save(settings, version=before['version'])
                outcomes.append('saved')
            except model_settings.ConflictError:
                outcomes.append('conflict')
        threads = [threading.Thread(target=write) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ['saved', 'conflict'])

    def test_claude_custom_selection_is_preserved_and_not_overwritten(self):
        self.path.write_text(json.dumps({**self.original, 'triage_backend':'claude', 'triage_model':'sonnet'}))
        saved = model_settings.snapshot()
        settings = copy.deepcopy(saved['settings']); settings['fast'] = {'model':'test-fast','effort':'high'}
        model_settings.save(settings, version=saved['version'])
        saved = model_settings.snapshot(); settings['triage'] = {'model':'test-deep','effort':'high'}
        with self.assertRaisesRegex(ValueError, 'Claude'):
            model_settings.save(settings, version=saved['version'])
        self.assertEqual(json.loads(self.path.read_text())['triage_model'], 'sonnet')

    def test_live_consumers_use_new_pair_without_recreating_workers(self):
        cfg = dict(config.DEFAULTS)
        fast = FastLane(cfg)
        router = Router(cfg, threading.Event())
        backend = SessionBackend(cfg); backend.caller = 'fake-caller'
        before, settings = self.changed()
        model_settings.save(settings, version=before['version'])
        (self.home / 'triage').mkdir()
        seen = []
        def run(cmd, **kw):
            seen.append(cmd)
            Path(cmd[cmd.index('-o') + 1]).write_text('{}')
            return subprocess.CompletedProcess(cmd, 0, '{}', '')
        with patch('ramble.fastlane.codex_runner.run', side_effect=run):
            fast.run_codex('test')
        with patch('ramble.router.subprocess.run', side_effect=run):
            router.run_codex('test', 'thought')
        with patch.object(backend, 'call', side_effect=lambda args, text=None: seen.append(args)):
            backend.create('fake-request', {'cwd':'/tmp','name':'Fake','prompt':'Test'})
        for cmd, pair in zip(seen, [settings['fast'], settings['triage'], settings['work']]):
            self.assertIn(pair['model'], cmd)
            if '--effort' in cmd:
                self.assertEqual(cmd[cmd.index('--effort') + 1], pair['effort'])
            else:
                self.assertIn('model_reasoning_effort="' + pair['effort'] + '"', cmd)
        for cmd in seen[:2]:
            self.assertTrue({'--ephemeral','--ignore-user-config','--disable','hooks'} <= set(cmd))
        saved = model_settings.snapshot(); settings['work'] = {'model':None,'effort':None}
        model_settings.save(settings, version=saved['version'])
        with patch.object(backend, 'call', return_value={}) as call:
            backend.create('other-fake-request', {'cwd':'/tmp','name':'Fake','prompt':'Test'})
        self.assertNotIn('--model', call.call_args.args[0])
        self.assertNotIn('--effort', call.call_args.args[0])

    def test_session_pair_overrides_defaults_without_saving_and_backend_keeps_it(self):
        before, settings = self.changed()
        model_settings.save(settings, version=before['version'])
        raw = self.path.read_bytes()
        selected = model_settings.work_selection(config.DEFAULTS, model='test-fast', effort='high')
        self.assertEqual(selected, {'model': 'test-fast', 'effort': 'high'})
        self.assertEqual(model_settings.work_selection(config.DEFAULTS, model='test-fast'),
                         {'model': 'test-fast', 'effort': None})
        self.assertEqual(model_settings.work_selection(config.DEFAULTS, effort='high'),
                         {'model': 'test-deep', 'effort': 'high'})
        self.assertEqual(self.path.read_bytes(), raw)
        backend = SessionBackend(config.DEFAULTS); backend.caller = 'fake-caller'
        with patch.object(backend, 'call', return_value={}) as call:
            backend.create('frozen', {'cwd': '/tmp', 'name': 'Fake', 'prompt': 'Test', **selected})
        args = call.call_args.args[0]
        self.assertEqual(args[args.index('--model') + 1], 'test-fast')
        self.assertEqual(args[args.index('--effort') + 1], 'high')

    def test_effort_alone_pins_provider_default_and_rejects_invalid_pairs(self):
        self.assertEqual(model_settings.work_selection(config.DEFAULTS, effort='high'),
                         {'model': 'test-fast', 'effort': 'high'})
        for pair in ({'model': 'unknown'}, {'model': 'test-fast', 'effort': 'ultra'},
                     {'effort': 'ultra'}):
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                model_settings.work_selection(config.DEFAULTS, **pair)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        caller = patch.dict(model_settings.os.environ, {'AGENT_COORD_SESSION_ID':'fake-caller'})
        caller.start(); self.addCleanup(caller.stop)

    def test_launchd_uses_installed_launcher_without_shell_path(self):
        receipt = {'status':'completed','result':{'models':[{
            'id':'test','supportedReasoningEfforts':[{'reasoningEffort':'low'}]
        }]}}
        temp = tempfile.TemporaryDirectory(prefix='ramble-test-catalog-', dir='/tmp')
        self.addCleanup(temp.cleanup)
        home = Path(temp.name)
        (home / 'agent_coord_session').write_text('fake-dispatcher')
        with patch.object(model_settings, '_cached_catalog', (0, None)), \
             patch.dict(model_settings.os.environ, {'PATH':'/usr/bin:/bin'}, clear=True), \
             patch.object(config, 'HOME', home), \
             patch.object(Path, 'is_file', return_value=True), \
             patch.object(model_settings.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps(receipt),'')) as run:
            model_settings.catalog()
            self.assertEqual(run.call_args.args[0][0], str(Path.home() / '.local/bin/agent-coord'))
            self.assertIn('--from-session', run.call_args.args[0])
            self.assertIn('fake-dispatcher', run.call_args.args[0])

    def test_provider_catalog_and_failed_discovery(self):
        receipt = {'status':'completed','result':{'models':[
            {'id':'test','displayName':'Model','supportedReasoningEfforts':[{'reasoningEffort':'high'}], 'defaultReasoningEffort':'high'},
            {'id':'secret','hidden':True}
        ]}}
        with patch.object(model_settings, '_cached_catalog', (0, None)), patch.object(model_settings.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,json.dumps(receipt),'')) as run:
            self.assertEqual(model_settings.catalog()[0]['efforts'], ['high'])
            self.assertEqual(len(model_settings.catalog()), 1)
            self.assertEqual(run.call_count, 1)
            self.assertIn('models', run.call_args.args[0])
        with patch.object(model_settings, '_cached_catalog', (0, None)), patch.object(model_settings.subprocess, 'run', side_effect=subprocess.TimeoutExpired([],15)):
            with self.assertRaisesRegex(OSError, 'unavailable'):
                model_settings.catalog()
