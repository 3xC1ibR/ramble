"""Fast lane: speech addressed to Ramble gets a spoken answer within seconds.

The slow lane (router.py) still sees every utterance. Addressed means the
segment contains the name ("Ramble, ...", "hey Ramble", mid-sentence), or it
starts within followup_s after Ramble finished speaking, or it follows a bare
"Ramble?" within a few seconds. A quick Codex run answers with a short spoken
reply. Real work gets "on it" plus a handoff marker, and the slow lane dispatches it.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
import time
from collections import deque

from . import config, db
from .router import clean_env
from .transcribe import stamp

log = logging.getLogger("ramble.fast")

# Whisper's usual spellings of the name.
NAME = re.compile(r"\b(?:hey|hi|ok|okay|yo)?[\s,]*\b(ramble|rambo|rumble|rambel|rambles)\b[\s,.!?]*", re.I)
FAST_PROMPT = config.REPO / "prompts" / "fast.md"

REPLY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reply": {"type": "string", "description": "What Ramble says out loud: one or two short sentences, no markdown."},
        "handoff": {"type": "boolean", "description": "True when this is real work for an agent (the slow lane will dispatch it)."},
    },
    "required": ["reply", "handoff"],
}


class FastLane:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.q: queue.Queue = queue.Queue()
        self.history: deque = deque(maxlen=8)  # (ts, who, text)
        self.name_only_until = 0.0
        self.busy = False

    # ---- called from the transcriber thread --------------------------------
    def offer(self, conn, seg, text: str) -> bool:
        """Queue the utterance if it's addressed to Ramble. Returns True if queued."""
        if not self.cfg.get("fast_enabled", True):
            return False
        named = bool(NAME.search(text))
        rest = NAME.sub(" ", text).strip(" ,.!?")
        if named and len(rest.split()) < 2:
            # Just "Ramble?" -> the next utterance is the request.
            self.name_only_until = seg["ended_at"] + self.cfg["fast_name_window_s"]
            log.info("name heard, listening for the request")
            return False
        followup = self._in_followup(conn, seg["started_at"])
        if not (named or followup or seg["started_at"] <= self.name_only_until):
            return False
        self.name_only_until = 0.0
        self.q.put({"seg": dict(seg), "text": text, "why": "named" if named else "follow-up"})
        log.info("addressed (%s): %s", "named" if named else "follow-up", text[:100])
        return True

    def _in_followup(self, conn, started_at: float) -> bool:
        row = conn.execute("SELECT MAX(ended_at) FROM speech_playbacks WHERE ended_at <= ?", (started_at + 1,)).fetchone()
        last_end = row[0] if row else None
        return bool(last_end) and -1 <= started_at - last_end <= self.cfg["followup_s"]

    # ---- worker ------------------------------------------------------------
    def run_forever(self, stop: threading.Event) -> None:
        conn = db.connect()
        while not stop.is_set():
            try:
                item = self.q.get(timeout=1)
            except queue.Empty:
                continue
            # Coalesce a request split across two segments by a short pause.
            items = [item]
            deadline = time.time() + self.cfg["fast_coalesce_s"]
            while time.time() < deadline:
                try:
                    items.append(self.q.get(timeout=max(0.05, deadline - time.time())))
                    deadline = time.time() + self.cfg["fast_coalesce_s"]
                except queue.Empty:
                    break
            try:
                self.busy = True
                self.respond(conn, items)
            except Exception:  # noqa: BLE001
                log.exception("fast lane failed")
            finally:
                self.busy = False

    def build_prompt(self, conn, text: str) -> str:
        now = time.time()
        convo = [f"- {stamp(ts)} {who}: {t}" for ts, who, t in self.history if now - ts < 600]
        asked = []
        for r in conn.execute(
            "SELECT started_at, triage_json FROM thoughts WHERE status='done' AND started_at>=? ORDER BY started_at",
            (now - 900,),
        ):
            for it in json.loads(r["triage_json"] or "{}").get("items", []):
                if it.get("action") == "ask" and it.get("question"):
                    asked.append(f"- {stamp(r['started_at'])} Ramble asked: {it['question']}")
        return "\n".join(
            [
                FAST_PROMPT.read_text(),
                "---",
                f"Now: {time.strftime('%A %Y-%m-%d %-I:%M %p %Z')}",
                "Recent conversation with Ramble (last 10 min):",
                *(convo or ["- none"]),
                "Questions the slow lane asked recently:",
                *(asked or ["- none"]),
                "",
                f"the user just said (speech-to-text): {text}",
            ]
        )

    def run_codex(self, prompt: str) -> dict:
        work = config.TRIAGE_DIR / "tmp"
        work.mkdir(parents=True, exist_ok=True)
        schema = work / "fast.schema.json"
        schema.write_text(json.dumps(REPLY_SCHEMA))
        out = work / f"fast-{time.time():.0f}.json"
        cmd = [
            "codex", "exec", "--ephemeral", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "--ignore-user-config", "--disable", "hooks",
            "-C", str(config.HOME),  # no AGENTS.md here: smaller prompt, faster
            "-m", self.cfg["fast_model"],
            "-c", f'model_reasoning_effort="{self.cfg["fast_effort"]}"',
            "--output-schema", str(schema), "-o", str(out), "-",
        ]
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=clean_env(),
                           timeout=self.cfg["fast_timeout_s"], cwd=str(config.HOME))
        result = out.read_text() if out.exists() else ""
        out.unlink(missing_ok=True)
        if p.returncode != 0 or not result.strip():
            raise RuntimeError(f"codex exited {p.returncode}: {p.stderr[-400:]}")
        return json.loads(result[result.index("{") : result.rindex("}") + 1])

    def respond(self, conn, items: list[dict]) -> None:
        t0 = time.time()
        text = " ".join(i["text"] for i in items)
        seg = items[-1]["seg"]
        try:
            ans = self.run_codex(self.build_prompt(conn, text))
        except Exception:  # noqa: BLE001
            log.exception("fast answer failed")
            ans = {"reply": "Sorry, I couldn't get that.", "handoff": False}
        reply = ans["reply"].strip()
        self.history.append((seg["started_at"], "the user", text))
        self.history.append((time.time(), "Ramble", reply))
        # Leave a trace in the thought so the slow lane knows this was answered or handed off.
        tid = conn.execute("SELECT thought_id FROM segments WHERE id=?", (seg["id"],)).fetchone()
        if tid and tid[0]:
            note = f"[{stamp(time.time())}] (Ramble replied: {reply}{' [handoff: dispatch this]' if ans['handoff'] else ''})"
            conn.execute("UPDATE thoughts SET text=text || char(10) || ? WHERE id=?", (note, tid[0]))
        log.info("fast reply in %.1fs (handoff=%s): %s", time.time() - t0, ans["handoff"], reply)
        if not self.cfg.get("fast_speak", True):  # tests
            return
        try:
            from . import speech

            speech.speak(reply, self.cfg)
        except Exception:  # noqa: BLE001
            log.exception("could not speak fast reply")
        log.info("spoken %.1fs after request", time.time() - seg["ended_at"])
