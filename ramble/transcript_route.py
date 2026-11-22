"""Daily transcript notes of the owner's own speech, for any notes system.

Ramble stays independent of the destination. For one local day it selects
thoughts of the configured classes, keeps only segments the source gate judges
to be the owner's words, renders an HTML document, and either writes it to
RAMBLE_HOME (the default, a dry run) or passes it as JSON on stdin to a
user-configured command. The document is rebuilt from scratch on every run, so
re-triage and late verdicts correct it; the destination should replace the
note it stored for the same ``external_key``.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config
from .source_gate import REMOVED_MARKER, SourceGate, thought_segments

NOTE_SCHEMA = 1
DEFAULT_ROUTE = {"enabled": False, "classes": ["log", "actionable", "dictation"],
                 "deliver_command": None, "output_dir": "transcript-notes"}


def route_settings(cfg: dict) -> dict:
    return {**DEFAULT_ROUTE, **(cfg.get("transcript_route") or {})}


def _zone(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone") or "UTC")


def day_bounds(day: dt.date, zone: ZoneInfo) -> tuple[float, float]:
    start = dt.datetime.combine(day, dt.time.min, zone)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def _clock(ts: float, zone: ZoneInfo) -> str:
    return dt.datetime.fromtimestamp(ts, zone).strftime("%-I:%M %p")


def build_day(conn, day: dt.date, cfg: dict, gate: SourceGate) -> dict:
    zone = _zone(cfg)
    route = route_settings(cfg)
    start, end = day_bounds(day, zone)
    classes = list(route["classes"])
    rows = conn.execute(
        "SELECT * FROM thoughts WHERE started_at >= ? AND started_at < ? AND status='done' "
        f"AND classification IN ({','.join('?' * len(classes))}) ORDER BY started_at",
        (start, end, *classes)).fetchall() if classes else []
    stats = {"thoughts_considered": len(rows), "thoughts_included": 0, "segments_kept": 0,
             "segments_removed": 0, "chars_kept": 0, "chars_removed": 0, "reasons": {}}
    entries = []
    for thought in rows:
        pieces, kept_any, removed_open = [], False, False
        for seg in thought_segments(conn, thought["id"]):
            verdict = gate.verdict(conn, seg)
            text = (seg["text"] or "").strip()
            stats["reasons"][verdict.reason] = stats["reasons"].get(verdict.reason, 0) + 1
            if verdict.keep:
                pieces.append(text)
                kept_any, removed_open = True, False
                stats["segments_kept"] += 1
                stats["chars_kept"] += len(text)
            else:
                if not removed_open and verdict.reason != "transcription_artifact":
                    pieces.append(REMOVED_MARKER)
                    removed_open = True
                stats["segments_removed"] += 1
                stats["chars_removed"] += len(text)
        if not kept_any:
            continue
        while pieces and pieces[-1] == REMOVED_MARKER:
            pieces.pop()
        while pieces and pieces[0] == REMOVED_MARKER:
            pieces.pop(0)
        stats["thoughts_included"] += 1
        entries.append({"thought_id": thought["id"], "started_at": thought["started_at"],
                        "dictation": thought["classification"] == "dictation", "text": " ".join(pieces)})
    title = f"Ramble — {day.isoformat()}"
    return {"schema": NOTE_SCHEMA, "date": day.isoformat(), "title": title,
            "external_key": f"ramble-transcripts/{day.isoformat()}", "tags": ["ramble", "transcript"],
            "html": render_html(day, entries, zone), "entries": entries, "stats": stats,
            "generated_at": dt.datetime.now(zone).isoformat(timespec="seconds")}


def render_html(day: dt.date, entries: list[dict], zone: ZoneInfo) -> str:
    heading = dt.datetime.combine(day, dt.time.min, zone).strftime("%A, %B %-d, %Y")
    out = [f"<p>Speech captured by Ramble on {html.escape(heading)}.</p>"]
    if not entries:
        out.append("<p>No speech was kept for this day.</p>")
    for e in entries:
        label = _clock(e["started_at"], zone) + (" · dictation" if e["dictation"] else "")
        out.append(f"<h3>{html.escape(label)}</h3>")
        out.append(f"<p>{html.escape(e['text'])}</p>")
    return "\n".join(out)


def output_dir(cfg: dict) -> Path:
    path = Path(route_settings(cfg)["output_dir"]).expanduser()
    return path if path.is_absolute() else config.HOME / path


def write_files(note: dict, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{note['date']}.html").write_text(note["html"] + "\n")
    path = directory / f"{note['date']}.json"
    path.write_text(json.dumps(note, indent=1) + "\n")
    return path


def deliver(note: dict, cfg: dict, *, live: bool, timeout_s: float = 120) -> dict:
    """Always writes the files; runs the configured command only when ``live`` is set."""
    path = write_files(note, output_dir(cfg))
    command = route_settings(cfg)["deliver_command"]
    if not live or not command:
        return {"delivered": False, "path": str(path), "reason": "dry_run" if command else "no_command"}
    result = subprocess.run(list(command), input=json.dumps(note), text=True, capture_output=True,
                            timeout=timeout_s)
    if result.returncode != 0:
        raise RuntimeError(f"transcript delivery failed ({result.returncode}): {result.stderr.strip()[:500]}")
    return {"delivered": True, "path": str(path), "output": result.stdout.strip()[:2000]}
