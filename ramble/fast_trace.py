"""Bounded, private fast-call diagnostics without prompts or tool payloads."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time
import uuid

from . import latency

log = logging.getLogger("ramble.codex")
EVENTS = {"thread.started", "turn.started", "turn.completed", "turn.failed",
          "item.started", "item.updated", "item.completed", "error"}
ITEMS = {"agent_message", "reasoning", "command_execution", "file_change",
         "mcp_tool_call", "web_search", "todo_list"}


def diagnostic(text: str) -> dict:
    """Only retain recognizable failure categories, never arbitrary error text."""
    lower = text.lower()
    result = {}
    status = re.search(r"(?:http(?:/\S+)?\s+|status(?: code)?[:= ]+|\b)(401|403|408|429|500|502|503|504)\b", lower)
    if status:
        result["http_status"] = int(status[1])
    for needle, category in (("rate limit", "rate_limit"), ("too many requests", "rate_limit"),
                             ("timed out", "timeout"), ("timeout", "timeout"),
                             ("reconnect", "reconnect"), ("retrying", "retry"),
                             ("stream disconnected", "stream_disconnected"),
                             ("unauthorized", "authentication"), ("failed to refresh", "authentication"),
                             ("connection", "connection"), ("dns", "dns")):
        if needle in lower:
            result["category"] = category
            break
    delay = re.search(r"retrying sampling request \(\d+/\d+ in ([\d.]+)s\)", text)
    if delay:
        result["retry_after_s"] = float(delay[1])
    return result


def command_kind(command: str) -> str:
    for needle, kind in (("publish-card weather", "weather_card"), ("wttr.in", "weather_http"),
                         ("bin/reminders", "reminders"), ("bin/time-entry", "time_entry"),
                         ("bin/publish", "companion_publish"), ("agent-coord", "agent_coord")):
        if needle in command:
            return kind
    return "shell"


class FastTrace:
    """A flushed JSONL file survives timeout/restart; the rotating log links it."""

    def __init__(self, folder: Path, *, request_id=None, **metadata):
        self.started = time.monotonic()
        self.call_id = uuid.uuid4().hex
        self.request_id = request_id
        self.path = folder / (time.strftime("%Y%m%d-%H%M%S-") + self.call_id + ".jsonl")
        self.file = None
        self.count = 0
        self.offsets = {"stdout": 0, "stderr": 0}
        self.partial = {"stdout": "", "stderr": ""}
        self.last_event = None
        self.active_tools = set()
        try:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Seven days and at most 500 calls; never touch unrelated files.
            paths = sorted(folder.glob("*.jsonl"), reverse=True)
            for i, path in enumerate(paths):
                if i >= 499 or path.stat().st_mtime < time.time() - 7 * 86400:
                    path.unlink(missing_ok=True)
            self.file = self.path.open("x", encoding="utf-8")
            os.chmod(self.path, 0o600)
        except OSError:
            log.warning("Could not create fast-call trace", exc_info=True)
        self.event("call_started", **metadata)
        latency.event("codex_call_start", call_id=self.call_id, trace_path=str(self.path), **metadata)

    def event(self, stage, **fields):
        # A noisy tool cannot grow an unbounded diagnostic file. Always retain end.
        if self.count >= 1000 and stage != "call_finished":
            return
        record = dict(stage=stage, wall_s=time.time(), elapsed_s=round(time.monotonic() - self.started, 4),
                      request_id=self.request_id, call_id=self.call_id, **fields)
        if self.file:
            try:
                self.file.write(json.dumps(record, separators=(",", ":")) + "\n")
                self.file.flush()
            except OSError:
                log.warning("Could not write fast-call trace", exc_info=True)
                self.file.close()
                self.file = None
        self.count += 1

    def consume(self, stdout, stderr, *, final=False):
        for stream, text in (("stdout", stdout), ("stderr", stderr)):
            if isinstance(text, bytes):
                text = text.decode(errors="replace")
            text = text or ""
            fresh = text[self.offsets[stream]:]
            self.offsets[stream] = len(text)
            lines = (self.partial[stream] + fresh).split("\n")
            self.partial[stream] = lines.pop()
            if final and self.partial[stream]:
                lines.append(self.partial[stream])
                self.partial[stream] = ""
            for line in lines:
                if stream == "stderr":
                    info = diagnostic(line)
                    if info:
                        self.event("stderr_diagnostic", **info)
                else:
                    self._codex_event(line)

    def _codex_event(self, line):
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict) or event.get("type") not in EVENTS:
            return
        kind = event["type"]
        fields = {"event": kind}
        item = event.get("item") or {}
        if not isinstance(item, dict):
            item = {}
        item_type = item.get("type")
        if item_type in ITEMS:
            fields["item_type"] = item_type
        tool_id = str(item.get("id", ""))
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", tool_id):
            fields["item_id"] = tool_id
        if item.get("status") in ("in_progress", "completed", "failed", "declined"):
            fields["status"] = item["status"]
        if isinstance(item.get("exit_code"), int):
            fields["exit_code"] = item["exit_code"]
        command = item.get("command")
        if isinstance(command, str):
            fields.update(tool=command_kind(command), command_sha256=hashlib.sha256(command.encode()).hexdigest()[:16])
        if item_type in ("command_execution", "mcp_tool_call", "web_search"):
            if kind == "item.started":
                self.active_tools.add(tool_id)
            elif kind == "item.completed":
                self.active_tools.discard(tool_id)
        if kind in ("error", "turn.failed"):
            fields.update(diagnostic(str(event.get("message", "")) + str(event.get("error", ""))))
        usage = event.get("usage")
        if isinstance(usage, dict):
            fields["usage"] = {k: v for k, v in usage.items() if k in (
                "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens") and isinstance(v, int)}
        self.last_event = kind
        self.event("codex_event", **fields)

    def finish(self, outcome, **fields):
        self.event("call_finished", outcome=outcome, last_event=self.last_event,
                   active_tools=len(self.active_tools), **fields)
        latency.event("codex_call_end", call_id=self.call_id, trace_path=str(self.path),
                      outcome=outcome, last_event=self.last_event, active_tools=len(self.active_tools),
                      elapsed_s=time.monotonic() - self.started, **fields)
        if self.file:
            self.file.close()
            self.file = None
