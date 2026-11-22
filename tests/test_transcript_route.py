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


class NoteStrictnessTests(unittest.TestCase):
    def test_gate_owner_below_note_threshold_is_removed(self):
        c, cfg, gate = setup({"a": 0.6, "b": 0.22, "d": 0.5})
        thought(c, "t1", NOON, "log", [("a", "Plan the week before Monday.", 3),
                                       ("b", "A voice that only just passes the gate.", 4),
                                       ("d", "Then the budget.", 3)])
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertEqual(note["entries"][0]["text"],
                         "Plan the week before Monday. [other voice removed] Then the budget.")
        self.assertEqual(note["stats"]["reasons"]["below_note_threshold"], 1)

    def test_short_clips_need_a_verified_neighbour(self):
        c, cfg, gate = setup({"a": 0.6, "s1": 0.05, "x": 0.03, "s2": 0.04})
        thought(c, "t1", NOON, "log", [("a", "Order more coffee filters.", 3), ("s1", "Today.", 1.0),
                                       ("x", "Next on the evening news, the weather.", 6), ("s2", "Hey!", 1.0)])
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertEqual(note["entries"][0]["text"], "Order more coffee filters. Today.")  # trailing marker trimmed
        r = note["stats"]["reasons"]
        self.assertEqual((r["too_short_beside_owner"], r["too_short_alone"]), (1, 1))

    def test_thought_without_a_verified_owner_segment_is_excluded(self):
        c, cfg, gate = setup({"s1": 0.05, "s2": 0.1})
        thought(c, "t1", NOON, "log", [("s1", "Hang on.", 1.2), ("s2", "I have something for you.", 1.5)])
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertEqual(note["entries"], [])
        self.assertEqual(note["stats"]["segments_removed"], 2)

    def test_notes_fail_closed_without_a_profile(self):
        c, cfg, _ = setup({})
        thought(c, "t1", NOON, "dictation", [("a", "Draft the reply to the landlord.", 3)])
        gate = SourceGate({**cfg, "source_gate_profile": "/nonexistent/owner-voice.json"})
        note = transcript_route.build_day(c, DAY, cfg, gate)
        self.assertEqual(note["entries"], [])
        self.assertEqual(note["stats"]["reasons"], {"unverified": 1})


class SchedulerTests(unittest.TestCase):
    def run_at(self, home, hour, command, scores=None, ledger_days=()):
        c, cfg, gate = setup(scores or {"a": 0.6})
        thought(c, "t1", NOON, "log", [("a", "Book the dentist for next week.", 3)])
        cfg["transcript_route"] = {"deliver_command": command}
        clock = dt.datetime(2026, 11, 22, hour, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7))).timestamp()
        with patch.object(config, "HOME", Path(home)):
            return transcript_route.TranscriptNoteScheduler(cfg, None, gate=gate, clock=lambda: clock).run_once(c)

    def test_completed_days_are_delivered_once_after_the_delivery_hour(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            log = Path(home) / "calls.txt"
            cmd = [sys.executable, "-c", f"import json,sys; open({str(log)!r},'a').write(json.load(sys.stdin)['date']+'\\n')"]
            early = self.run_at(home, 1, cmd)
            self.assertFalse(any(r["delivered"] for r in early))
            self.assertTrue((Path(home) / "transcript-notes/2026-11-22.html").exists())
            first = self.run_at(home, 4, cmd)
            self.assertEqual([r["date"] for r in first if r["delivered"]], ["2026-11-21"])  # Nov 20 is empty
            again = self.run_at(home, 5, cmd)
            self.assertFalse(any(r["delivered"] for r in again))
            self.assertEqual(log.read_text().split(), ["2026-11-21"])
            ledger = json.loads((Path(home) / "transcript-notes/delivered.json").read_text())
            self.assertEqual(sorted(ledger), ["2026-11-21"])

    def test_days_without_kept_speech_create_no_note(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            results = self.run_at(home, 4, [sys.executable, "-c", "raise SystemExit(2)"], scores={"a": 0.01})
            self.assertFalse(any(r["delivered"] for r in results))

    def test_failed_delivery_is_retried(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as home:
            with self.assertRaises(RuntimeError):
                self.run_at(home, 4, [sys.executable, "-c", "raise SystemExit(2)"])
            self.assertFalse((Path(home) / "transcript-notes/delivered.json").exists())


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
