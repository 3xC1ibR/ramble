"""Observe new ambient dispatches and route explicit Stop clicks to their owner."""
from __future__ import annotations

from dataclasses import dataclass, replace
import logging
import queue
import sqlite3
import threading
import time
import uuid

from . import config

log = logging.getLogger("ramble.dispatch_activity")
NOTICE_SECONDS = 12
RESULT_SECONDS = 6


@dataclass(frozen=True)
class DispatchTarget:
    dispatch_id: str
    thread_id: str
    turn_id: str | None


@dataclass(frozen=True)
class DispatchNotice:
    dispatch_id: str
    thread_id: str
    name: str
    expires_at: float
    mode: str = "background"
    target: DispatchTarget | None = None
    detail: str = ""


class DispatchActivityMonitor:
    """Keep CLI/SQLite operations off AppKit and the microphone activity worker.

    Only dispatches created after this monitor starts produce a notice. Watching
    a thread never submits work. Stop uses the identity shown when clicked,
    including its observed turn, even if another dispatch appears meanwhile.
    """
    def __init__(self, cfg, *, backend=None):
        if backend is None:
            from .session_backend import SessionBackend
            backend = SessionBackend(cfg)
        self.backend = backend
        self.current = None
        self.since = time.time()
        self.seen = set()
        self.requests = queue.Queue()
        self.wake = threading.Event()
        self.pending = {}
        self.watching = set()
        self.finished = False
        self.stop_confirmed = None
        self.stop_error = None

    def start(self, stop):
        threading.Thread(target=self.run, args=(stop,), name="dispatch-activity", daemon=True).start()

    def run(self, stop):
        while not stop.is_set():
            try:
                self.update()
            except Exception:
                log.warning("Background work status unavailable", exc_info=True)
                if self.current:
                    self.current = replace(self.current, target=None,
                                           detail="Work status unavailable; check Ribbon Field.")
            self.wake.wait(1)
            self.wake.clear()

    def request_stop(self, target):
        if target is not None:
            self.requests.put(target)
            self.wake.set()

    def _scan(self):
        if not config.DB_PATH.exists():
            return
        conn = sqlite3.connect(f"{config.DB_PATH.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id,name,thread_id FROM dispatches WHERE status='created' "
                "AND thread_id IS NOT NULL AND created_at>=? ORDER BY created_at,id",
                (self.since,),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            if row["id"] in self.seen:
                continue
            self.seen.add(row["id"])
            self.current = DispatchNotice(row["id"], row["thread_id"], row["name"],
                                          time.monotonic() + NOTICE_SECONDS)
            self.finished = False
            self.stop_confirmed = self.stop_error = None

    def _mode(self, mode, *, detail="", target=None):
        notice = self.current
        expiry = notice.expires_at if notice.mode == mode else time.monotonic() + RESULT_SECONDS
        self.current = replace(notice, mode=mode, target=target, detail=detail, expires_at=expiry)

    def _receipt(self, request_id, target, receipt):
        status = receipt.get("status")
        if status not in {"completed", "failed", "uncertain", "cancelled"}:
            self.pending[request_id] = target
            return
        self.pending.pop(request_id, None)
        if not self.current or self.current.dispatch_id != target.dispatch_id:
            return
        if status == "completed":
            self.stop_confirmed = target
        else:
            self.stop_error = receipt.get("error") or "Stop could not be confirmed."
            self._mode("stop_error", detail=self.stop_error)

    def update(self):
        self._scan()
        for request_id, target in list(self.pending.items()):
            try:
                receipt = self.backend.status(request_id)
            except Exception as exc:
                if "App request not found" not in str(exc):
                    raise
                receipt = self.backend.command(request_id, target.thread_id, "interrupt",
                                               expected=target.turn_id)
            self._receipt(request_id, target, receipt)
        while True:
            try:
                target = self.requests.get_nowait()
            except queue.Empty:
                break
            if target in self.pending.values():
                continue
            request_id = "mini-stop-" + uuid.uuid4().hex
            if self.current and self.current.dispatch_id == target.dispatch_id:
                self.stop_error = None
                self._mode("stopping", detail="Stopping " + self.current.name)
            # Record before submission; a lost CLI response is reconciled by ID.
            self.pending[request_id] = target
            try:
                receipt = self.backend.command(request_id, target.thread_id, "interrupt",
                                               expected=target.turn_id)
            except Exception:
                log.warning("Stop receipt pending for %s", target.dispatch_id, exc_info=True)
            else:
                self._receipt(request_id, target, receipt)
        notice = self.current
        if notice is None or self.finished:
            return
        if notice.dispatch_id not in self.watching:
            receipt = self.backend.command("mini-watch-" + notice.dispatch_id,
                                           notice.thread_id, "watch")
            if receipt.get("status") == "completed":
                self.watching.add(notice.dispatch_id)
            elif receipt.get("status") in {"failed", "uncertain", "cancelled"}:
                raise RuntimeError(receipt.get("error") or "Could not watch background work.")
            else:
                return
        live = self.backend.snapshot(notice.thread_id, 0)
        if not live.get("connected") or live.get("status") in {"connecting", "disconnected"}:
            self.current = replace(self.current, target=None, detail="Connecting to " + notice.name)
            return
        target = DispatchTarget(notice.dispatch_id, notice.thread_id, live.get("active_turn_id"))
        stopping = any(t.dispatch_id == notice.dispatch_id for t in self.pending.values())
        if stopping or (self.stop_confirmed == target and target.turn_id is not None):
            self._mode("stopping", detail="Stopping " + notice.name)
        elif self.stop_confirmed and not target.turn_id:
            self._mode("stopped", detail="Stopped " + notice.name)
            self.finished = True
        elif not target.turn_id and live.get("last_turn_id"):
            outcome = {"failed": "Failed ", "interrupted": "Stopped "}.get(live.get("status"), "Finished ")
            self._mode("stopped", detail=outcome + notice.name)
            self.finished = True
        elif self.stop_error:
            self._mode("stop_error", detail=self.stop_error, target=target)
        else:
            self._mode("background", detail="Started automatically: " + notice.name, target=target)
