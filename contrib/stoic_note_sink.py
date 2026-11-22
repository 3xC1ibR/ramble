#!/usr/bin/env python3
"""Store a Ramble transcript note in Stoic as a user-authored note (one note per day).

This adapter is not imported by Ramble. Configure it as the transcript route's
delivery command, for example in ~/.ramble/config.json:

    "transcript_route": {"enabled": true,
                         "deliver_command": ["python3", "/path/to/contrib/stoic_note_sink.py"]}

It reads the note JSON that ``ramble transcript-note --deliver`` writes to stdin,
creates the day's note on first delivery, and replaces its content on later
deliveries (PATCH with If-Match), so each day has exactly one note. Notes are
sent with ``"author": "user"``: the words are the owner's, and Stoic's memory
service (KES) indexes only user-authored notes. Ramble-written summaries are
never included.

Environment: STOIC_API_URL (default https://tryblume.ai), STOIC_API_TOKEN or
STOIC_API_TOKEN_FILE (default ~/.config/stoic/codex-token), and
STOIC_RAMBLE_NOTE_STATE (default ~/.config/stoic/ramble-note-ids.json), which
maps each external key to its Stoic note ID. ``--dry-run`` prints the request
instead of sending it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://tryblume.ai"


def read_token(env=os.environ) -> str:
    token = env.get("STOIC_API_TOKEN", "").strip()
    if token:
        return token
    path = Path(env.get("STOIC_API_TOKEN_FILE") or Path.home() / ".config/stoic/codex-token").expanduser()
    return path.read_text().strip()


class StoicNotes:
    def __init__(self, base_url: str, token: str, opener=urllib.request.urlopen):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.opener = opener

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
            "Accept": "application/json", **(headers or {})})
        with self.opener(req, timeout=30) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None, dict(resp.headers)

    def get(self, note_id: str):
        return self.request("GET", f"/api/notes/{urllib.parse.quote(note_id)}")

    def find_by_title(self, title: str) -> str | None:
        notes, _ = self.request("GET", "/api/notes?" + urllib.parse.urlencode({"search": title, "limit": 50}))
        for note in notes or []:
            if note.get("title") == title and not note.get("is_deleted"):
                return note["id"]
        return None

    def create(self, body: dict):
        return self.request("POST", "/api/notes", body)

    def replace(self, note_id: str, etag: str, body: dict):
        return self.request("PATCH", f"/api/notes/{urllib.parse.quote(note_id)}", body, {"If-Match": etag})


def note_body(note: dict) -> dict:
    if note.get("schema") != 1:
        raise ValueError(f"unsupported Ramble note schema {note.get('schema')!r}")
    return {"title": note["title"], "content": note["html"], "tags": note.get("tags") or ["ramble"],
            "author": "user"}


def upsert(client: StoicNotes, note: dict, state_path: Path) -> dict:
    body = note_body(note)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    note_id = state.get(note["external_key"]) or client.find_by_title(note["title"])
    if note_id:
        try:
            current, headers = client.get(note_id)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            current, note_id = None, None
        if current is not None and not current.get("is_deleted"):
            etag = headers.get("ETag") or headers.get("etag")
            if current.get("content") == body["content"] and current.get("title") == body["title"]:
                return {"action": "unchanged", "note_id": note_id}
            client.replace(note_id, etag, {k: body[k] for k in ("title", "content", "tags", "author")})
            action = "replaced"
        else:
            note_id = None
    if not note_id:
        created, _ = client.create(body)
        note_id, action = created["id"], "created"
    state[note["external_key"]] = note_id
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, state_path)
    return {"action": action, "note_id": note_id}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print the request; send nothing")
    a = p.parse_args(argv)
    note = json.load(sys.stdin)
    if a.dry_run:
        print(json.dumps({"method": "POST or PATCH", "path": "/api/notes", "body": note_body(note)}, indent=1))
        return 0
    client = StoicNotes(os.environ.get("STOIC_API_URL", DEFAULT_URL), read_token())
    state = Path(os.environ.get("STOIC_RAMBLE_NOTE_STATE")
                 or Path.home() / ".config/stoic/ramble-note-ids.json").expanduser()
    print(json.dumps(upsert(client, note, state)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
