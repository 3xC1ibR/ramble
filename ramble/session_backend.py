"""Supported Agent Coord CLI boundary; no private app ports or shared DB writes."""
from __future__ import annotations
import json
import subprocess
from . import model_settings
from .router import clean_env, ensure_coord_session


class SessionBackend:
    def __init__(self, cfg):
        self.cfg = cfg
        self.caller = None

    def call(self, args, text=None):
        result = subprocess.run(
            ["agent-coord", *args],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
            env=clean_env(),
        )
        if result.returncode:
            raise RuntimeError(
                (result.stderr or result.stdout or "Agent Coord unavailable")[-1000:]
            )
        return json.loads(result.stdout)

    def sender(self):
        if not self.caller:
            self.caller = ensure_coord_session()
        return self.caller

    def create(self, command_id, payload):
        # New commands freeze the chosen pair before queuing; legacy commands
        # without these keys keep their previous defaults behavior.
        selected = ({"model": payload["model"], "effort": payload["effort"]}
                    if "model" in payload and "effort" in payload
                    else model_settings.selection(self.cfg, "work"))
        args = [
            "thread",
            "create",
            "--from-session",
            self.sender(),
            "--cwd",
            payload["cwd"],
            "--name",
            payload["name"],
            "--client",
            "codex",
            "--no-subscribe",
            "--interactive",
            "--wait",
            "0",
            "--request-id",
            command_id,
        ]
        if self.cfg.get("work_session_yolo", False):
            args.append("--yolo")
        if selected["model"]:
            args += ["--model", selected["model"]]
        if selected["effort"]:
            args += ["--effort", selected["effort"]]
        return self.call([*args, "-"], payload["prompt"])

    def command(
        self, command_id, thread, action, *, message=None, expected=None, adopt=False
    ):
        args = [
            "session-control",
            action,
            "--thread",
            thread,
            "--from-session",
            self.sender(),
            "--request-id",
            command_id,
            "--wait",
            "0",
        ]
        if adopt:
            args.append("--adopt")
        if expected:
            args += ["--expected-turn-id", expected]
        if message is not None:
            args += ["--message", "-"]
        return self.call(args, message)

    def settings(self, thread):
        return self.call(["thread", "settings", "--session-id", thread])

    def status(self, command_id):
        return self.call(["thread", "request-status", "--request-id", command_id])

    def cancel(self, command_id):
        return self.call(["thread", "cancel-request", "--request-id", command_id])

    def snapshot(self, thread, after):
        return self.call(
            [
                "session-control",
                "snapshot",
                "--thread",
                thread,
                "--from-session",
                self.sender(),
                "--after",
                str(after),
            ]
        )
