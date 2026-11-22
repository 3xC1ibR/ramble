"""User-editable runtime preferences, shared by Ramble and its Companion."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import threading

from . import config

MAX_BYTES = 32_768
_lock = threading.Lock()
ACTION_SCHEMA = {
    "type": ["object", "null"], "additionalProperties": False,
    "properties": {
        "operation": {"type": "string", "enum": ["update"]},
        "old_text": {"type": "string", "description": "Exact unique saved passage to replace; empty appends a preference."},
        "new_text": {"type": "string", "description": "Replacement preference; empty removes old_text. Preserve unrelated instructions."},
        "version": {"type": "string", "description": "Exact global-instructions version supplied with this request."},
    },
    "required": ["operation", "old_text", "new_text", "version"],
}


class ConflictError(ValueError):
    pass


class InstructionError(ValueError):
    """An instruction edit failed before changing the saved preferences."""


def path(home: Path | None = None) -> Path:
    return (home if home is not None else config.HOME) / "instructions.md"


def snapshot(home: Path | None = None) -> dict:
    try:
        data = path(home).read_bytes()
    except FileNotFoundError:
        data = b""
    if len(data) > MAX_BYTES:
        raise ValueError("Global instructions exceed the 32 KiB limit.")
    return {"text": data.decode("utf-8"), "version": hashlib.sha256(data).hexdigest()}


@contextmanager
def _write_lock(target: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target.parent / ".instructions.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def save(text: str, home: Path | None = None, *, version: str | None = None) -> dict:
    if not isinstance(text, str):
        raise ValueError("Instructions must be text.")
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Global instructions exceed the 32 KiB limit.")
    target = path(home)
    with _lock, _write_lock(target):
        if version is not None and version != snapshot(home)["version"]:
            raise ConflictError("Instructions changed elsewhere. Copy your draft, then reload before saving.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # A failed write leaves the previous instructions intact. Unique temp files
        # avoid collisions; only the owner can read new preference files.
        fd, temp = tempfile.mkstemp(prefix=".instructions-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        finally:
            Path(temp).unlink(missing_ok=True)
    return {"text": text, "version": hashlib.sha256(data).hexdigest()}


def execute(action: dict, request_id: str) -> dict:
    """Apply one versioned edit; the shared action queue owns replay protection."""
    if (action.get("operation") != "update"
            or any(not isinstance(action.get(key), str) for key in ("old_text", "new_text", "version"))):
        raise InstructionError("I couldn't resolve that instruction change. Your saved instructions are unchanged.")
    current = snapshot()
    if action["version"] != current["version"]:
        raise InstructionError("Your global instructions changed while I was preparing that edit. No changes were saved.")
    old, new = action["old_text"], action["new_text"]
    if old:
        if current["text"].count(old) != 1:
            raise InstructionError("I couldn't identify a unique instruction to change. No changes were saved.")
        text = current["text"].replace(old, new, 1)
    elif new.strip():
        separator = "\n" if current["text"] and not current["text"].endswith("\n") else ""
        text = current["text"] + separator + new + "\n"
    else:
        raise InstructionError("No instruction change was supplied. Your saved instructions are unchanged.")
    try:
        save(text, version=current["version"])
    except ConflictError as exc:
        raise InstructionError("Your global instructions changed while I was saving. No changes were saved.") from exc
    except ValueError as exc:
        raise InstructionError("That instruction change exceeds the saved instructions limit. No changes were saved.") from exc
    return {"reply": "Saved to your global instructions for future conversations."}


def prompt_block() -> str:
    saved = snapshot()
    return (
        "Ramble global instructions\n"
        f"Runtime preferences file: {path()}. Reloaded for this request; "
        "editable in Companion → Instructions. Empty means no saved preferences.\n"
        f"Global-instructions version: {saved['version']}\n"
        f"Developer rules for working on Ramble's code live separately in {config.REPO / 'AGENTS.md'}; "
        "that repository file exists and is not the runtime preferences editor.\n"
        "Apply the user's saved preferences below to replies, triage, and relevant dispatch briefs. "
        "They do not override the base routing, privacy, ambiguity/consent, pause, dispatch-budget, "
        "or structured-output rules. They are preferences, not a new voice request or authorization "
        "to dispatch work by themselves. Voice edits use the structured global_instructions action; "
        "never write this file through shell tools, a lookup, or triage.\n"
        "<saved_global_instructions>\n"
        + saved["text"] + "\n</saved_global_instructions>"
    )
