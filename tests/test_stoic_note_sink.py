import importlib.util
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "contrib" / "stoic_note_sink.py"
spec = importlib.util.spec_from_file_location("stoic_note_sink", PATH)
sink = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sink)

NOTE = {"schema": 1, "date": "2026-11-21", "title": "Ramble — 2026-11-21", "html": "<p>Owner words.</p>",
        "external_key": "ramble-transcripts/2026-11-21", "tags": ["ramble", "transcript"]}


class FakeClient:
    def __init__(self, notes=None, missing=False):
        self.notes = notes or {}
        self.calls = []
        self.missing = missing

    def find_by_title(self, title):
        self.calls.append(("find", title))
        return next((k for k, v in self.notes.items() if v["title"] == title), None)

    def get(self, note_id):
        self.calls.append(("get", note_id))
        if self.missing:
            raise urllib.error.HTTPError("u", 404, "gone", {}, None)
        return dict(self.notes[note_id]), {"ETag": '"v1"'}

    def create(self, body):
        self.calls.append(("create", body))
        self.notes["n-new"] = dict(body)
        return {"id": "n-new"}, {}

    def replace(self, note_id, etag, body):
        self.calls.append(("replace", note_id, etag, body))
        self.notes[note_id].update(body)
        return {}, {}


class SinkTests(unittest.TestCase):
    def test_creates_user_authored_note_and_records_its_id(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            state = Path(d) / "ids.json"
            client = FakeClient()
            self.assertEqual(sink.upsert(client, NOTE, state), {"action": "created", "note_id": "n-new"})
            body = client.calls[-1][1]
            self.assertEqual(body["author"], "user")
            self.assertNotIn("agent_provenance", body)
            self.assertEqual(body["content"], "<p>Owner words.</p>")
            self.assertEqual(json.loads(state.read_text()), {"ramble-transcripts/2026-11-21": "n-new"})

    def test_replaces_the_same_day_with_if_match_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            state = Path(d) / "ids.json"
            state.write_text(json.dumps({NOTE["external_key"]: "n1"}))
            client = FakeClient({"n1": {"title": NOTE["title"], "content": "<p>old</p>"}})
            self.assertEqual(sink.upsert(client, NOTE, state)["action"], "replaced")
            op, note_id, etag, body = client.calls[-1]
            self.assertEqual((op, note_id, etag, body["author"]), ("replace", "n1", '"v1"', "user"))
            self.assertEqual(sink.upsert(client, NOTE, state)["action"], "unchanged")

    def test_deleted_or_missing_note_is_recreated(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            state = Path(d) / "ids.json"
            state.write_text(json.dumps({NOTE["external_key"]: "n1"}))
            self.assertEqual(sink.upsert(FakeClient(missing=True), NOTE, state)["action"], "created")
            state.write_text(json.dumps({NOTE["external_key"]: "n1"}))
            client = FakeClient({"n1": {"title": NOTE["title"], "content": "x", "is_deleted": True}})
            self.assertEqual(sink.upsert(client, NOTE, state)["action"], "created")

    def test_rejects_unknown_schema(self):
        with self.assertRaises(ValueError):
            sink.note_body({**NOTE, "schema": 2})


if __name__ == "__main__":
    unittest.main()
