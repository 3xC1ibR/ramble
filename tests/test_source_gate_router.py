"""The source gate runs before Jev and Codex triage; isolated storage, no model calls."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ramble import config, db
from ramble.router import Router
from ramble.source_gate import SourceGate


class Profile:
    threshold = 0.2

    def score(self, e):
        return float(e[0])


class Embedder:
    def __init__(self, scores):
        self.scores = scores

    def embed(self, audio):
        return np.array([self.scores[audio]])


class RouterGateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-gate-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        paths = {"HOME": home, "DB_PATH": home / "ramble.db",
                 "STATE_PATH": home / "state.json", "CONFIG_PATH": home / "config.json"}
        paths.update({name: home / name.lower() for name in ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        patched = patch.multiple(config, **paths)
        patched.start()
        self.addCleanup(patched.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.now = time.time() - 600

    def router(self, scores, enabled):
        cfg = dict(config.load(), fast_enabled=False, notify=False, source_gate_triage=enabled)
        router = Router(cfg, threading.Event())
        router.source_gate = SourceGate(cfg, embedder=Embedder(scores), profile=Profile(),
                                        read_audio=lambda path: path)
        return router

    def thought(self, segments):
        self.conn.execute("INSERT INTO thoughts(id,started_at,ended_at,status,text) VALUES ('t1',?,?, 'closed', ?)",
                          (self.now, self.now + 30, " ".join(t for _, t in segments)))
        for i, (sid, text) in enumerate(segments):
            self.conn.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) "
                              "VALUES (?,?,?,3,?, 'transcribed', ?, 't1')",
                              (sid, self.now + 5 * i, self.now + 5 * i + 3, sid, text))
        return self.conn.execute("SELECT * FROM thoughts WHERE id='t1'").fetchone()

    def test_media_only_thought_skips_jev_and_codex(self):
        thought = self.thought([("s1", "Welcome back to the evening news at nine."),
                                ("s2", "Tonight, the city council votes on the budget.")])
        router = self.router({"s1": 0.02, "s2": 0.05}, enabled=True)
        with patch.object(router.triage_filter, "decide") as jev, patch.object(router, "run_triage") as codex:
            router.triage(self.conn, thought)
        jev.assert_not_called()
        codex.assert_not_called()
        row = self.conn.execute("SELECT status, classification, summary FROM thoughts WHERE id='t1'").fetchone()
        self.assertEqual((row[0], row[1]), ("done", "ambient"))
        self.assertIn("No owner speech", row[2])

    def test_owner_words_and_disabled_gate_keep_normal_triage(self):
        decision = {"classification": "log", "summary": "Noted.", "items": [], "notify": None}
        for scores, enabled in (({"s1": 0.6}, True), ({"s1": 0.0}, False)):
            with self.subTest(enabled=enabled):
                self.conn.execute("DELETE FROM thoughts")
                self.conn.execute("DELETE FROM segments")
                thought = self.thought([("s1", "Remember to compare the two e-ink tablets tomorrow.")])
                router = self.router(scores, enabled=enabled)
                with patch.object(router.triage_filter, "decide", return_value=None) as jev, \
                        patch.object(router, "run_triage", return_value=decision) as codex, \
                        patch("ramble.router.write_digest"):
                    router.triage(self.conn, thought)
                jev.assert_called_once()
                codex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
