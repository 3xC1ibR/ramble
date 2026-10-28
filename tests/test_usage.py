"""Usage evidence, accounting, and provider hooks without model requests."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from ramble import config, usage
from ramble.fast_trace import FastTrace
from ramble.jev import JevClient
from ramble.router import Router


class UsageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-usage-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.enterContext(patch.object(config, "HOME", self.home))
        self.store = usage.UsageStore()

    def record(self, identity="one", **overrides):
        fields = dict(started_at=time.time(), purpose="reply", provider="codex", model="test-model",
                      counts={"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 20,
                              "reasoning_output_tokens": 10})
        fields.update(overrides)
        self.store.record(identity, **fields)

    def test_cache_and_reasoning_are_subsets_not_extra_tokens(self):
        self.record()
        result = self.store.report()
        self.assertEqual(result["totals"]["counts"]["total_tokens"], 120)
        self.assertEqual(result["groups"][0]["model"], "test-model")
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_claude_cache_splits_are_normalized_once(self):
        counts = usage.normalize(dict(input_tokens=12, cache_read_input_tokens=30,
                                      cache_creation_input_tokens=8, output_tokens=5), "claude")
        self.assertEqual(counts, dict(input_tokens=50, cached_input_tokens=30, output_tokens=5, total_tokens=55))
        self.record(counts=counts, provider="claude")
        self.assertEqual(self.store.report()["totals"]["counts"]["total_tokens"], 55)
        self.assertNotIn("input_tokens", usage.normalize(dict(input_tokens=12, output_tokens=5), "claude"))

    def test_invalid_counters_and_unknown_calls_never_become_zero(self):
        self.assertEqual(usage.normalize(dict(input_tokens=True, output_tokens=-5, total_tokens="20")), {})
        self.record(counts={}, outcome="failed")
        result = self.store.report()["totals"]
        self.assertEqual(result["counts"], {})
        self.assertEqual(result["measured"], 0)
        self.assertEqual(result["missing"]["total_tokens"], 1)
        with self.assertRaises(ValueError):
            self.record(started_at=float("nan"))

    def test_refresh_replaces_snapshot_but_retries_are_separate(self):
        self.record(request_id="request")
        self.record(counts={"input_tokens": 200, "output_tokens": 20})
        self.record("retry", request_id="request", outcome="failed", counts={})
        result = self.store.report()
        self.assertEqual(result["call_count"], 2)
        self.assertEqual(result["totals"]["counts"]["total_tokens"], 220)
        self.assertEqual(result["totals"]["missing"]["total_tokens"], 1)

    def test_date_filters_exclude_old_calls_and_keep_unknowns(self):
        self.record(started_at=time.time() - 10 * 86400)
        self.record("new", counts={})
        self.assertEqual(self.store.report(7)["call_count"], 1)
        self.assertEqual(self.store.report(None)["call_count"], 2)

    def test_live_fast_trace_and_recovery_are_idempotent_and_private(self):
        trace = FastTrace(self.home / "triage/fast", request_id="request", purpose="reply", model="test-model")
        initial = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "SECRET PROMPT"}}) + "\n"
        trace.consume(initial, "")
        self.store.recover()
        self.assertEqual(self.store.report()["calls"][0]["coverage"], "unknown")
        stream = json.dumps({"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 40}}) + "\n"
        trace.consume(initial + stream, "")
        trace.finish("completed")
        usage.capture_fast(trace.path)
        self.store.recover()
        self.store.recover()
        result = self.store.report()
        self.assertEqual(result["call_count"], 1)
        self.assertEqual(result["totals"]["counts"]["total_tokens"], 540)
        self.assertNotIn(b"SECRET PROMPT", self.store.path.read_bytes())

    def test_legacy_audits_are_partial_and_bad_files_do_not_break_recovery(self):
        folder = self.home / "triage"
        folder.mkdir()
        (folder / "old.json").write_text(json.dumps({"backend": "codex", "returncode": 0,
                                                    "cmd": ["codex", "-m", "legacy-model"], "stderr": "tokens used\n1,234\n"}))
        (folder / "invalid.json").write_text("[]")
        (folder / "filter.jev.json").write_text("not imported")
        self.assertEqual(self.store.recover(), 1)
        result = self.store.report()
        self.assertEqual(result["call_count"], 1)
        self.assertEqual(result["calls"][0]["coverage"], "partial")
        self.assertEqual(result["totals"]["counts"], {"total_tokens": 1234})

    def test_triage_retries_survive_overwritten_audit_and_collect_json_usage(self):
        folder = self.home / "triage"
        folder.mkdir()
        policy = self.home / "policy.md"
        policy.write_text("Test policy")
        self.enterContext(patch.multiple(config, TRIAGE_DIR=folder, TRIAGE_PROMPT=policy))
        router = Router(dict(config.DEFAULTS, triage_cwd=str(self.home)), threading.Event())
        def complete(cmd, **kwargs):
            self.assertIn("--json", cmd)
            self.assertIn("--ignore-user-config", cmd)
            Path(cmd[cmd.index("-o") + 1]).write_text('{"summary":"ok"}')
            return Mock(returncode=0, stdout=json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 10}}), stderr="")
        with patch("ramble.router.subprocess.run", side_effect=complete):
            router.run_codex("SECRET PROMPT", "same-thought")
            router.run_codex("SECRET PROMPT", "same-thought")
        self.store.recover()
        result = self.store.report()
        self.assertEqual(result["call_count"], 2)
        self.assertEqual(result["totals"]["counts"]["total_tokens"], 120)
        self.assertNotIn(b"SECRET PROMPT", self.store.path.read_bytes())

    def test_jev_missing_usage_is_an_attributed_unknown(self):
        with patch.dict(os.environ, {"JEV_API_KEY": "test-only"}):
            client = JevClient({})
        response = {"model": "jev-1.13.0", "answers": {"directed": {"type": "noul", "noul": .9}, "related": {"type": "noul", "noul": .9}}}
        with patch.object(client, "_post", return_value=response):
            client.classify([], "Continue?", 110)
        row = self.store.report()["calls"][0]
        self.assertEqual((row["purpose"], row["provider"], row["coverage"]), ("chat_followup", "jev", "unknown"))

    def test_linked_lifetime_snapshots_deduplicate_replace_and_stay_separate(self):
        with sqlite3.connect(self.home / "ramble.db") as conn:
            for table in ("work_sessions", "dispatches"):
                conn.execute(f"CREATE TABLE {table}(thread_id TEXT,name TEXT,created_at REAL)")
                conn.execute(f"INSERT INTO {table} VALUES ('thread','Test session',1)")
        fetch = Mock(return_value={"provider": "codex", "status": "partial", "scope": "direct", "totals": {"input_tokens": 200, "output_tokens": 30}, "models": [], "prompt": "SECRET"})
        self.store.refresh_linked(fetch)
        self.store.refresh_linked(fetch)
        fetch.assert_called_once_with("thread")
        result = self.store.report()
        self.assertEqual(result["totals"]["counts"], {})
        self.assertEqual(len(result["linked"]), 1)
        self.assertNotIn("prompt", result["linked"][0]["snapshot"])
        with self.store.connect() as conn:
            conn.execute("UPDATE linked SET checked_at=0")
        fetch.return_value["totals"]["input_tokens"] = 300
        self.store.refresh_linked(fetch)
        self.assertEqual(self.store.report()["linked"][0]["snapshot"]["totals"]["total_tokens"], 330)
        with self.store.connect() as conn:
            conn.execute("UPDATE linked SET checked_at=0")
        self.store.refresh_linked(Mock(side_effect=OSError("offline")))
        row = self.store.report()["linked"][0]
        self.assertEqual(row["snapshot"]["totals"]["total_tokens"], 330)
        self.assertIsNotNone(row["error"])

    def test_recording_failure_cannot_break_provider_calls(self):
        with patch("ramble.usage.UsageStore", side_effect=OSError("locked")), self.assertLogs("ramble.usage", level="WARNING"):
            usage.record_call("call", started_at=1, purpose="reply", provider="codex")
            usage.capture_fast(self.home / "missing")


if __name__ == "__main__":
    unittest.main()
