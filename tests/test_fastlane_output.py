"""Reply-file races and cleanup, with no real model processes or playback."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from ramble import config
from ramble.fastlane import FastLane, REPLY_SCHEMA, FOLLOWUP_SCHEMA


class FastOutputTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.work = Path(self.home.name) / "triage"
        self.enterContext(patch.object(config, "HOME", Path(self.home.name)))
        self.enterContext(patch.object(config, "TRIAGE_DIR", self.work))
        self.fast = FastLane(dict(config.DEFAULTS, fast_speak=False, dispatch_daily_cap=0))

    def test_simultaneous_replies_in_same_second_keep_their_own_results(self):
        ready = threading.Barrier(2)
        paths = []

        def model(cmd, *, input, **kwargs):
            out = Path(cmd[cmd.index("-o") + 1])
            schema = Path(cmd[cmd.index("--output-schema") + 1])
            paths.append(out)
            self.assertEqual(json.loads(schema.read_text()), REPLY_SCHEMA)
            out.write_text(json.dumps({"reply": input, "handoff": False}))
            # Both writes finish before either caller can read or clean up.
            ready.wait(timeout=5)
            return subprocess.CompletedProcess(cmd, 0, stderr="")

        with patch("ramble.fastlane.codex_runner.run", side_effect=model), \
                patch("ramble.fastlane.time.time", return_value=1000), \
                ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.fast.run_codex, ("first", "second")))
        self.assertEqual([r["reply"] for r in results], ["first", "second"])
        self.assertEqual(len(set(paths)), 2)
        self.assertEqual(list((self.work / "tmp").iterdir()), [])

    def test_timeout_cleans_partial_result(self):
        def timeout(cmd, **kwargs):
            Path(cmd[cmd.index("-o") + 1]).write_text("partial")
            raise subprocess.TimeoutExpired(cmd, 1)

        with patch("ramble.fastlane.codex_runner.run", side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.fast.run_codex("request")
        self.assertEqual(list((self.work / "tmp").iterdir()), [])
        traces = list((self.work / 'fast').glob('*.jsonl'))
        self.assertEqual(len(traces), 1)
        rows = [json.loads(line) for line in traces[0].read_text().splitlines()]
        self.assertEqual(rows[-1]['outcome'], 'timeout')
        self.assertNotIn('partial', traces[0].read_text())

    def test_deadline_tracks_live_reasoning_choice_and_matches_trace(self):
        seen = []

        def model(cmd, **kwargs):
            seen.append(kwargs['timeout'])
            self.assertIn('model_reasoning_effort="' + effort + '"', cmd)
            Path(cmd[cmd.index('-o') + 1]).write_text('{"reply":"Ready","handoff":false}')
            return subprocess.CompletedProcess(cmd, 0, stderr='')

        # The same FastLane object observes settings saved between requests.
        for effort, expected in [('low', 30), ('medium', 60), ('high', 90), ('xhigh', 120), ('max', 120), ('ultra', 900)]:
            with self.subTest(effort=effort):
                (config.HOME / 'config.json').write_text(json.dumps({
                    'fast_model': 'selected-model', 'fast_effort': effort,
                    'dispatch_daily_cap': 0, 'fast_speak': False, 'speak_questions': False,
                }))
                with patch('ramble.fastlane.codex_runner.run', side_effect=model):
                    self.fast.run_codex('request', request_id=effort)
                self.assertEqual(seen[-1], expected)
                rows = [json.loads(p.read_text().splitlines()[0]) for p in (self.work / 'fast').glob('*.jsonl')]
                trace = next(r for r in rows if r['request_id'] == effort)
                self.assertEqual(trace['timeout_s'], expected)
                self.assertEqual(trace['model'], 'selected-model')

    def test_explicit_deadline_is_authoritative_even_at_ultra_effort(self):
        self.fast.cfg.update(fast_timeout_s=.25, fast_effort='ultra')
        with patch('ramble.fastlane.codex_runner.run', side_effect=subprocess.TimeoutExpired('codex', .25)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.fast.run_codex('request')
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['timeout'], .25)

    def test_invalid_deadline_never_starts_process(self):
        for value in (0, -1, float('inf'), float('nan'), True, '120'):
            with self.subTest(value=value):
                self.fast.cfg['fast_timeout_s'] = value
                with patch('ramble.fastlane.codex_runner.run') as run:
                    with self.assertRaises(ValueError):
                        self.fast.run_codex('request')
                run.assert_not_called()

    def test_candidate_classifier_is_read_only_ephemeral_and_strict(self):
        def model(cmd, **kwargs):
            self.assertIn('--ephemeral', cmd)
            self.assertIn('--ignore-user-config', cmd)
            self.assertEqual(cmd[cmd.index('--disable') + 1], 'hooks')
            self.assertEqual(cmd[cmd.index('--sandbox') + 1], 'read-only')
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', cmd)
            schema = json.loads(Path(cmd[cmd.index('--output-schema') + 1]).read_text())
            self.assertEqual(schema, FOLLOWUP_SCHEMA)
            self.assertEqual(set(schema['properties']), set(schema['required']))
            self.assertFalse(schema['additionalProperties'])
            Path(cmd[cmd.index('-o') + 1]).write_text('{"accept":false}')
            return subprocess.CompletedProcess(cmd, 0, stderr='')
        with patch('ramble.fastlane.codex_runner.run', side_effect=model):
            self.assertEqual(self.fast.run_codex('ambient', schema=FOLLOWUP_SCHEMA, read_only=True), {'accept': False})

    def test_failed_process_and_missing_output_are_reported_and_cleaned(self):
        for code in (0, 1):
            with self.subTest(code=code), patch("ramble.fastlane.codex_runner.run", return_value=
                    subprocess.CompletedProcess([], code, stderr="failure detail")):
                with self.assertRaisesRegex(RuntimeError, f"Codex exited {code} without a usable reply"):
                    self.fast.run_codex("request")
                self.assertEqual(list((self.work / "tmp").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
