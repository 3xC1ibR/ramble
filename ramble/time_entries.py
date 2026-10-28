"""Toggl timer actions through Stoic; credentials stay on their service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

from .reminders import IDENTIFIER, ReminderError, Reminders, UncertainError

API = "https://tryblume.ai/api/integrations/toggl/current"
ACTION_SCHEMA = {
    "type": ["object", "null"], "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["current", "update", "start"]},
        "entry_id": {"type": ["integer", "null"], "description": "Observed running entry ID from current; for start, null only when no timer is running."},
        "workspace_id": {"type": ["integer", "null"], "description": "For start, exact workspace ID from time-entry workspaces; otherwise null."},
        "description": {"type": ["string", "null"], "description": "New description; null preserves it."},
        "project_id": {"type": ["integer", "null"], "description": "Exact Toggl project ID from current lookup; null preserves it."},
        "clear_project": {"type": "boolean", "description": "True only when explicitly asked to remove the project."},
    },
}
ACTION_SCHEMA["required"] = list(ACTION_SCHEMA["properties"])


class TimeEntries(Reminders):
    def request(self, method="GET", body=None, *, path="current"):
        try:
            token = os.environ.get("STOIC_API_TOKEN") or Path(os.environ.get(
                "STOIC_API_TOKEN_FILE", str(Path.home() / ".config/stoic/codex-token"))).read_text().strip()
        except OSError:
            raise ReminderError("Connect Stoic on this Mac before managing time entries.") from None
        if not token:
            raise ReminderError("Connect Stoic on this Mac before managing time entries.")
        req = urllib.request.Request(API.rsplit("/", 1)[0] + "/" + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                result = json.load(response)
            if not isinstance(result, dict) or ("workspaces" if path == "workspaces" else "entry") not in result:
                raise ValueError("Missing time entry data")
            return result
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raw = exc.headers.get("Retry-After", "")
                seconds = min(86400, max(1, int(raw))) if raw.isascii() and raw.isdigit() and len(raw) < 8 else 60
                minutes = (seconds + 59) // 60
                message = f"Toggl's API limit is reached. Try again in about {minutes} minute{'s' if minutes != 1 else ''}."
                if method != "GET":
                    raise UncertainError(message + " Check the current timer before trying another change.") from None
                raise ReminderError(message) from None
            if exc.code in (401, 403):
                raise ReminderError("Sign in to Stoic again on this Mac.") from None
            if exc.code in (409, 422):
                raise ReminderError("The timer or project could not be used. Check your Toggl connection and read the current entry again.") from None
            raise UncertainError("Stoic did not confirm the time entry. Check Toggl before trying again.") from None
        except (OSError, ValueError):
            raise UncertainError("Stoic did not confirm the time entry. Check Toggl before trying again.") from None

    def snapshot(self):
        return self.request()

    def workspaces(self):
        return self.request(path="workspaces")

    def result(self, entry, verb):
        if not isinstance(entry, dict) or not entry.get("id"):
            raise UncertainError("Stoic did not return a time entry. Check Toggl before trying again.")
        project = entry.get("project_name") or ("an unnamed project" if entry.get("project_id") else "no project")
        return {"ok": True, "entry": entry,
                "reply": f'{verb}: {entry.get("description") or "no description"}, under {project}.'}

    def execute(self, action, request_id):
        if not isinstance(action, dict) or set(action) - set(ACTION_SCHEMA["properties"]):
            raise ReminderError("Invalid time entry action.")
        if action.get("operation") == "current":
            entry = self.snapshot()["entry"]
            return self.result(entry, "Your running entry is") if entry else {"ok": True, "reply": "You don't have a timer running in Toggl."}
        if action.get("operation") == "start":
            return self.start(action, request_id)
        if action.get("operation") != "update" or not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ReminderError("Invalid time entry request.")
        entry_id = action.get("entry_id")
        if type(entry_id) is not int or entry_id <= 0:
            raise ReminderError("Read the current time entry before changing it.")
        changes = {}
        if action.get("description") is not None:
            value = action["description"]
            if not isinstance(value, str) or len(value) > 3000:
                raise ReminderError("Keep the time entry description under three thousand characters.")
            changes["description"] = value
        project_id = action.get("project_id")
        if project_id is not None:
            if type(project_id) is not int or project_id <= 0 or action.get("clear_project"):
                raise ReminderError("Choose one Toggl project for this entry.")
            changes["project_id"] = project_id
        if action.get("clear_project"):
            changes["project_id"] = None
        if not changes:
            raise ReminderError("Tell me the new description or project for your current entry.")
        encoded = json.dumps(action, sort_keys=True)
        # Reuse the private receipt journal, with a separate key namespace.
        with self.journal() as con:
            key = "time-" + request_id
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM operations WHERE request_id=?", (key,)).fetchone()
            if old:
                if old["action"] != encoded or old["status"] != "done":
                    raise UncertainError("This time entry request was already attempted. Check Toggl before trying again.")
                return json.loads(old["result"])
            snapshot = self.snapshot()
            entry = snapshot["entry"]
            if not entry or entry.get("id") != entry_id or entry.get("stop"):
                raise ReminderError("Your running timer changed. Ask again using the current entry.")
            if project_id is not None and not any(p.get("id") == project_id for p in snapshot.get("projects", [])):
                raise ReminderError("Choose a project from your current Toggl workspace.")
            con.execute("INSERT INTO operations VALUES (?, ?, 'pending', NULL)", (key, encoded))
            con.commit()  # Durable before the write: never repeat an uncertain outcome.
            saved = self.request("PATCH", {"expected_entry_id": entry_id, **changes}).get("entry")
            if (not isinstance(saved, dict) or saved.get("id") != entry_id or saved.get("stop")
                    or any(saved.get(k) != v for k, v in changes.items())):
                raise UncertainError("Stoic did not confirm the requested edit. Check Toggl before trying again.")
            result = self.result(saved, "Updated your running entry")
            con.execute("UPDATE operations SET status='done', result=? WHERE request_id=?", (json.dumps(result), key))
            con.commit()
            return result

    def start(self, action, request_id):
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ReminderError("Invalid time entry request.")
        workspace_id, project_id = action.get("workspace_id"), action.get("project_id")
        entry_id, description = action.get("entry_id"), action.get("description")
        if type(workspace_id) is not int or workspace_id <= 0:
            raise ReminderError("Choose a workspace from your connected Toggl account.")
        if "entry_id" not in action or (entry_id is not None and (type(entry_id) is not int or entry_id <= 0)):
            raise ReminderError("Read the current time entry before starting a timer.")
        if project_id is not None and (type(project_id) is not int or project_id <= 0):
            raise ReminderError("Choose a project from the selected Toggl workspace.")
        if action.get("clear_project"):
            raise ReminderError("Use a project or no project when starting a timer.")
        if not isinstance(description, str) or len(description) > 3000:
            raise ReminderError("Supply a timer description under three thousand characters.")
        encoded = json.dumps(action, sort_keys=True)
        with self.journal() as con:
            key = "time-" + request_id
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM operations WHERE request_id=?", (key,)).fetchone()
            if old:
                if old["action"] != encoded or old["status"] != "done":
                    raise UncertainError("This time entry request was already attempted. Check Toggl before trying again.")
                return json.loads(old["result"])
            entry = self.snapshot()["entry"]
            if (entry.get("id") if entry else None) != entry_id or (entry and entry.get("stop")):
                raise ReminderError("Your running timer changed. Read the current entry before starting a timer.")
            workspace = next((w for w in self.workspaces()["workspaces"] if w.get("id") == workspace_id), None)
            if not workspace or (project_id is not None and not any(p.get("id") == project_id for p in workspace.get("projects", []))):
                raise ReminderError("Choose a project from your selected Toggl workspace.")
            con.execute("INSERT INTO operations VALUES (?, ?, 'pending', NULL)", (key, encoded))
            con.commit()
            saved = self.request("POST", {"workspace_id": workspace_id, "project_id": project_id,
                "description": description, "expected_entry_id": entry_id}, path="start").get("entry")
            if (not isinstance(saved, dict) or type(saved.get("id")) is not int or saved["id"] <= 0
                    or saved["id"] == entry_id or saved.get("stop") or not saved.get("start")
                    or saved.get("workspace_id") != workspace_id or saved.get("project_id") != project_id
                    or saved.get("description") != description):
                raise UncertainError("Stoic did not confirm the new timer. Check Toggl before trying again.")
            result = self.result(saved, "Started your timer")
            con.execute("UPDATE operations SET status='done', result=? WHERE request_id=?", (json.dumps(result), key))
            con.commit()
            return result


def main():
    parser = argparse.ArgumentParser(description="Read Toggl's running entry or available workspaces and projects through Stoic.")
    parser.add_argument("operation", choices=["current", "workspaces"])
    args = parser.parse_args()
    try:
        adapter = TimeEntries()
        print(json.dumps(adapter.workspaces() if args.operation == "workspaces" else adapter.snapshot()))
    except ReminderError as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
