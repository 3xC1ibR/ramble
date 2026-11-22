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
                 "deliver_command": None, "output_dir": "transcript-notes",
                 "min_owner_score": 0.25, "interval_s": 1800, "deliver_after_hour": 3}


def route_settings(cfg: dict) -> dict:
    return {**DEFAULT_ROUTE, **(cfg.get("transcript_route") or {})}


def _zone(cfg: dict) -> ZoneInfo:
    return ZoneInfo(cfg.get("timezone") or "UTC")


def day_bounds(day: dt.date, zone: ZoneInfo) -> tuple[float, float]:
    start = dt.datetime.combine(day, dt.time.min, zone)
    return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()


def _clock(ts: float, zone: ZoneInfo) -> str:
    return dt.datetime.fromtimestamp(ts, zone).strftime("%-I:%M %p")


def note_decisions(verdicts, min_score: float) -> list[tuple[bool, str]]:
    """Stricter than the gate, because notes become permanent memory.

    A segment enters a note only when its owner score reaches ``min_score``.
    A clip too short to judge enters only beside such a segment, and
    unverified segments (no profile or model) never enter: notes fail closed
    and are rebuilt once verification works again. Triage still fails open.
    """
    strong = [v.reason == "owner_verified" and v.score is not None and v.score >= min_score for v in verdicts]
    out = []
    for i, v in enumerate(verdicts):
        if strong[i]:
            out.append((True, "owner_verified"))
        elif v.reason == "owner_verified":
            out.append((False, "below_note_threshold"))
        elif v.reason == "too_short_to_judge":
            beside = (i > 0 and strong[i - 1]) or (i + 1 < len(verdicts) and strong[i + 1])
            out.append((beside, "too_short_beside_owner" if beside else "too_short_alone"))
        elif v.reason == "unverified":
            out.append((False, "unverified"))
        else:
            out.append((False, v.reason))
    return out


def _count(stats: dict, reason: str, keep: bool, text: str) -> None:
    stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1
    stats["segments_kept" if keep else "segments_removed"] += 1
    stats["chars_kept" if keep else "chars_removed"] += len(text)


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
    min_score = float(route["min_owner_score"])
    entries = []
    for thought in rows:
        segments = thought_segments(conn, thought["id"])
        verdicts = [gate.verdict(conn, seg) for seg in segments]
        decisions = note_decisions(verdicts, min_score)
        if not any(reason == "owner_verified" for _, reason in decisions):
            for seg, (_, reason) in zip(segments, decisions):
                _count(stats, reason, False, (seg["text"] or "").strip())
            continue
        pieces, removed_open = [], False
        for seg, (keep, reason) in zip(segments, decisions):
            text = (seg["text"] or "").strip()
            _count(stats, reason, keep, text)
            if keep:
                pieces.append(text)
                removed_open = False
            elif not removed_open and reason != "transcription_artifact":
                pieces.append(REMOVED_MARKER)
                removed_open = True
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


class TranscriptNoteScheduler:
    """Daemon worker: refreshes today's file and delivers completed days.

    Every ``interval_s`` it rebuilds the files for today and the two previous
    days. After ``deliver_after_hour`` local time it delivers the two previous
    days through ``deliver_command`` when their content differs from the last
    successful delivery, so late verdicts or re-triage correct the stored note.
    """

    def __init__(self, cfg: dict, connect, gate: SourceGate | None = None, clock=None):
        import time
        self.cfg = cfg
        self.connect = connect
        self.gate = gate or SourceGate(cfg)
        self.clock = clock or time.time

    def _ledger_path(self) -> Path:
        return output_dir(self.cfg) / "delivered.json"

    def run_once(self, conn) -> list[dict]:
        import hashlib
        import logging
        log = logging.getLogger("ramble.transcripts")
        route = route_settings(self.cfg)
        zone = _zone(self.cfg)
        now = dt.datetime.fromtimestamp(self.clock(), zone)
        ledger_path = self._ledger_path()
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
        results = []
        for offset in (2, 1, 0):
            day = now.date() - dt.timedelta(days=offset)
            note = build_day(conn, day, self.cfg, self.gate)
            digest = hashlib.sha256(note["html"].encode()).hexdigest()
            closed = offset > 0 and now.hour >= int(route["deliver_after_hour"])
            empty_first = not note["entries"] and note["date"] not in ledger  # never create an empty note
            if not closed or not route["deliver_command"] or ledger.get(note["date"]) == digest or empty_first:
                write_files(note, output_dir(self.cfg))
                results.append({"date": note["date"], "delivered": False})
                continue
            result = deliver(note, self.cfg, live=True)
            ledger[note["date"]] = digest
            ledger_path.write_text(json.dumps(ledger, indent=1))
            log.info("transcript note %s delivered: %s", note["date"], result.get("output", ""))
            results.append({"date": note["date"], "delivered": True})
        return results

    def run_forever(self, stop) -> None:
        import logging
        log = logging.getLogger("ramble.transcripts")
        conn = self.connect()
        try:
            while not stop.is_set():
                try:
                    self.run_once(conn)
                except Exception:  # noqa: BLE001 - a failed delivery is retried next interval
                    log.exception("transcript note refresh failed")
                stop.wait(float(route_settings(self.cfg)["interval_s"]))
        finally:
            conn.close()
