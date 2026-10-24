"""Close finished thoughts, run the triage agent on each, execute its decision.

Triage is one headless Claude Code run per thought (YOLO, cwd /path/to/projects) that
may look things up but must return a structured decision. The router then does
the side effects deterministically: dispatch Ribbon Field (agent-coord) threads,
append to the daily digest, and notify.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import uuid

from . import config, db, state
from .transcribe import stamp

log = logging.getLogger("ramble.router")

DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "classification": {
            "type": "string",
            "enum": ["ambient", "personal", "log", "actionable"],
            "description": "ambient = TV/other people/noise not addressed to the system; personal = the user talking but nothing to keep; log = worth keeping, no work; actionable = at least one item to dispatch",
        },
        "summary": {"type": "string", "description": "One or two sentences on what the user said."},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["idea", "task", "link", "question", "reflection", "note"]},
                    "text": {"type": "string", "description": "The item restated cleanly in the user's terms."},
                    "action": {"type": "string", "enum": ["log", "dispatch"]},
                    "dispatch": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "name": {"type": "string", "description": "Short thread name, 3-6 words."},
                            "cwd": {"type": "string", "description": "Absolute working directory for the agent."},
                            "prompt": {"type": "string", "description": "Complete, self-contained brief for the agent."},
                        },
                        "required": ["name", "cwd", "prompt"],
                    },
                },
                "required": ["kind", "text", "action"],
            },
        },
        "notify": {
            "type": ["string", "null"],
            "description": "Short notification text for the user, or null to stay quiet.",
        },
    },
    "required": ["classification", "summary", "items", "notify"],
}


def clean_env() -> dict:
    """Environment for child processes, minus any caller-session identity we might have inherited."""
    drop = ("AGENT_COORD_SESSION_ID", "AGENT_COORD_CLIENT", "CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_PID", "CODEX_THREAD_ID")
    return {k: v for k, v in os.environ.items() if not k.startswith(drop)}


def ensure_coord_session() -> str:
    """Ramble's own agent-coord session ("Ramble voice dispatcher"), the creator of dispatched threads.

    agent-coord requires a caller session to create threads; a launchd process has none,
    so ramble registers a stable one (id persisted in RAMBLE_HOME) and refreshes it.
    """
    path = config.HOME / "agent_coord_session"
    sid = path.read_text().strip() if path.exists() else str(uuid.uuid4())
    subprocess.run(
        ["agent-coord", "register", "--session-id", sid, "--client", "claude", "--cwd", str(config.REPO), "--name", "Ramble voice dispatcher"],
        capture_output=True, text=True, timeout=60, env=clean_env(), check=True,
    )
    path.write_text(sid)
    return sid


def day_start(ts: float | None = None) -> float:
    lt = time.localtime(ts or time.time())
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


class Router:
    def __init__(self, cfg: dict, wake: threading.Event) -> None:
        self.cfg = cfg
        self.wake = wake
        self.current: str | None = None

    # ---- loop -------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        conn = db.connect()
        # Thoughts interrupted mid-triage by a restart get retried.
        conn.execute("UPDATE thoughts SET status='closed' WHERE status='triaging'")
        while not stop.is_set():
            try:
                self.close_stale(conn)
                row = conn.execute(
                    "SELECT * FROM thoughts WHERE status='closed' ORDER BY started_at LIMIT 1"
                ).fetchone()
                if row:
                    self.triage(conn, row)
                    continue
            except Exception:  # noqa: BLE001
                log.exception("router loop error")
            stop.wait(5)

    def close_stale(self, conn) -> None:
        pending = conn.execute("SELECT COUNT(*) FROM segments WHERE status='pending'").fetchone()[0]
        if pending:
            return
        now = time.time()
        listening = state.is_listening()
        for t in conn.execute("SELECT * FROM thoughts WHERE status='open'").fetchall():
            quiet = now - t["ended_at"] >= self.cfg["thought_gap_s"]
            too_long = t["ended_at"] - t["started_at"] >= self.cfg["thought_max_s"]
            if quiet or too_long or not listening:
                conn.execute("UPDATE thoughts SET status='closed' WHERE id=?", (t["id"],))
                log.info("thought %s closed", t["id"])

    # ---- triage -----------------------------------------------------------
    def build_input(self, conn, thought) -> str:
        since = time.time() - 24 * 3600
        recent = conn.execute(
            "SELECT id, started_at, classification, summary FROM thoughts WHERE status='done' AND started_at>=? ORDER BY started_at DESC LIMIT 10",
            (since,),
        ).fetchall()
        lines = []
        for r in reversed(recent):
            names = [
                d["name"]
                for d in conn.execute(
                    "SELECT name FROM dispatches WHERE thought_id=? AND status='created'", (r["id"],)
                )
            ]
            extra = f" (dispatched: {', '.join(names)})" if names else ""
            lines.append(f"- {stamp(r['started_at'])} [{r['classification']}] {r['summary']}{extra}")
        used = self.dispatches_today(conn)
        cap = self.cfg["dispatch_daily_cap"]
        return "\n".join(
            [
                f"Now: {time.strftime('%A %Y-%m-%d %-I:%M %p %Z')}",
                f"Dispatch budget today: {max(0, cap - used)} of {cap} remaining.",
                "",
                "Recent thoughts (last 24h, for continuity):",
                *(lines or ["- none"]),
                "",
                f"New thought {thought['id']} ({stamp(thought['started_at'])}–{stamp(thought['ended_at'])}), speech-to-text:",
                "<transcript>",
                thought["text"],
                "</transcript>",
            ]
        )

    def run_claude(self, prompt: str, thought_id: str) -> dict:
        cmd = [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(DECISION_SCHEMA),
            "--append-system-prompt-file",
            str(config.TRIAGE_PROMPT),
            # Triage runs are internal plumbing: keep them out of Ribbon Field's thread list.
            "--settings",
            json.dumps({"disableAllHooks": True}),
            "--no-session-persistence",
        ]
        if self.cfg["triage_model"]:
            cmd += ["--model", self.cfg["triage_model"]]
        env = clean_env()
        proc = subprocess.run(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            cwd=self.cfg["triage_cwd"],
            env=env,
            timeout=self.cfg["triage_timeout_s"],
        )
        (config.TRIAGE_DIR / f"{thought_id}.json").write_text(
            json.dumps({"input": prompt, "returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr[-4000:]}, indent=2)
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude exited {proc.returncode}: {proc.stderr[-500:] or proc.stdout[-500:]}")
        out = json.loads(proc.stdout)
        decision = out.get("structured_output")
        if decision is None:
            # Fallback: model put JSON in the text result.
            text = out.get("result", "")
            decision = json.loads(text[text.index("{") : text.rindex("}") + 1])
        return decision

    def triage(self, conn, thought) -> None:
        tid = thought["id"]
        self.current = tid
        conn.execute("UPDATE thoughts SET status='triaging' WHERE id=?", (tid,))
        log.info("triaging %s", tid)
        try:
            decision = self.run_claude(self.build_input(conn, thought), tid)
        except Exception as e:  # noqa: BLE001
            log.exception("triage failed %s", tid)
            conn.execute("UPDATE thoughts SET status='error', error=? WHERE id=?", (str(e)[:2000], tid))
            self.current = None
            return
        dispatched = []
        for item in decision.get("items", []):
            if item.get("action") == "dispatch" and item.get("dispatch"):
                dispatched.append(self.dispatch(conn, thought, item))
        conn.execute(
            "UPDATE thoughts SET status='done', classification=?, summary=?, triage_json=?, triaged_at=? WHERE id=?",
            (decision.get("classification"), decision.get("summary"), json.dumps(decision), time.time(), tid),
        )
        write_digest(thought, decision, dispatched)
        msg = decision.get("notify")
        created = [d for d in dispatched if d["status"] == "created"]
        if created and not msg:
            msg = "Started: " + ", ".join(d["name"] for d in created)
        if msg:
            notify(self.cfg, msg)
        log.info("triaged %s -> %s (%d dispatched)", tid, decision.get("classification"), len(created))
        self.current = None

    # ---- dispatch ---------------------------------------------------------
    def dispatches_today(self, conn) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM dispatches WHERE status='created' AND created_at>=?", (day_start(),)
        ).fetchone()[0]

    def dispatch(self, conn, thought, item) -> dict:
        d = item["dispatch"]
        did = db.new_id()
        name = d["name"][:80]
        cwd = d["cwd"] if os.path.isdir(d["cwd"]) else self.cfg["triage_cwd"]
        prompt = (
            d["prompt"].rstrip()
            + "\n\n---\n"
            + f"Dispatched automatically by ramble (the user's ambient voice capture, /path/to/projects/experiments/ramble) "
            + f"from something he said out loud at {stamp(thought['started_at'])}. He is not watching this thread live. "
            + "Work autonomously, prefer the technically best approach, keep your Agent Coord checkpoint current, and "
            + "finish with a short result he can skim. Source transcript (speech-to-text, may contain errors):\n"
            + thought["text"][-3000:]
        )
        rec = {"id": did, "name": name, "cwd": cwd, "status": "error", "thread_id": None, "url": None, "detail": None}
        if self.dispatches_today(conn) >= self.cfg["dispatch_daily_cap"]:
            rec.update(status="skipped_cap", detail="daily dispatch cap reached")
        else:
            cmd = [
                "agent-coord", "thread", "create",
                "--from-session", ensure_coord_session(),
                "--cwd", cwd,
                "--name", name,
                "--client", self.cfg["dispatch_client"],
                "--yolo",
                "--no-subscribe",
                "--request-id", f"ramble-{did}",
                "-",
            ]
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=120, env=clean_env())
                rec["detail"] = (p.stdout + p.stderr)[-2000:]
                if p.returncode == 0:
                    rec["status"] = "created"
                    try:
                        out = json.loads(p.stdout)
                        rec["thread_id"] = out.get("thread_id")
                        rec["url"] = (out.get("result") or {}).get("url")
                    except json.JSONDecodeError:
                        pass
            except Exception as e:  # noqa: BLE001
                rec["detail"] = str(e)
        conn.execute(
            "INSERT INTO dispatches (id, thought_id, created_at, name, cwd, prompt, thread_id, status, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (did, thought["id"], time.time(), name, cwd, prompt, rec["thread_id"], rec["status"], rec["detail"]),
        )
        log.info("dispatch %s %s -> %s", did, name, rec["status"])
        return rec


def write_digest(thought, decision: dict, dispatched: list[dict]) -> None:
    day = time.strftime("%Y-%m-%d", time.localtime(thought["started_at"]))
    path = config.DIGEST_DIR / f"{day}.md"
    new = not path.exists()
    with path.open("a") as f:
        if new:
            f.write(f"# Ramble digest {day}\n\n")
        cls = decision.get("classification")
        f.write(f"## {stamp(thought['started_at'])} · {cls} · `{thought['id']}`\n\n")
        f.write(f"{decision.get('summary', '').strip()}\n\n")
        for item in decision.get("items", []):
            f.write(f"- **{item.get('kind')}** ({item.get('action')}): {item.get('text')}\n")
        for d in dispatched:
            link = f" ([open]({d['url']}))" if d.get("url") else ""
            f.write(f"- → dispatched `{d['name']}` in `{d['cwd']}`: {d['status']}{link}\n")
        if cls not in ("ambient",):
            f.write("\n<details><summary>transcript</summary>\n\n```\n" + thought["text"] + "\n```\n</details>\n")
        f.write("\n")


def notify(cfg: dict, message: str, title: str = "Ramble") -> None:
    if not cfg.get("notify"):
        return
    try:
        script = f"display notification {json.dumps(message[:200])} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
    except Exception:  # noqa: BLE001
        log.exception("macOS notification failed")
    topic = cfg.get("ntfy_topic")
    if topic:
        try:
            req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode(), headers={"Title": title})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception:  # noqa: BLE001
            log.exception("ntfy failed")
