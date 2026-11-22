import sqlite3
import unittest

import numpy as np

from ramble import db, source_gate
from ramble.source_gate import SourceGate, judge, is_artifact

CFG = {"whisper_initial_prompt": "Ramble, Ribbon Field, Stoic, ElevenLabs."}


def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    return c


def add(c, sid, thought, text, duration=3.0, start=1000.0):
    c.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) "
              "VALUES (?,?,?,?,?, 'transcribed', ?, ?)", (sid, start, start + duration, duration, sid, text, thought))


class FixedProfile:
    threshold = 0.2

    def __init__(self, scores):
        self.scores = scores

    def score(self, embedding):
        return float(embedding[0])


class FixedEmbedder:
    def __init__(self, scores):
        self.scores = scores

    def embed(self, audio):
        return np.array([self.scores[audio]])


def gate(scores, **cfg):
    return SourceGate({**CFG, **cfg}, embedder=FixedEmbedder(scores), profile=FixedProfile(scores),
                      read_audio=lambda path: path)


class RuleTests(unittest.TestCase):
    def v(self, **kw):
        args = dict(text="I need to call the bank tomorrow.", duration=3.0, score=0.5, threshold=0.2,
                    playback=None, cfg=CFG)
        args.update(kw)
        return judge(**args)

    def test_rule_order(self):
        self.assertEqual(self.v().reason, "owner_verified")
        self.assertEqual(self.v(score=None).reason, "unverified")
        self.assertTrue(self.v(score=None).keep)
        self.assertEqual(self.v(score=0.05).reason, "other_voice")
        self.assertEqual(self.v(score=0.05, duration=1.2).reason, "too_short_to_judge")
        self.assertEqual(self.v(score=0.05, duration=1.2, playback=0.8).reason, "external_playback")
        self.assertEqual(self.v(score=0.5, playback=1.0).reason, "owner_verified")  # owner over music
        self.assertEqual(self.v(text="Stoic, ElevenLabs.").reason, "transcription_artifact")

    def test_artifacts(self):
        self.assertTrue(is_artifact("And the 1. " * 30, CFG))
        self.assertTrue(is_artifact("Ramble.", CFG))
        self.assertTrue(is_artifact("   ", CFG))
        self.assertFalse(is_artifact("Ramble, remind me to buy coffee.", CFG))
        self.assertFalse(is_artifact("We can get it, we can do this, and then we ship on Friday.", CFG))


class GateTests(unittest.TestCase):
    def test_verdicts_are_cached_and_unverified_is_retried(self):
        c = conn()
        add(c, "s1", "t1", "Let me write that down for later.")
        g = gate({"s1": 0.05})
        seg = c.execute("SELECT * FROM segments WHERE id='s1'").fetchone()
        self.assertEqual(g.verdict(c, seg).reason, "other_voice")
        g.embedder = None  # a cached verdict needs no model
        self.assertEqual(g.verdict(c, seg).reason, "other_voice")
        c.execute("DELETE FROM segment_sources")
        g.profile = None
        self.assertEqual(g.verdict(c, seg).reason, "unverified")
        self.assertIsNone(c.execute("SELECT 1 FROM segment_sources").fetchone())

    def test_scoring_errors_fail_open(self):
        c = conn()
        add(c, "s1", "t1", "Let me write that down for later.")
        g = gate({})  # KeyError inside embed
        seg = c.execute("SELECT * FROM segments").fetchone()
        v = g.verdict(c, seg)
        self.assertTrue(v.keep)
        self.assertEqual(v.reason, "unverified")

    def test_missing_profile_fails_open(self):
        c = conn()
        add(c, "s1", "t1", "Let me write that down for later.")
        g = SourceGate({**CFG, "source_gate_profile": "/nonexistent/owner-voice.json"})
        self.assertEqual(g.verdict(c, c.execute("SELECT * FROM segments").fetchone()).reason, "unverified")

    def test_playback_record_removes_short_low_scoring_clip(self):
        c = conn()
        add(c, "s1", "t1", "Hang on, I have something for you.", duration=1.5, start=1000.0)
        c.execute(source_gate.output_activity.SCHEMA)
        for t in range(998, 1004):
            c.execute("INSERT INTO output_activity VALUES (?,1,'4242')", (float(t),))
        v = gate({"s1": 0.03}).verdict(c, c.execute("SELECT * FROM segments").fetchone())
        self.assertEqual(v.reason, "external_playback")


class TriageDecisionTests(unittest.TestCase):
    def test_disabled_by_default(self):
        c = conn()
        add(c, "s1", "t1", "Did you see the game last night?")
        self.assertIsNone(source_gate.triage_decision(c, {"id": "t1"}, gate({"s1": 0.0})))

    def test_no_owner_words_gives_local_ambient_decision(self):
        c = conn()
        add(c, "s1", "t1", "Did you see the game last night?")
        add(c, "s2", "t1", "And the 1. " * 30, start=1004.0)
        d = source_gate.triage_decision(c, {"id": "t1"}, gate({"s1": 0.0}, source_gate_triage=True))
        self.assertEqual(d["classification"], "ambient")
        self.assertEqual(d["items"], [])
        self.assertIn("other_voice", d["summary"])

    def test_any_owner_words_keep_full_triage(self):
        c = conn()
        add(c, "s1", "t1", "Did you see the game last night?")
        add(c, "s2", "t1", "Ramble, research the best e-ink tablets.", start=1004.0)
        self.assertIsNone(source_gate.triage_decision(c, {"id": "t1"},
                                                      gate({"s1": 0.0, "s2": 0.7}, source_gate_triage=True)))


if __name__ == "__main__":
    unittest.main()
