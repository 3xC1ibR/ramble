"""Native shortcut registration and paste policy without sending keystrokes."""
import ctypes as C
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import AppKit as A

from ramble import config
from ramble.shortcuts import GlobalShortcut, HotKeyID, SIGNATURE, parse_shortcut, load_shortcut
from ramble.desktop_paste import DesktopPaste


class ShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        A.NSApplication.sharedApplication()

    def test_parser_accepts_aliases_and_rejects_plain_typing(self):
        self.assertEqual(parse_shortcut("ctrl + alt + space"), (49, 6144, "Control+Option+Space"))
        self.assertEqual(parse_shortcut("Command+N")[0], 45)
        self.assertEqual(parse_shortcut("Control+T")[0], 17)
        self.assertEqual(parse_shortcut("Control+Y")[0], 16)
        for invalid in ("Space", "Shift+A", "Control+Control+A", "Super+A", "Control+", "", None, 123):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_shortcut(invalid)

    def test_registration_conflict_preserves_previous_shortcut_and_saved_value(self):
        temporary = tempfile.TemporaryDirectory(prefix="ramble-test-shortcut-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(config, 'HOME', Path(temporary.name)))
        first, second = GlobalShortcut(Mock()), GlobalShortcut(Mock())
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        # Uncommon combinations avoid the user's normal shortcuts.
        first.save("Control+Option+Shift+F8")
        second.register("Control+Option+Shift+F9")
        self.assertEqual(load_shortcut(), "Control+Option+Shift+F8")
        with self.assertRaisesRegex(ValueError, "unavailable"):
            first.save("Control+Option+Shift+F9")
        self.assertEqual(first.value, "Control+Option+Shift+F8")
        self.assertEqual(load_shortcut(), first.value)
        with patch.object(Path, 'write_text', side_effect=OSError("Disk unavailable")):
            with self.assertRaises(OSError):
                first.save("Control+Option+Shift+F10")
        self.assertEqual(first.value, "Control+Option+Shift+F8")

    def test_missing_permission_or_changed_focus_keeps_clipboard_fallback(self):
        desktop = DesktopPaste()
        with patch.object(desktop, "permitted", return_value=False), patch("ramble.desktop_paste.Q.CGEventPostToPid") as post:
            pasted, detail = desktop.paste((123, "input"))
            self.assertFalse(pasted)
            self.assertIn("Accessibility", detail)
            post.assert_not_called()
        with patch.object(desktop, "permitted", return_value=True), patch.object(desktop, "target", return_value=(124, "other")), \
                patch("ramble.desktop_paste.Q.CGEventPostToPid") as post:
            self.assertFalse(desktop.paste((123, "input"))[0])
            post.assert_not_called()

    def send_event(self, shortcut, kind, identifier=None):
        library = shortcut.library
        for name, result, arguments in (
            ("CreateEvent", C.c_int32, [C.c_void_p, C.c_uint32, C.c_uint32, C.c_double, C.c_uint32, C.POINTER(C.c_void_p)]),
            ("SetEventParameter", C.c_int32, [C.c_void_p, C.c_uint32, C.c_uint32, C.c_uint32, C.c_void_p]),
            ("SendEventToEventTarget", C.c_int32, [C.c_void_p, C.c_void_p]),
            ("ReleaseEvent", None, [C.c_void_p]),
        ):
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
        event = C.c_void_p()
        self.assertEqual(library.CreateEvent(None, int.from_bytes(b'keyb', 'big'), kind, 0, 0, C.byref(event)), 0)
        try:
            identifier = HotKeyID(SIGNATURE, shortcut.sequence if identifier is None else identifier)
            self.assertEqual(library.SetEventParameter(event, int.from_bytes(b'----', 'big'),
                                                      int.from_bytes(b'hkid', 'big'), C.sizeof(identifier), C.byref(identifier)), 0)
            return library.SendEventToEventTarget(event, library.GetApplicationEventTarget())
        finally:
            library.ReleaseEvent(event)

    def test_native_event_dispatch_toggles_once_per_press_and_ignores_repeats(self):
        callback = Mock()
        shortcut = GlobalShortcut(callback)
        self.addCleanup(shortcut.close)
        shortcut.register("Control+Option+Shift+F8")
        for kind in (5, 5, 6, 5, 6):
            self.assertEqual(self.send_event(shortcut, kind), 0)
        self.assertEqual(callback.call_count, 2)

    def test_escape_cancels_recording_and_processing_without_late_paste(self):
        from ramble.app import RambleApp
        from ramble.dictation import Dictation

        app = RambleApp.__new__(RambleApp)
        recorder = SimpleNamespace(active=True, error=None, set_dictation=Mock())
        controller = Dictation(recorder)
        app.daemon = SimpleNamespace(dictation=controller)
        app.desktop = Mock()
        app.dictation_shortcut = shortcut = GlobalShortcut(app.toggle_dictation, cancel=app.cancel_dictation)
        self.addCleanup(shortcut.close)
        self.assertFalse(shortcut.cancel_reference)
        previous_identifier = None
        with patch('ramble.dictation.state.is_listening', return_value=True), patch('ramble.dictation.threading.Thread'):
            for finishing in (False, True):
                app.toggle_dictation()
                recording = controller.recording
                self.assertTrue(shortcut.cancel_reference)
                if previous_identifier:
                    self.assertNotEqual(self.send_event(shortcut, 5, previous_identifier), 0)
                    self.assertIs(controller.recording, recording)
                if finishing:
                    app.toggle_dictation()
                    self.assertTrue(controller.finishing)
                    self.assertTrue(shortcut.cancel_reference)
                previous_identifier = shortcut.cancel_identifier
                controller.results.put((recording.id, "Cancelled text", None))
                self.assertEqual(self.send_event(shortcut, 5, previous_identifier), 0)
                self.assertTrue(recording.cancelled.is_set())
                self.assertIsNone(controller.recording)
                self.assertFalse(controller.finishing)
                self.assertFalse(shortcut.cancel_reference)
                self.assertEqual(controller.activity.label, "Cancelled")
                recorder.set_dictation.assert_called_with(None)
                app.poll_dictation(None)
                app.desktop.copy.assert_not_called()
                app.desktop.paste.assert_not_called()

    def test_escape_registration_releases_on_completion_and_close(self):
        first = GlobalShortcut(Mock(), cancel=Mock())
        second = GlobalShortcut(Mock(), cancel=Mock())
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.set_cancel_active(True)
        with self.assertRaisesRegex(ValueError, "Escape is reserved"):
            second.set_cancel_active(True)
        first.set_cancel_active(False)
        second.set_cancel_active(True)
        second.close()
        first.set_cancel_active(True)
        self.assertTrue(first.cancel_reference)

    def test_poll_releases_escape_after_completion_or_microphone_stop(self):
        from ramble.app import RambleApp
        app = SimpleNamespace(daemon=SimpleNamespace(dictation=Mock(recording=None)),
                              desktop=Mock(), dictation_shortcut=Mock())
        RambleApp.poll_dictation(app, None)
        app.dictation_shortcut.set_cancel_active.assert_called_once_with(False)

    def test_escape_conflict_prevents_starting_uncancellable_dictation(self):
        from ramble.app import RambleApp
        controller = Mock(recording=None)
        app = SimpleNamespace(daemon=SimpleNamespace(dictation=controller), desktop=Mock(), dictation_shortcut=Mock())
        app.dictation_shortcut.set_cancel_active.side_effect = ValueError("Escape is reserved")
        RambleApp.toggle_dictation(app)
        controller.toggle.assert_not_called()
        controller.notice.assert_called_once_with("Unavailable", "Escape is reserved", error=True)

    def test_paste_posts_only_command_v_to_the_original_focused_application(self):
        desktop = DesktopPaste()
        target = (123, "input")
        with patch.object(desktop, "permitted", return_value=True), patch.object(desktop, "target", return_value=target), \
                patch("ramble.desktop_paste.Q.CGEventPostToPid") as post:
            self.assertTrue(desktop.paste(target)[0])
            self.assertEqual([call.args[0] for call in post.call_args_list], [123, 123])
            from ramble.desktop_paste import Q
            for call in post.call_args_list:
                self.assertEqual(Q.CGEventGetIntegerValueField(call.args[1], Q.kCGKeyboardEventKeycode), 9)
                self.assertEqual(Q.CGEventGetFlags(call.args[1]), Q.kCGEventFlagMaskCommand)


if __name__ == '__main__':
    unittest.main()
