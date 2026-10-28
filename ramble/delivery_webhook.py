"""Authenticated Shortcuts app-open events -> Ramble's existing guarded speech.

Run with the repo venv: python -m ramble.delivery_webhook --init, then without
arguments to serve. Secrets and duplicate state stay under RAMBLE_HOME.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, state

log = logging.getLogger("ramble.delivery")
APPS = {"ubereats": "Uber Eats", "doordash": "DoorDash"}
DEFAULTS = {
    "cooldown_s": 90,
    "tone": "teasing",
    "callouts": {
        "teasing": [
            "{app} again? Your fridge would like a word.",
            "Opening {app}? Those delivery fees are getting a little too comfortable.",
            "Step away from {app}. The kitchen is still accepting walk-ins.",
        ],
        "firm": [
            "Close {app}. Check the kitchen before paying delivery fees.",
            "Pause. Give it ten minutes before ordering from {app}.",
        ],
    },
}


def load_settings(home: Path) -> dict:
    settings = dict(DEFAULTS)
    path = home / "delivery-webhook.json"
    if path.exists():
        overrides = json.loads(path.read_text())
        if not isinstance(overrides, dict) or set(overrides) - set(DEFAULTS):
            raise ValueError("invalid delivery-webhook.json keys")
        settings.update(overrides)
    cooldown = settings["cooldown_s"]
    if (isinstance(cooldown, bool) or not isinstance(cooldown, (int, float))
            or not math.isfinite(cooldown) or not 10 <= cooldown <= 3600):
        raise ValueError("cooldown_s must be between 10 and 3600")
    if settings["tone"] not in ("teasing", "firm"):
        raise ValueError("tone must be teasing or firm")
    callouts = settings["callouts"]
    if not isinstance(callouts, dict) or set(callouts) != {"teasing", "firm"}:
        raise ValueError("callouts needs teasing and firm lists")
    for lines in callouts.values():
        if not isinstance(lines, list) or not lines:
            raise ValueError("callouts must be nonempty lists")
        for line in lines:
            if not isinstance(line, str) or not line.strip() or len(line) > 240:
                raise ValueError("callouts must be short nonempty strings")
            if len(line.format(app="Uber Eats")) > 300:
                raise ValueError("formatted callout too long")
    return settings


def init_secret(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    path = home / "delivery-webhook.token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path  # Reinstallation preserves devices' existing credential.
    with os.fdopen(fd, "w") as stream:
        stream.write(secrets.token_urlsafe(32) + "\n")
    return path


class DeliveryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, home: Path, token: str, settings: dict,
                 speaker=None, clock=time.time):
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError("webhook token must be at least 32 ASCII characters without whitespace")
        self.home, self.token, self.settings = home, token, settings
        self.speaker, self.clock = speaker, clock
        self.busy = threading.Lock()
        home.mkdir(parents=True, exist_ok=True)
        self.store = home / "delivery-webhook.sqlite3"
        with self.connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS trigger_state "
                         "(id INTEGER PRIMARY KEY CHECK(id=1), accepted_at REAL NOT NULL)")
        super().__init__(address, DeliveryHandler)

    def connection(self):
        # Closing context manager: sqlite's native context only commits/rolls back.
        from contextlib import closing
        return closing(sqlite3.connect(self.store, timeout=5, isolation_level=None))

    def trigger(self, payload):
        # Validate everything before acquiring a slot or touching the cooldown.
        if not isinstance(payload, dict) or set(payload) - {"app", "device", "tone"}:
            return 400, {"error": "expected app, optional device and tone"}
        app, device = payload.get("app"), payload.get("device", "unknown")
        tone = payload.get("tone", self.settings["tone"])
        if not isinstance(app, str) or app not in APPS:
            return 400, {"error": "app must be ubereats or doordash"}
        if not isinstance(device, str) or not 1 <= len(device) <= 64 or not device.isprintable():
            return 400, {"error": "device must be 1-64 printable characters"}
        if not isinstance(tone, str) or tone not in ("teasing", "firm"):
            return 400, {"error": "tone must be teasing or firm"}
        if state.is_paused():
            return 200, {"status": "suppressed", "reason": "ramble_paused"}
        if not self.busy.acquire(blocking=False):
            return 200, {"status": "suppressed", "reason": "playback_in_progress"}
        try:
            now = self.clock()
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT accepted_at FROM trigger_state WHERE id=1").fetchone()
                if row and now - row[0] < self.settings["cooldown_s"]:
                    remaining = math.ceil(self.settings["cooldown_s"] - (now - row[0]))
                    conn.rollback()
                    return 200, {"status": "suppressed", "reason": "cooldown",
                                 "retry_after_s": remaining}
                conn.execute("INSERT OR REPLACE INTO trigger_state VALUES (1,?)", (now,))
                conn.commit()  # Survives process restart and Shortcuts retries.
            text = secrets.choice(self.settings["callouts"][tone]).format(app=APPS[app])
            try:
                if self.speaker is None:
                    from .speech import speak
                    result = speak(text)  # Same HOME, speech lock and feedback guard as app.
                else:
                    result = self.speaker(text)
            except state.PausedError:
                return 200, {"status": "suppressed", "reason": "ramble_paused"}
            except Exception:
                log.exception("delivery callout failed")
                # May already have played partially: retain cooldown to prevent a loop.
                return 503, {"error": "speech_failed", "retry_after_s": self.settings["cooldown_s"]}
            log.info("delivery callout played: app=%s engine=%s", app, result.engine)
            return 200, {"status": "spoken", "text": text, "playback": "mac",
                         "engine": result.engine, "duration_s": result.duration}
        finally:
            self.busy.release()


class DeliveryHandler(BaseHTTPRequestHandler):
    server_version = "RambleDelivery/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, fmt, *args):
        pass  # Do not log headers, secrets, arbitrary paths or request bodies.

    def respond(self, status, body):
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def authorized(self):
        provided = self.headers.get("Authorization", "")
        if not hmac.compare_digest(provided.encode(), ("Bearer " + self.server.token).encode()):
            self.respond(401, {"error": "unauthorized"})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        self.respond(200 if self.path == "/healthz" else 404,
                     {"status": "ready"} if self.path == "/healthz" else {"error": "not_found"})

    def do_POST(self):
        if not self.authorized():
            return
        if self.path != "/delivery-open":
            self.respond(404, {"error": "not_found"})
            return
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "expected application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if self.headers.get("Transfer-Encoding") or not 0 < length <= 2048:
            self.respond(413, {"error": "expected body of 1-2048 bytes"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            payload = json.loads(raw)
        except (ValueError, OSError):
            self.respond(400, {"error": "invalid_json"})
            return
        status, body = self.server.trigger(payload)
        self.respond(status, body)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init", action="store_true", help="create a private token, never print it")
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args()
    if args.init:
        print(f"Token file: {init_secret(config.HOME)}")
        return
    token_path = config.HOME / "delivery-webhook.token"
    if token_path.stat().st_mode & 0o077:
        parser.error("token file must be private: chmod 600 " + str(token_path))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with DeliveryServer(("127.0.0.1", args.port), config.HOME,
                        token_path.read_text().strip(), load_settings(config.HOME)) as server:
        log.info("delivery webhook listening on 127.0.0.1:%s", args.port)
        server.serve_forever()


if __name__ == "__main__":
    main()
