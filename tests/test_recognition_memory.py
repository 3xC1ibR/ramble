"""Memory policy tests with a fake MLX runtime; no GPU, mic, audio or workers."""

import os
import unittest
from unittest.mock import Mock, patch

from ramble.recognition_memory import MIB, REFRESH_ENV, RecognitionMemory


class RecognitionMemoryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        self.mx = Mock()
        self.mx.device_info.return_value = {
            "memory_size": 16 * 1024 * MIB,
            "max_recommended_working_set_size": 12 * 1024 * MIB,
        }
        self.mx.set_wired_limit.return_value = 0
        self.mx.set_cache_limit.return_value = 8 * 1024 * MIB
        self.mx.get_active_memory.return_value = 1543 * MIB
        self.mx.get_cache_memory.return_value = 120 * MIB
        self.mx.get_peak_memory.return_value = 2400 * MIB
        importer = patch("ramble.recognition_memory.importlib.import_module", return_value=self.mx)
        self.importer = importer.start()
        self.addCleanup(importer.stop)

    def test_refresh_is_configured_before_mlx_and_limits_apply_once(self):
        def load(name):
            self.assertEqual(name, "mlx.core")
            self.assertEqual(os.environ[REFRESH_ENV], "1000")
            return self.mx
        self.importer.side_effect = load
        memory = RecognitionMemory({})
        memory.prepare()
        memory.prepare()
        self.importer.assert_called_once()
        self.mx.set_wired_limit.assert_called_once_with(2048 * MIB)
        self.mx.set_cache_limit.assert_called_once_with(128 * MIB)

    def test_oversized_settings_are_bounded_by_ram_and_metal(self):
        self.mx.device_info.return_value = {"memory_size": 4096 * MIB,
                                           "max_recommended_working_set_size": 768 * MIB}
        memory = RecognitionMemory({"whisper_wired_mb": 64000, "whisper_cache_mb": 64000})
        memory.prepare()
        self.mx.set_wired_limit.assert_called_once_with(768 * MIB)
        self.mx.set_cache_limit.assert_called_once_with(256 * MIB)

    def test_disable_restores_original_behavior(self):
        memory = RecognitionMemory({"whisper_wired_mb": 0, "whisper_cache_mb": None})
        memory.prepare()
        self.assertEqual(os.environ[REFRESH_ENV], "0")
        self.mx.set_cache_limit.assert_not_called()
        self.mx.set_wired_limit.assert_called_once_with(0)

    def test_refresh_interval_cannot_overflow_native_integer(self):
        RecognitionMemory({"whisper_residency_refresh_ms": 10**100}).prepare()
        self.assertEqual(os.environ[REFRESH_ENV], "60000")

    def test_off_releases_and_resume_restores_without_repeated_calls(self):
        memory = RecognitionMemory({})
        memory.prepare()
        memory.set_resident(False)
        memory.set_resident(False)
        self.assertEqual(memory.snapshot()["wired_limit_bytes"], 0)
        memory.set_resident(True)
        self.assertEqual([c.args[0] for c in self.mx.set_wired_limit.call_args_list],
                         [2048 * MIB, 0, 2048 * MIB])

    def test_close_restores_preexisting_limits_once(self):
        self.mx.set_wired_limit.return_value = 64 * MIB
        memory = RecognitionMemory({})
        memory.prepare()
        memory.close()
        memory.close()
        self.assertEqual([c.args[0] for c in self.mx.set_wired_limit.call_args_list],
                         [2048 * MIB, 64 * MIB])
        self.assertEqual([c.args[0] for c in self.mx.set_cache_limit.call_args_list],
                         [128 * MIB, 8192 * MIB])

    def test_unsupported_wiring_keeps_cache_limit_and_does_not_retry_forever(self):
        self.mx.set_wired_limit.side_effect = RuntimeError("Metal residency unavailable")
        memory = RecognitionMemory({})
        with self.assertLogs("ramble.transcribe", level="WARNING"):
            memory.prepare()
        memory.set_resident(True)
        self.mx.set_wired_limit.assert_called_once()
        self.assertEqual(memory.snapshot()["wired_limit_bytes"], 0)
        self.mx.set_cache_limit.assert_called_once_with(128 * MIB)

    def test_unavailable_runtime_does_not_escape(self):
        self.importer.side_effect = ImportError("MLX unavailable")
        memory = RecognitionMemory({})
        with self.assertLogs("ramble.transcribe", level="WARNING"):
            memory.prepare()
        memory.close()
        self.assertEqual(memory.snapshot()["wired_limit_bytes"], 0)

    def test_invalid_settings_do_not_apply_partial_policy(self):
        for key, value in [("whisper_wired_mb", -1), ("whisper_cache_mb", float("inf")),
                           ("whisper_residency_refresh_ms", "1000"), ("whisper_wired_mb", True)]:
            with self.subTest(key=key, value=value), self.assertLogs("ramble.transcribe", level="WARNING"):
                RecognitionMemory({key: value}).prepare()
        self.importer.assert_not_called()

    def test_memory_diagnostic_failure_is_nonfatal(self):
        memory = RecognitionMemory({})
        self.mx.get_active_memory.side_effect = RuntimeError("counter unavailable")
        memory.prepare()
        stats = memory.snapshot()
        self.assertNotIn("mlx_active_bytes", stats)
        self.assertEqual(stats["mlx_cache_bytes"], 120 * MIB)


if __name__ == "__main__":
    unittest.main()
