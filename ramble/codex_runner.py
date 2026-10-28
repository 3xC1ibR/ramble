"""Bound fast Codex calls, including server-directed retry backoff."""

from __future__ import annotations

import logging
import math
import os
import re
import signal
import subprocess
import tempfile
import time

log = logging.getLogger("ramble.codex")


class RateLimitedError(RuntimeError):
    def __init__(self, retry_after_s: float | None = None):
        super().__init__("Codex response service returned HTTP 429 (rate limited)")
        self.retry_after_s = retry_after_s


class CancelledError(RuntimeError):
    pass


def _rate_limit(stderr: str) -> RateLimitedError | None:
    # Only inspect Codex's retry diagnostic, not echoed prompts or tool output.
    for line in stderr.splitlines():
        if "codex_core::responses_retry:" not in line or "429 Too Many Requests" not in line:
            continue
        delay = re.search(r"retrying sampling request \(\d+/\d+ in ([\d.]+)s\)", line)
        seconds = float(delay[1]) if delay else None
        return RateLimitedError(math.ceil(seconds) if seconds is not None and math.isfinite(seconds) else None)
    return None


def run(cmd, *, input: str, env: dict, timeout: float, cwd: str,
        trace=None, on_wait=None, cancelled=None):
    env = dict(env)
    # The CLI otherwise hides 429 behind a retry that can exceed our entire
    # voice-response deadline. Keep this narrow: never log request/auth payloads.
    env["RUST_LOG"] = ",".join(filter(None, (env.get("RUST_LOG"), "codex_core::responses_retry=warn")))
    started = time.monotonic()
    deadline = started + timeout
    # communicate(input=...) with short polling deadlines can stop writing a
    # large prompt: subsequent communicate(input=None) calls no longer register
    # stdin for writes. The child waits forever for the rest/EOF. An anonymous,
    # private file gives it the complete prompt independently of output polling.
    with tempfile.TemporaryFile(dir=cwd) as prompt_file:
        prompt_file.write(input.encode())
        prompt_file.seek(0)
        return _run(cmd, prompt_file, env=env, cwd=cwd, started=started, deadline=deadline,
                    timeout=timeout, trace=trace, on_wait=on_wait, cancelled=cancelled)


def _run(cmd, prompt_file, *, env, cwd, started, deadline, timeout, trace, on_wait, cancelled):
    with subprocess.Popen(cmd, stdin=prompt_file, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env, cwd=cwd,
                          start_new_session=True) as process:
        try:
            if trace:
                trace.event("process_started", pid=process.pid)
            while True:
                if cancelled and cancelled():
                    raise CancelledError("Codex call cancelled")
                remaining = deadline - time.monotonic()
                try:
                    stdout, stderr = process.communicate(timeout=max(0, min(.1, remaining)))
                    if trace:
                        trace.consume(stdout, stderr, final=True)
                    limited = _rate_limit(stderr)
                    if limited:
                        raise limited
                    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired as exc:
                    if trace:
                        trace.consume(exc.output, exc.stderr)
                    if len(exc.output or b"") + len(exc.stderr or b"") > 4 * 1024 * 1024:
                        raise RuntimeError("Codex output exceeded the 4 MiB limit") from None
                    stderr = exc.stderr or b""
                    if isinstance(stderr, bytes):
                        stderr = stderr.decode(errors="replace")
                    limited = _rate_limit(stderr)
                    if limited:
                        log.warning("Codex HTTP 429; retry_after_s=%s", limited.retry_after_s)
                        raise limited
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(cmd, timeout, output=exc.output, stderr=exc.stderr) from None
                    if on_wait:
                        on_wait(time.monotonic() - started)
        finally:
            # codex may be an npm launcher with a native child and tool children.
            # Kill the owned group even if the launcher exited ahead of a child.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired as exc:
                # Even a detached tool retaining pipe handles cannot hold us open.
                stdout, stderr = exc.output, exc.stderr
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe:
                        pipe.close()
            if trace:
                trace.consume(stdout, stderr, final=True)
                trace.event("process_reaped", returncode=process.returncode)
