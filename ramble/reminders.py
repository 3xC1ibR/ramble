"""Stoic reminder actions shared by the voice responder and Companion.

Stoic owns schedules and delivery. The local SQLite journal only prevents
replaying writes; it is never a second scheduler. No automatic POST retries.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from . import config

API = "https://tryblume.ai"
TIMEZONE = config.load().get("timezone", "UTC")
IDENTIFIER = re.compile(r"[a-zA-Z0-9_-]{1,120}\Z")
MARKER = re.compile(r"\n\n\[Ramble request: [a-zA-Z0-9_-]+\]\Z")
ACTION_SCHEMA = {
    "type": ["object", "null"], "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["create", "list", "reschedule", "cancel", "complete"]},
        "title": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "due_at": {"type": ["string", "null"], "description": "Future ISO 8601 datetime with explicit UTC offset in the configured timezone; null unless creating or rescheduling."},
        "recurrence_type": {"type": ["string", "null"], "enum": ["none", "daily", "weekly", "monthly", None]},
        "kind": {"type": ["string", "null"], "enum": ["reminder", "check-in", None]},
        "job_id": {"type": ["string", "null"], "description": "Exact Stoic note_id from the reminder list; never guess."},
        "occurrence_id": {"type": ["string", "null"], "description": "Exact pending occurrence ID for completing or moving one occurrence."},
    },
}
ACTION_SCHEMA["required"] = list(ACTION_SCHEMA["properties"])


class ReminderError(Exception):
    """Safe to display; never includes credentials or remote response bodies."""


class UncertainError(ReminderError):
    pass


def due_time(value: str, zone: str = TIMEZONE) -> str:
    """Validate explicit offsets or resolve a Companion datetime-local value.

    Reject nonexistent/ambiguous local times instead of silently moving a reminder.
    """
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        tz = ZoneInfo(zone)
        if dt.tzinfo is None:
            candidates = []
            for fold in (0, 1):
                candidate = dt.replace(tzinfo=tz, fold=fold)
                if candidate.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) == dt:
                    candidates.append(candidate)
            offsets = {d.utcoffset() for d in candidates}
            if len(offsets) != 1:
                raise ReminderError("That local time is ambiguous or skipped by daylight saving. Choose another time or specify its UTC offset.")
            dt = candidates[0]
        if dt <= datetime.now(timezone.utc):
            raise ReminderError("Choose a reminder time in the future.")
        return dt.astimezone(tz).isoformat()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReminderError("Use a valid reminder date and time.") from exc


def when(value: str, zone: str = TIMEZONE) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
    return dt.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")


def compact(job: dict) -> dict:
    return {
        "note_id": job["note_id"], "title": job["title"],
        "description": MARKER.sub("", job.get("description") or ""),
        "type": job.get("type", "reminder"), "owner": job.get("owner", "user"),
        "status": job.get("status"), "recurrence_type": job.get("recurrence_type") or "none",
        "due_instances": [{k: i.get(k) for k in ("id", "due_at", "status")}
                          for i in job.get("due_instances", []) if i.get("status") == "pending"],
    }


class Reminders:
    def __init__(self, home: Path | None = None, *, zone: str = TIMEZONE):
        self.home = home if home is not None else config.HOME
        self.zone = zone

    def request(self, method: str, path: str, body=None):
        try:
            token = os.environ.get("STOIC_API_TOKEN") or Path(os.environ.get(
                "STOIC_API_TOKEN_FILE", str(Path.home() / ".config/stoic/codex-token"))).read_text().strip()
        except OSError as exc:
            raise ReminderError("Stoic is not connected on this Mac. Sign in before managing reminders.") from exc
        if not token:
            raise ReminderError("Stoic is not connected on this Mac.")
        request = urllib.request.Request(API + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        for attempt in range(2 if method == "GET" else 1):
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise ReminderError("Stoic needs you to sign in again on this Mac.") from None
                if exc.code in (400, 404, 409, 422, 429):
                    raise ReminderError("Stoic could not accept this reminder request. Refresh the list and check the details.") from None
                if method != "GET":
                    raise UncertainError("Stoic did not confirm the change. Check your reminders before trying again.") from None
            except (OSError, ValueError):
                if method != "GET":
                    raise UncertainError("Stoic did not confirm the change. Check your reminders before trying again.") from None
            if attempt == (1 if method == "GET" else 0):
                raise ReminderError("Stoic is unavailable. Your reminders could not be loaded.")

    def jobs(self) -> list[dict]:
        # API currently caps the newest inventory at 200 with no pagination.
        result = self.request("GET", "/api/assistant-jobs?status_filter=all&limit=200")
        if not isinstance(result, list):
            raise ReminderError("Stoic returned an unreadable reminder list.")
        return result

    def snapshot(self) -> dict:
        jobs = self.jobs()
        entries = [compact(j) for j in jobs if j.get("type") in ("reminder", "check-in")
                   and j.get("status") in ("pending", "active")]
        entries.sort(key=lambda j: min((i["due_at"] for i in j["due_instances"]), default="9999"))
        return {"entries": entries, "timezone": self.zone, "limited": len(jobs) >= 200}

    @contextmanager
    def journal(self):
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / "reminders.sqlite3"
        con = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE IF NOT EXISTS operations (request_id TEXT PRIMARY KEY, action TEXT NOT NULL, status TEXT NOT NULL, result TEXT)")
        con.commit()
        try:
            yield con
        finally:
            con.close()

    def normalize(self, action: dict) -> dict:
        if not isinstance(action, dict) or set(action) - set(ACTION_SCHEMA["properties"]):
            raise ReminderError("Invalid reminder action.")
        op = action.get("operation")
        if op not in ACTION_SCHEMA["properties"]["operation"]["enum"]:
            raise ReminderError("Unknown reminder action.")
        result = {"operation": op}
        if op == "list":
            return result
        if op == "create":
            title = action.get("title")
            description = action.get("description") or ""
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 240:
                raise ReminderError("Give the reminder a short title.")
            if not isinstance(description, str) or len(description) > 4000:
                raise ReminderError("Keep the reminder description under 4,000 characters.")
            kind, repeat = action.get("kind") or "reminder", action.get("recurrence_type") or "none"
            if kind not in ("reminder", "check-in") or repeat not in ("none", "daily", "weekly", "monthly"):
                raise ReminderError("Choose a reminder or check-in with a supported repeat schedule.")
            result.update(title=title.strip(), description=description, kind=kind, recurrence_type=repeat)
        else:
            for field in (["job_id"] if op == "cancel" else ["job_id", "occurrence_id"]):
                value = action.get(field)
                if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                    raise ReminderError("Choose an exact reminder and occurrence from the current list.")
                result[field] = value
        if op in ("create", "reschedule"):
            result["due_at"] = due_time(action.get("due_at"), self.zone)
        return result

    def execute(self, action: dict, request_id: str) -> dict:
        """Replay a confirmed receipt; never repeat an uncertain mutation."""
        if not isinstance(request_id, str) or not IDENTIFIER.fullmatch(request_id):
            raise ReminderError("A stable request ID is required.")
        # Preserve the original input in the journal so a retry still works after
        # its due time passes. Revalidation occurs only for a genuinely new action.
        encoded = json.dumps(action, sort_keys=True, separators=(",", ":"))
        if isinstance(action, dict) and action.get("operation") == "list":
            snapshot = self.snapshot()
            occurrences = [(j, i) for j in snapshot["entries"] for i in j["due_instances"]]
            occurrences.sort(key=lambda pair: pair[1]["due_at"])
            descriptions = [f'{j["title"]}, {when(i["due_at"], self.zone)}' for j, i in occurrences[:3]]
            reply = "Your reminders are: " + "; ".join(descriptions) + "." if descriptions else "No upcoming reminders were found in Stoic."
            if snapshot["limited"]:
                reply += " This list covers only your most recent two hundred items."
            return {"ok": True, "reply": reply, **snapshot}
        with self.journal() as con:
            con.execute("BEGIN IMMEDIATE")
            old = con.execute("SELECT * FROM operations WHERE request_id=?", (request_id,)).fetchone()
            if old:
                con.commit()
                if old["action"] != encoded:
                    raise ReminderError("This request was already used. Refresh before making a different change.")
                if old["status"] == "done":
                    return json.loads(old["result"])
                if old["status"] == "failed":
                    raise ReminderError(json.loads(old["result"])["error"])
                # A lost create response can be reconciled by the source marker.
                if isinstance(action, dict) and action.get("operation") == "create":
                    marker = f"[Ramble request: {request_id}]"
                    found = next((j for j in self.jobs() if (j.get("description") or "").endswith(marker)), None)
                    if found:
                        result = self.result("create", found)
                        self.finish(con, request_id, "done", result)
                        return result
                raise UncertainError("This change is still unconfirmed. Check Stoic before making another request; it has not been repeated.")
            normalized = self.normalize(action)
            con.execute("INSERT INTO operations VALUES (?, ?, 'pending', NULL)", (request_id, encoded))
            con.commit()  # Durable before the network side effect.
            try:
                result = self.mutate(normalized, request_id)
            except UncertainError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError):
                raise UncertainError("Stoic returned an unreadable result. Check Stoic before retrying the change.") from None
            except ReminderError as exc:
                self.finish(con, request_id, "failed", {"error": str(exc)})
                raise
            self.finish(con, request_id, "done", result)
            return result

    @staticmethod
    def finish(con, key, status, result):
        con.execute("UPDATE operations SET status=?, result=? WHERE request_id=?", (status, json.dumps(result), key))
        con.commit()

    def mutate(self, action: dict, request_id: str) -> dict:
        op = action["operation"]
        if op == "create":
            owner = "assistant" if action["kind"] == "check-in" else "user"
            jobs = self.jobs()
            for job in jobs:
                if (job.get("status") in ("pending", "active")
                        and job.get("title", "").strip().casefold() == action["title"].casefold()
                        and job.get("owner") == owner and job.get("type") == action["kind"]
                        and (job.get("recurrence_type") or "none") == action["recurrence_type"]
                        and any(i.get("status") == "pending" and
                                datetime.fromisoformat(i["due_at"].replace("Z", "+00:00")) == datetime.fromisoformat(action["due_at"])
                                for i in job.get("due_instances", []))):
                    return self.result(op, job, duplicate=True)
            body = {k: action[k] for k in ("title", "description", "due_at", "recurrence_type")}
            body.update(owner=owner, type=action["kind"])
            body["description"] += f"\n\n[Ramble request: {request_id}]"
            job = self.request("POST", "/api/assistant-jobs", body)
        else:
            path = "/api/assistant-jobs/" + action["job_id"]
            job = self.request("GET", path)
            if job.get("type") not in ("reminder", "check-in"):
                raise ReminderError("This tool manages reminders and check-ins only.")
            if job.get("status") not in ("active", "pending"):
                raise ReminderError("That reminder is no longer active. Refresh the list.")
            if op == "cancel":
                job = self.request("PATCH", path, {"status": "cancelled"})
            else:
                occurrence = next((i for i in job.get("due_instances", []) if i["id"] == action["occurrence_id"]), None)
                if not occurrence or occurrence.get("status") != "pending":
                    raise ReminderError("That occurrence is no longer pending. Refresh the list.")
                if op == "reschedule":
                    job = self.request("PATCH", path + "/due-instances/" + action["occurrence_id"], {"due_at": action["due_at"]})
                else:
                    job = self.request("PATCH", path + "/occurrences", {"occurrence_id": action["occurrence_id"], "status": "completed", "note": "Completed through Ramble."})
        if op in ("create", "reschedule"):
            wanted = datetime.fromisoformat(action["due_at"])
            matching = [i for i in job.get("due_instances", [])
                        if i.get("status") == "pending" and
                        (op == "create" or i.get("id") == action["occurrence_id"])]
            if not any(datetime.fromisoformat(i["due_at"].replace("Z", "+00:00")) == wanted for i in matching):
                raise UncertainError("Stoic did not confirm the requested time. Check Stoic before retrying.")
        return self.result(op, job, occurrence_id=action.get("occurrence_id"))

    def result(self, op: str, job: dict, *, occurrence_id=None, duplicate=False) -> dict:
        if not isinstance(job, dict) or not job.get("note_id") or not job.get("title"):
            raise UncertainError("Stoic did not return a saved reminder. Check Stoic before retrying.")
        item = compact(job)
        title = item["title"]
        if op in ("create", "reschedule"):
            occurrence = next((i for i in item["due_instances"] if not occurrence_id or i["id"] == occurrence_id), None)
            if not occurrence:
                raise UncertainError("Stoic did not confirm a pending occurrence. Check Stoic before retrying.")
            verb = "Already saved" if duplicate else "Moved" if op == "reschedule" else "Saved"
            reply = f'{verb}: {title}, {when(occurrence["due_at"], self.zone)}.'
            if item["recurrence_type"] != "none":
                reply += f' Repeats {item["recurrence_type"]}.'
        elif op == "cancel":
            if job.get("status") != "cancelled":
                raise UncertainError("Stoic did not confirm cancellation. Check Stoic before retrying.")
            reply = f"Cancelled: {title}."
        else:
            occurrence = next((i for i in job.get("due_instances", []) if i["id"] == occurrence_id), None)
            if not occurrence or occurrence.get("status") != "completed":
                raise UncertainError("Stoic did not confirm completion. Check Stoic before retrying.")
            reply = f"Marked done: {title}."
        return {"ok": True, "reply": reply, "job": item}


def voice_key(items: list[dict]) -> str:
    return "voice-" + hashlib.sha256("|".join(i["seg"]["id"] for i in items).encode()).hexdigest()[:32]


def main():
    parser = argparse.ArgumentParser(description="Read Stoic reminders for Ramble; writes are executed by the app.")
    parser.add_argument("operation", choices=["list"])
    parser.parse_args()
    try:
        print(json.dumps(Reminders().snapshot()))
    except ReminderError as exc:
        print(json.dumps({"error": str(exc)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
