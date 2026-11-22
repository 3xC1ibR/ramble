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

from ramble import config, instructions, state, usage
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
        self.enterContext(patch.multiple(config, HOME=self.home, STATE_PATH=self.home / "state.json"))
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

    def test_dictation_shortcut_saves_without_changing_capture_and_preserves_failed_draft(self):
        self.app.save_dictation_shortcut = Mock(return_value="Control+Option+D")
        self.window.shortcut_field.setStringValue_("ctrl+alt+d")
        self.window.shortcut_button.performClick_(None)
        self.app.save_dictation_shortcut.assert_called_once_with("ctrl+alt+d")
        self.assertEqual(self.window.shortcut_field.stringValue(), "Control+Option+D")
        self.assertIn("saved", self.window.shortcut_note.stringValue())
        self.app.save_dictation_shortcut.side_effect = ValueError("Shortcut unavailable")
        self.window.shortcut_field.setStringValue_("Control+Space")
        self.window.shortcut_button.performClick_(None)
        self.assertEqual(self.window.shortcut_field.stringValue(), "Control+Space")
        self.assertEqual(self.window.shortcut_note.stringValue(), "Shortcut unavailable")
        self.app.toggle.assert_not_called()
        self.assertFalse((self.home / "state.json").exists())

    def test_native_keyboard_menu_and_text_undo_are_available(self):
        menu = A.NSApplication.sharedApplication().mainMenu()
        edit = menu.itemAtIndex_(1).submenu()
        keys = {item.title(): item.keyEquivalent() for item in edit.itemArray()}
        self.assertEqual(keys["Copy"], "c")
        self.assertEqual(keys["Paste"], "v")
        self.assertEqual(keys["Undo"], "z")
        self.assertTrue(self.window.editor.allowsUndo())

    def test_reading_navigation_reuses_window_and_preserves_settings_draft(self):
        self.window.editor.setString_('Unsaved settings draft')
        with patch.object(self.window.reading_view, 'refresh') as refresh:
            self.window.nav[2].performClick_(None)
        refresh.assert_called_once()
        self.assertEqual(self.window.page, 'reading')
        self.assertFalse(self.window.reading_view.view.isHidden())
        self.assertTrue(self.window.settings_page.isHidden())
        self.window.select_page('settings')
        self.assertTrue(self.window.reading_view.view.isHidden())
        self.assertEqual(self.window.editor.string(), 'Unsaved settings draft')
        self.assertFalse((self.home / 'state.json').exists())

    def test_native_window_controller_is_reused_and_settings_selects_page(self):
        from ramble.app import RambleApp
        controller = Mock()
        app = SimpleNamespace(main_window=None)
        with patch("ramble.app.RambleWindow", return_value=controller) as create:
            RambleApp.open_window(app, "usage")
            RambleApp.open_window(app, "settings")
        create.assert_called_once_with(app)
        self.assertEqual([c.args[0] for c in controller.show.call_args_list], ["usage", "settings"])

    def test_expansion_opens_saved_reading_and_preserves_explicit_navigation(self):
        self.window.window.orderOut_(None)
        with patch.object(self.window.reading_view.service, 'store') as store, patch.object(self.window, 'tick'), patch.object(self.window.reading_view, 'refresh'):
            store.return_value.snapshot.return_value = {'reading': {'status': 'paused'}}
            self.window.show()
            self.assertEqual(self.window.page, 'reading')
            self.window.show('settings')
            self.assertEqual(self.window.page, 'settings')
            self.window.show()
            self.assertEqual(self.window.page, 'settings')
            self.window.window.orderOut_(None)
            self.window.show('usage')
            self.assertEqual(self.window.page, 'usage')

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

    def test_pause_menu_and_early_resume_use_shared_quiet_pause_state_on_both_pages(self):
        from ramble.app import RambleApp
        self.app.tick = Mock()
        self.app.pause = lambda seconds: RambleApp.pause(self.app, seconds)
        self.app.resume = lambda sender: RambleApp.resume(self.app, sender)
        self.window.window.orderFront_(None)
        with patch.object(config, "ensure_dirs"), patch.object(self.window, "refresh_usage"):
            for page in ("settings", "usage"):
                self.window.select_page(page)
                for index, seconds in enumerate((3600, 10800)):
                    state.write(True)
                    self.window.tick()
                    self.assertEqual(self.window.pause_button.title(), "Pause Ramble…")
                    self.window.pause_menu.performActionForItemAtIndex_(index)
                    paused = state.read()
                    self.assertAlmostEqual(paused["paused_until"] - paused["changed_at"], seconds, delta=.1)
                    self.assertFalse(state.activity_allowed())
                    self.assertEqual(self.window.pause_button.title(), "Resume Ramble")
                    self.assertIn("Paused until", self.window.capture_status.stringValue())
                    self.window.pause_button.performClick_(None)
                    self.assertTrue(state.is_listening())
                    self.assertIsNone(state.read()["paused_until"])
                    self.assertEqual(state.read()["last_paused_at"], paused["last_paused_at"])
                    self.assertFalse(state.activity_allowed(paused["changed_at"] - 1))
                    self.assertEqual(self.window.pause_button.title(), "Pause Ramble…")

    def test_pause_control_tracks_external_changes_and_expiry_without_mutating_state(self):
        self.window.window.orderFront_(None)
        with patch.object(config, "ensure_dirs"), patch.object(self.window, "refresh_usage"):
            self.window.select_page("usage")
            state.write(True, time.time() + 3600)
            self.window.tick()
            self.assertEqual(self.window.pause_button.title(), "Resume Ramble")
            state.write(True, time.time() - 1)
            before = config.STATE_PATH.read_bytes()
            self.window.tick()
            self.assertEqual(self.window.pause_button.title(), "Pause Ramble…")
            self.assertEqual(self.window.capture_status.stringValue(), "Listening")
            self.assertEqual(config.STATE_PATH.read_bytes(), before)
            state.write(False)
            self.window.tick()
            self.assertEqual(self.window.capture_status.stringValue(), "Microphone off")
            self.assertTrue(self.window.pause_button.isEnabled())

    def test_resume_click_at_expiry_does_not_toggle_microphone_off(self):
        from ramble.app import RambleApp
        self.app.tick = Mock()
        self.app.resume = lambda sender: RambleApp.resume(self.app, sender)
        self.window.window.orderFront_(None)
        with patch.object(config, "ensure_dirs"):
            state.write(True, time.time() + 3600)
            self.window.tick()
            state.write(True, time.time() - 1)
            self.window.pause_button.performClick_(None)
            self.assertTrue(state.is_listening())
            self.assertIsNone(state.read()["paused_until"])
            self.app.toggle.assert_not_called()

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
