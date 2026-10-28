"""Passive stage timing in the rotating app log; never claims acoustic output onset."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import time

log = logging.getLogger("ramble.latency")
_request = contextvars.ContextVar("latency_request", default=None)


def event(stage: str, **fields) -> None:
    # No transcripts, prompts, or credentials. Logging must not break a reply.
    try:
        log.info("latency %s", json.dumps(dict(
            stage=stage, wall_s=time.time(), mono_s=time.monotonic(),
            request_id=_request.get(), **fields), separators=(",", ":")))
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def request(request_id: str):
    token = _request.set(request_id)
    try:
        yield
    finally:
        _request.reset(token)


@contextlib.contextmanager
def span(stage: str, **fields):
    start = time.monotonic()
    event(stage + "_start", **fields)
    try:
        yield
    except BaseException:
        event(stage + "_end", elapsed_s=time.monotonic() - start, ok=False, **fields)
        raise
    else:
        event(stage + "_end", elapsed_s=time.monotonic() - start, ok=True, **fields)


def measured(stage: str):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            with span(stage):
                return fn(*args, **kwargs)
        return wrapped
    return decorate
