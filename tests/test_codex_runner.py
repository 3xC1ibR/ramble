"""Exercise real pipes and process cleanup without models, mic or playback."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from ramble.codex_runner import CancelledError, RateLimitedError, run


class CodexRunnerTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)

    def run_script(self, script, timeout=3, text="request"):
        return run([sys.executable, "-c", script], input=text,
                   env=dict(os.environ), timeout=timeout, cwd=self.home.name)

    def test_success_preserves_input_and_output_across_poll_timeouts(self):
        result = self.run_script("import sys,time; text=sys.stdin.read(); time.sleep(.25); print(text); print('diagnostic',file=sys.stderr)")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "request")
        self.assertEqual(result.stderr.strip(), "diagnostic")

    def test_large_prompt_reaches_slow_reader_after_multiple_poll_timeouts(self):
        prompt = 'speech context ☀\n' * 8192
        result = self.run_script("import sys,time,hashlib; time.sleep(.35); text=sys.stdin.read(); print(hashlib.sha256(text.encode()).hexdigest())", text=prompt)
        import hashlib
        self.assertEqual(result.stdout.strip(), hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(list(Path(self.home.name).iterdir()), [])

    def test_rate_limit_fails_promptly_with_server_delay(self):
        start = time.monotonic()
        with self.assertRaises(RateLimitedError) as error:
            self.run_script("import sys,time; print('WARN codex_core::responses_retry: retrying sampling request (1/5 in 495.999927s)... last status: 429 Too Many Requests',file=sys.stderr,flush=True); time.sleep(30)")
        self.assertEqual(error.exception.retry_after_s, 496)
        self.assertLess(time.monotonic() - start, 2)

    def test_echoed_429_does_not_abort_valid_reply(self):
        result = self.run_script("import sys; print('the user asked: why 429 Too Many Requests?',file=sys.stderr); print('answer')")
        self.assertEqual(result.stdout.strip(), "answer")

    def test_deadline_kills_launcher_and_its_child(self):
        marker = Path(self.home.name) / "orphan-ran"
        child = f"import time; from pathlib import Path; time.sleep(.5); Path({str(marker)!r}).touch()"
        script = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
        with self.assertRaises(subprocess.TimeoutExpired) as error:
            self.run_script(script, timeout=.15)
        self.assertEqual(error.exception.timeout, .15)
        time.sleep(.6)
        self.assertFalse(marker.exists(), "native/tool child survived the deadline")

    def test_wait_notice_and_cancellation_are_polled_without_waiting_for_deadline(self):
        polls = []
        started = time.monotonic()
        with self.assertRaises(CancelledError):
            run([sys.executable,'-c','import time; time.sleep(20)'], input='request',
                env=dict(os.environ), timeout=10, cwd=self.home.name,
                on_wait=polls.append, cancelled=lambda: len(polls) >= 2)
        self.assertEqual(len(polls), 2)
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
