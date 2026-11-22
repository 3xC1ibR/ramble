import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from ramble import config, db, transcript_route
from ramble.source_gate import SourceGate

ZONE = "America/Denver"
DAY = dt.date(2026, 11, 21)
NOON = dt.datetime(2026, 11, 21, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7))).timestamp()


class Profile:
    threshold = 0.2

    def score(self, e):
        return float(e[0])


class Embedder:
    def __init__(self, scores):
        self.scores = scores

    def embed(self, audio):
        return np.array([self.scores[audio]])


def setup(scores):
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript(db.SCHEMA)
    cfg = {"timezone": ZONE, "whisper_initial_prompt": "Ramble, Stoic."}
    gate = SourceGate(cfg, embedder=Embedder(scores), profile=Profile(), read_audio=lambda p: p)
    return c, cfg, gate


def thought(c, tid, start, cls, segments):
    c.execute("INSERT INTO thoughts(id,started_at,ended_at,status,text,classification,summary) "
              "VALUES (?,?,?, 'done', '', ?, 'machine summary')", (tid, start, start + 60, cls))
    for i, (sid, text, dur) in enumerate(segments):
        c.execute("INSERT INTO segments(id,started_at,ended_at,duration,audio_path,status,text,thought_id) "
                  "VALUES (?,?,?,?,?, 'transcribed', ?, ?)", (sid, start + i * 5, start + i * 5 + dur, dur, sid, text, tid))


class BuildDayTests(unittest.TestCase):
    def test_keeps_owner_words_and_marks_removed_voices(self):
        c, cfg, gate = setup({"a": 0.6, "b": 0.05, "c": 0.04, "d": 0.7, "e": 0.01, "f": 0.6, "g": 0.6})
        thought(c, "t1", NOON, "log", [("a", "I think the reading view needs a <pause> button.", 3),
                                       ("b", "Welcome back to the evening news at nine.", 4),
                                       ("c", "Your job is on the line!", 3),
                                       ("d", "Also note that the dog had his walk.", 3)])
        thought(c, "t2", NOON + 600, "ambient", [("f", "This should not appear.", 3)])
        thought(c, "t3", NOON + 1200, "log", [("e", "Television only, start to finish.", 5)])
        thought(c, "t4", NOON + 86400, "log", [("g", "Next day.", 3)])
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertEqual(note["title"], "Ramble — 2026-11-21")
        self.assertEqual(note["external_key"], "ramble-transcripts/2026-11-21")
        self.assertEqual([e["thought_id"] for e in note["entries"]], ["t1"])
        self.assertEqual(note["entries"][0]["text"], "I think the reading view needs a <pause> button. "
                         "[other voice removed] Also note that the dog had his walk.")
        self.assertIn("&lt;pause&gt;", note["html"])
        self.assertNotIn("evening news", note["html"])
        self.assertNotIn("machine summary", note["html"])
        self.assertIn("<h3>12:00 PM</h3>", note["html"])
        s = note["stats"]
        self.assertEqual((s["thoughts_considered"], s["thoughts_included"]), (2, 1))
        self.assertEqual((s["segments_kept"], s["segments_removed"]), (2, 3))

    def test_artifacts_are_dropped_without_marker_and_dictation_is_labeled(self):
        c, cfg, gate = setup({"a": 0.5})
        thought(c, "t1", NOON, "dictation", [("a", "Draft the reply to the landlord.", 3),
                                             ("z", "And the 1. " * 30, 9)])
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertEqual(note["entries"][0]["text"], "Draft the reply to the landlord.")
        self.assertIn("12:00 PM · dictation", note["html"])

    def test_empty_day(self):
        c, cfg, gate = setup({})
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertIn("No speech was kept", note["html"])


class DeliveryTests(unittest.TestCase):
    def note(self):
        return {"schema": 1, "date": "2026-11-21", "title": "Ramble — 2026-11-21", "html": "<p>x</p>",
                "external_key": "ramble-transcripts/2026-11-21", "stats": {}}

    def test_dry_run_writes_files_and_never_runs_the_command(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as home, patch.object(config, "HOME", Path(home)), \
                patch("ramble.transcript_route.subprocess.run") as run:
            cfg = {"transcript_route": {"deliver_command": ["false"]}}
            result = transcript_route.deliver(self.note(), cfg, live=False)
            run.assert_not_called()
            self.assertEqual(result["reason"], "dry_run")
            self.assertEqual((Path(home) / "transcript-notes/2026-11-21.html").read_text(), "<p>x</p>\n")
            self.assertEqual(json.loads((Path(home) / "transcript-notes/2026-11-21.json").read_text())["title"],
                             "Ramble — 2026-11-21")

    def test_live_delivery_sends_json_on_stdin(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as home, patch.object(config, "HOME", Path(home)):
            echo = [sys.executable, "-c", "import json,sys; print(json.load(sys.stdin)['external_key'])"]
            result = transcript_route.deliver(self.note(), {"transcript_route": {"deliver_command": echo}}, live=True)
            self.assertTrue(result["delivered"])
            self.assertEqual(result["output"], "ramble-transcripts/2026-11-21")
            fail = {"transcript_route": {"deliver_command": [sys.executable, "-c", "raise SystemExit(3)"]}}
            with self.assertRaises(RuntimeError):
                transcript_route.deliver(self.note(), fail, live=True)


if __name__ == "__main__":
    unittest.main()
