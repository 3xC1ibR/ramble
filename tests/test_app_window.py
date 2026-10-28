"""Native app window behavior without a daemon, mic, or model calls."""
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import AppKit as A
import Foundation as F

from ramble import config, instructions, usage
from ramble.app_window import RambleWindow, number


class AppWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        A.NSApplication.sharedApplication()

    def setUp(self):
        native = A.NSApplication.sharedApplication()
        previous_menu = native.mainMenu()
        self.addCleanup(native.setMainMenu_, previous_menu)
        native.setMainMenu_(None)
        tmp = tempfile.TemporaryDirectory(prefix="ramble-test-window-", dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.enterContext(patch.object(config, "HOME", self.home))
        self.app = SimpleNamespace(mini=SimpleNamespace(visible=True, menu_open=False),
                                   model_snapshot=None, model_busy=False, model_error=None,
                                   toggle=Mock(), toggle_voice=Mock(), toggle_mini=Mock())
        self.window = RambleWindow(self.app)
        self.addCleanup(self.window.close)

    def test_native_window_is_keyable_resizable_and_retained_on_close(self):
        window = self.window.window
        self.assertTrue(window.canBecomeKeyWindow())
        self.assertTrue(window.styleMask() & A.NSWindowStyleMaskResizable)
        self.assertFalse(window.isReleasedWhenClosed())
        window.close()
        self.assertEqual(window.title(), "Ramble")
        self.assertTrue(self.window.timer.isValid())
        self.assertFalse((self.home / "state.json").exists())
        self.app.toggle.assert_not_called()

    def test_native_keyboard_menu_and_text_undo_are_available(self):
        menu = A.NSApplication.sharedApplication().mainMenu()
        edit = menu.itemAtIndex_(1).submenu()
        keys = {item.title(): item.keyEquivalent() for item in edit.itemArray()}
        self.assertEqual(keys["Copy"], "c")
        self.assertEqual(keys["Paste"], "v")
        self.assertEqual(keys["Undo"], "z")
        self.assertTrue(self.window.editor.allowsUndo())

    def test_native_window_controller_is_reused_and_settings_selects_page(self):
        from ramble.app import RambleApp
        controller = Mock()
        app = SimpleNamespace(main_window=None)
        with patch("ramble.app.RambleWindow", return_value=controller) as create:
            RambleApp.open_window(app, "usage")
            RambleApp.open_window(app, "settings")
        create.assert_called_once_with(app)
        self.assertEqual([c.args[0] for c in controller.show.call_args_list], ["usage", "settings"])

    def test_instructions_conflict_keeps_draft_and_external_content(self):
        self.window.editor.setString_("My unsaved draft")
        instructions.save("Changed in Companion")
        self.window.save_instructions()
        self.assertEqual(self.window.editor.string(), "My unsaved draft")
        self.assertEqual(instructions.snapshot()["text"], "Changed in Companion")
        self.assertIn("changed elsewhere", self.window.instructions_status.stringValue())
        self.window.load_instructions()
        self.window.editor.setString_("Merged preferences")
        self.window.save_instructions()
        self.assertEqual(instructions.snapshot()["text"], "Merged preferences")

    def test_unreadable_initial_instructions_prevent_saving(self):
        self.window.instructions_version = None
        self.window.editor.setString_("Draft")
        self.window.save_instructions()
        self.assertFalse((self.home / "instructions.md").exists())
        self.assertIn("Reload", self.window.instructions_status.stringValue())

    def test_model_popup_uses_existing_menu_and_protects_it_until_closed(self):
        menu = Mock()
        self.app.mini_model_menu = Mock(return_value=menu)
        def popup(*args):
            self.assertTrue(self.app.mini.menu_open)
        menu.popUpMenuPositioningItem_atLocation_inView_.side_effect = popup
        self.window.choose_model(self.window.model_rows[0][2])
        self.assertFalse(self.app.mini.menu_open)
        self.app.mini_model_menu.assert_called_once()

    def test_usage_render_preserves_unknowns_and_attribution(self):
        store = usage.UsageStore()
        store.record("one", started_at=time.time(), purpose="reply", provider="codex", model="test",
                     counts={"input_tokens": 100, "output_tokens": 20})
        store.record("unknown", started_at=time.time(), purpose="chat_followup", provider="jev", counts={})
        self.window.report = store.report()
        self.window.render_usage()
        self.assertEqual(self.window.cards[0][1].stringValue(), "≥ 120")
        self.assertEqual(self.window.cards[2][1].stringValue(), "—")
        self.assertEqual(len(self.window.attribution_data.rows), 2)
        self.window.grouping.setSelectedSegment_(1)
        self.window.render_usage()
        self.assertTrue(any("unknown" in row["activity"] and row["total"] == "—" for row in self.window.attribution_data.rows))
        self.assertEqual(number({"total_tokens": 0}, "total_tokens"), "0")

    def test_period_change_while_loading_does_not_show_old_period(self):
        release = threading.Event()
        started = threading.Event()
        store = usage.UsageStore()
        def recover():
            started.set()
            release.wait(2)
            return 0
        store.recover = recover
        store.refresh_linked = lambda: None
        self.window.store_factory = lambda: store
        try:
            self.window.refresh_usage()
            self.assertTrue(started.wait(1))
            self.window.days = 7
            self.window.refresh_usage()
            release.set()
            deadline = time.monotonic() + 2
            while self.window.usage_busy and time.monotonic() < deadline:
                if self.window.usage_result and self.window.usage_result[0] == 1:
                    self.window.tick()
                    self.assertIsNone(self.window.report)
                else:
                    self.window.tick()
                time.sleep(.01)
            self.assertFalse(self.window.usage_busy)
            self.assertIsNotNone(self.window.report)
        finally:
            release.set()

    def test_both_pages_fit_minimum_size_and_render(self):
        self.window.root.setFrameSize_(F.NSMakeSize(920, 720))
        self.window.layout()
        self.assertLessEqual(F.NSMaxY(self.window.linked_scroll.frame()), self.window.usage_note.frame().origin.y)
        self.assertLessEqual(F.NSMaxY(self.window.editor_scroll.frame()), self.window.save_button.frame().origin.y)
        self.assertLessEqual(F.NSMaxY(self.window.usage_note2.frame()), 720)
        for page in ("settings", "usage"):
            with patch.object(self.window, "refresh_usage"):
                self.window.select_page(page)
            bitmap = self.window.root.bitmapImageRepForCachingDisplayInRect_(self.window.root.bounds())
            self.window.root.cacheDisplayInRect_toBitmapImageRep_(self.window.root.bounds(), bitmap)
            self.assertGreater(bitmap.pixelsWide(), 0)
            self.assertGreater(bitmap.pixelsHigh(), 0)


if __name__ == "__main__":
    unittest.main()
