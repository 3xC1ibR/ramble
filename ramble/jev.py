"""Small, bounded Jev classifier. Never executes or generates an assistant reply."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import config, usage

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE = 65536


class JevError(Exception):
    """Safe diagnostic code, never response bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JevClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._slot = threading.Lock()
        self.unavailable_until = 0.0
        self._token = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
        path = (cfg.get("jev_api_key_file") or os.environ.get("JEV_API_KEY_FILE")
                or os.environ.get("TYPESAFE_API_KEY_FILE"))
        if not self._token and path:
            try:
                self._token = Path(path).expanduser().read_text().strip()
            except OSError:
                pass

    @property
    def available(self):
        return bool(self._token) and time.monotonic() >= self.unavailable_until

    def classify(self, conversation, utterance, seconds_since_reply):
        def parse(answer, model):
            scores = self.parse(answer, model)
            threshold = float(self.cfg.get("chat_followup_threshold", .8))
            if not math.isfinite(threshold) or not .5 < threshold <= 1:
                raise JevError("invalid_threshold")
            return {**scores, "accept": all(v >= threshold for v in scores.values())}

        return self.evaluate(
            {"recent_conversation": conversation[-8:], "new_utterance": utterance,
             "seconds_since_assistant_reply": seconds_since_reply},
            json.loads((config.REPO / "prompts/chat-followup.json").read_text()), parse,
        )

    def evaluate(self, content, questions, parse):
        """Reuse the bounded transport; each consumer validates its own answers."""
        if not self.available or not self._slot.acquire(blocking=False):
            raise JevError("unavailable")
        # Only one transport, even if a system DNS call outlives our deadline.
        # A stuck call cannot accumulate threads or block addressed speech.
        result = queue.Queue(maxsize=1)
        try:
            timeout = min(10.0, max(.1, float(self.cfg.get("jev_timeout_s", 3))))
            deadline = time.monotonic() + timeout
            payload = {
                "model": self.cfg.get("jev_model", "jev-1.13.0"),
                "state": content,
                "questions": questions,
            }
            body = json.dumps(payload, ensure_ascii=False).encode()
            if len(body) > 65536:
                raise JevError("request_too_large")
        except BaseException:
            self._slot.release()
            raise

        def send():
            try:
                result.put(self._post(body, deadline))
            except urllib.error.HTTPError as exc:
                result.put(JevError("http_" + str(exc.code)))
                exc.close()
            except JevError as exc:
                result.put(exc)
            except Exception:
                result.put(JevError("transport_error"))
            finally:
                self._slot.release()

        threading.Thread(target=send, name="jev-request", daemon=True).start()
        call_id, started_at = "jev:" + uuid.uuid4().hex, time.time()
        counts, outcome = {}, "failed"
        try:
            try:
                answer = result.get(timeout=max(.001, deadline - time.monotonic()))
            except queue.Empty:
                raise JevError("timeout") from None
            if isinstance(answer, Exception):
                raise answer
            counts = usage.normalize(answer.get("usage") if isinstance(answer, dict) else None, "jev")
            parsed = parse(answer, payload["model"])
            outcome = "completed"
            return parsed
        except JevError:
            self.unavailable_until = time.monotonic() + 30
            raise
        finally:
            usage.record_call(call_id, started_at=started_at, provider="jev", model=payload["model"],
                              purpose="triage_filter" if "thought_input" in content else "chat_followup",
                              outcome=outcome, counts=counts)

    def _post(self, body, deadline):
        request = urllib.request.Request(ENDPOINT, data=body, headers={
            "Authorization": "Bearer " + self._token,
            "Content-Type": "application/json", "Accept": "application/json",
            "Accept-Encoding": "identity",
        })
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=max(.001, deadline - time.monotonic())) as response:
            if response.status != 200:
                raise JevError("http_" + str(response.status))
            data = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise JevError("timeout")
                chunk = response.read1(min(8192, MAX_RESPONSE + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    raise JevError("response_too_large")
            try:
                return json.loads(data)
            except (ValueError, UnicodeError):
                raise JevError("invalid_json") from None

    @staticmethod
    def parse(response, model):
        if not isinstance(response, dict) or response.get("model") != model:
            raise JevError("invalid_model")
        answers = response.get("answers")
        if not isinstance(answers, dict) or set(answers) != {"directed", "related"}:
            raise JevError("invalid_answers")
        scores = {}
        for key, answer in answers.items():
            if not isinstance(answer, dict) or answer.get("type") != "noul":
                raise JevError("invalid_answer")
            value = answer.get("noul")
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise JevError("invalid_score")
            scores[key] = value
        return scores
