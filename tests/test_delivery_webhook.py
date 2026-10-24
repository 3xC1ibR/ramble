"""HTTP + guarded playback checks; no speakers, mic, agents or real-home writes."""

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

from ramble import config, db, speech
from ramble.delivery_webhook import DEFAULTS, DeliveryServer, init_secret, load_settings


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ramble-delivery-", dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        paths = {"HOME": self.home, "DB_PATH": self.home / "ramble.db",
                 "CONFIG_PATH": self.home / "config.json"}
        paths.update({name: self.home / name.lower() for name in
                      ("AUDIO_DIR", "DIGEST_DIR", "LOG_DIR", "TRIAGE_DIR")})
        patcher = patch.multiple(config, **paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token = "test-token-" + "x" * 32
        self.now = 1000
        self.wav = self.home / "fixture.wav"
        with wave.open(str(self.wav), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(b"\0\0" * 1600)
        self.generated = speech.SpeechResult(self.wav, "fixture", .1)
        synth = patch.object(speech, "synthesize", return_value=self.generated)
        self.synth = synth.start()
        self.addCleanup(synth.stop)
        player = patch.object(speech.subprocess, "run", side_effect=self.player)
        self.player_mock = player.start()
        self.addCleanup(player.stop)
        self.start_server()

    def player(self, args, **kwargs):
        self.assertEqual(args, ["/usr/bin/afplay", str(self.wav)])
        conn = db.connect()
        try:
            self.assertTrue(speech.overlaps_playback(conn, time.time(), time.time() + .01))
        finally:
            conn.close()

    def start_server(self):
        self.server = DeliveryServer(("127.0.0.1", 0), self.home, self.token,
                                     copy.deepcopy(DEFAULTS), clock=lambda: self.now)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01})
        thread.start()
        server = self.server
        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, body=None, *, token=True, raw=None, content_type="application/json",
                method="POST", path="/delivery-open"):
        data = raw if raw is not None else json.dumps(body or {"app": "ubereats"}).encode()
        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = "Bearer " + (self.token if token is True else token)
        request = urllib.request.Request(self.url + path, data=data if method == "POST" else None,
                                         headers=headers, method=method)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, json.loads(response.read())

    def test_authenticated_event_reaches_existing_guarded_playback(self):
        status, body = self.request({"app": "ubereats", "device": "iphone", "tone": "firm"})
        self.assertEqual((status, body["status"], body["playback"]), (200, "spoken", "mac"))
        self.assertIn("Uber Eats", body["text"])
        self.synth.assert_called_once()
        self.player_mock.assert_called_once()

    def test_missing_wrong_and_unicode_credentials_do_not_speak_or_consume_cooldown(self):
        for token in (False, "wrong", "caf\xe9"):
            self.assertEqual(self.request(token=token)[0], 401)
        self.player_mock.assert_not_called()
        self.assertEqual(self.request()[1]["status"], "spoken")

    def test_rejects_bad_payloads_and_http_contract(self):
        for raw in (b"{", b"null", b"[]", b'"hello"', b'\xff', b"x" * 2049):
            self.assertIn(self.request(raw=raw)[0], (400, 413))
        for payload in ({"app": "other"}, {"app": []}, {"app": "doordash", "text": "say this"},
                        {"app": "doordash", "tone": []}, {"app": "doordash", "device": ""}):
            self.assertEqual(self.request(payload)[0], 400)
        self.assertEqual(self.request(content_type="text/plain")[0], 415)
        self.assertEqual(self.request(path="/other")[0], 404)
        self.assertEqual(self.request(path="/delivery-open?token=wrong", token=False)[0], 401)
        self.player_mock.assert_not_called()

    def test_health_requires_auth_and_never_speaks(self):
        self.assertEqual(self.request(method="GET", path="/healthz", token=False)[0], 401)
        self.assertEqual(self.request(method="GET", path="/healthz"), (200, {"status": "ready"}))
        self.assertEqual(self.request(method="GET")[0], 404)
        self.player_mock.assert_not_called()

    def test_global_cooldown_covers_both_apps_and_devices_then_expires(self):
        self.request()
        status, body = self.request({"app": "doordash", "device": "ipad"})
        self.assertEqual((status, body["status"], body["retry_after_s"]), (200, "suppressed", 90))
        self.now += 90
        self.assertEqual(self.request({"app": "doordash"})[1]["status"], "spoken")
        self.assertEqual(self.player_mock.call_count, 2)

    def test_cooldown_survives_server_restart(self):
        self.assertEqual(self.request()[1]["status"], "spoken")
        self.start_server()
        self.assertEqual(self.request()[1]["status"], "suppressed")
        self.player_mock.assert_called_once()

    def test_simultaneous_open_is_suppressed_while_playing(self):
        entered, release = threading.Event(), threading.Event()
        def slow_speaker(text):
            entered.set()
            release.wait(3)
            return self.generated
        self.server.speaker = slow_speaker
        result = []
        thread = threading.Thread(target=lambda: result.append(self.request()))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertEqual(self.request()[1]["reason"], "playback_in_progress")
        finally:
            release.set()
            thread.join()
        self.assertEqual(result[0][1]["status"], "spoken")

    def test_speech_failure_surfaces_and_retains_retry_cooldown(self):
        with patch.object(speech, "speak", side_effect=OSError("player failed")):
            with self.assertLogs("ramble.delivery", level="ERROR"):
                self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.request()[1]["status"], "suppressed")

    def test_private_secret_is_stable_and_custom_callouts_load(self):
        path = init_secret(self.home)
        before = path.read_text()
        self.assertGreaterEqual(len(before.strip()), 32)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        init_secret(self.home)
        self.assertEqual(path.read_text(), before)
        (self.home / "delivery-webhook.json").write_text(json.dumps({"tone": "firm", "cooldown_s": 30}))
        self.assertEqual(load_settings(self.home)["tone"], "firm")
        (self.home / "delivery-webhook.json").write_text('{"cooldown_s": 0}')
        with self.assertRaises(ValueError):
            load_settings(self.home)


if __name__ == "__main__":
    unittest.main()
